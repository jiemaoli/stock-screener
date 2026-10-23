#!/usr/bin/env python3
"""Interactive web dashboard for 强势突破股票筛选器 results.

Usage:
    python dashboard.py                    # Start dashboard on port 5050
    python dashboard.py --port 8080        # Custom port
    python dashboard.py --scan             # Run test scan first, then launch
    python dashboard.py --scan --full      # Run full scan first, then launch
"""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Timer
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, redirect, render_template_string, request, session, url_for
from dotenv import load_dotenv
from werkzeug.security import check_password_hash

from src.config.scan_settings import (
    UNIVERSE_MODES,
    read_scan_settings,
    write_scan_settings,
)
from src.config.telegram_settings import (
    public_telegram_settings,
    read_telegram_settings,
    write_telegram_settings,
)
from src.integrations.screener_api import completed_envelope, failed_envelope, running_envelope

PROJECT_ROOT = Path(__file__).parent.resolve()
load_dotenv(PROJECT_ROOT / ".env")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true",
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

SCAN_DIR = PROJECT_ROOT / "data" / "daily_scans"
VENV_PYTHON = PROJECT_ROOT / "venv" / "bin" / "python"
SCAN_LOG_DIR = PROJECT_ROOT / "data" / "logs"
SCAN_STATE_FILE = SCAN_LOG_DIR / "scan_job.json"
SCAN_LOCK_FILE = PROJECT_ROOT / "data" / ".scan.lock"
API_SCAN_DIR = PROJECT_ROOT / "data" / "api_scans"
SCHEDULE_CONFIG_FILE = PROJECT_ROOT / "data" / "config" / "schedule.json"
SCHEDULER_STATE_FILE = PROJECT_ROOT / "data" / "logs" / "scheduler_state.json"
SCAN_COMMAND_ARGS = [
    "run_optimized_scan.py",
    "--conservative",
    "--git-storage",
    "--clear-progress",
]

SYSTEM_INFO = {
    "schedule": {
        "cron": "15 18 * * 1-5",
        "timezone": "America/New_York",
        "description": "每周一至周五，美东时间 18:15（美股收盘后）",
        "beijing_time": "夏令时次日 06:15；冬令时次日 07:15",
        "command": "python run_optimized_scan.py --conservative --git-storage --clear-progress",
        "overlap_protection": "/app/data/.scan.lock（手动与定时任务共用）",
    },
    "runtime": {
        "universe_size": "由界面配置决定",
        "test_size": 100,
        "full_scan_time": "指数并集通常约 10–20 分钟；全部美股通常约 60–90 分钟",
        "workers": 2,
        "request_delay": "全局价格请求之间至少间隔 1.0 秒",
        "price_refresh": "首次获取五年日线，之后增量更新；每 30 天完整校验一次",
        "fundamental_cache": "财报季缓存 7 天，非财报季缓存 90 天",
        "telegram": "全量和定时扫描完成后通知；测试扫描默认不通知",
    },
    "filters": [
        {"name": "股票池", "value": "由扫描配置决定", "meaning": "默认标普 500 + 纳斯达克 100；可随时切回原全部美股模式"},
        {"name": "历史数据", "value": "至少 200 个交易日", "meaning": "确保均线和阶段判断有足够样本"},
        {"name": "股价", "value": "$5–$10,000", "meaning": "排除低价股和明显异常价格"},
        {"name": "流动性", "value": "20 日平均成交量 ≥ 100,000", "meaning": "排除成交过于稀疏的股票"},
        {"name": "严重回撤", "value": "5 年最大回撤不得超过 90%", "meaning": "排除历史结构严重破坏的标的"},
    ],
    "market_gate": [
        "SPY 必须处于 Phase 1（筑底）或 Phase 2（上升趋势）",
        "全市场 Phase 2 股票占比必须达到 15%",
        "市场门槛只控制买入信号；卖出信号在任何市场环境下都会检查",
    ],
    "phases": [
        {"phase": 1, "name": "筑底/盘整", "meaning": "均线趋平，价格形成底部；尚不是正式买点"},
        {"phase": 2, "name": "上升趋势", "meaning": "价格和均线多头排列；买入候选必须处于本阶段"},
        {"phase": 3, "name": "派发/见顶", "meaning": "趋势趋平或转弱，关注减仓和卖出风险"},
        {"phase": 4, "name": "下降趋势", "meaning": "价格和均线走弱，应回避或考虑退出"},
    ],
    "minervini": [
        "股价高于 150 日和 200 日均线",
        "150 日均线高于 200 日均线",
        "200 日均线最近至少一个月向上",
        "50 日均线高于 150 日均线，且 150 日高于 200 日",
        "股价高于 50 日均线",
        "股价至少比 52 周低点高 30%",
        "股价距离 52 周高点不超过 25%",
        "确认处于 Phase 2 上升阶段",
    ],
    "buy_scores": [
        {"name": "趋势结构", "points": 40, "meaning": "均线排列与斜率、突破质量、距离均线是否过度延伸"},
        {"name": "基本面", "points": 40, "meaning": "营收增长、EPS 增长、库存变化及当前预留的利润率分值"},
        {"name": "风险收益比", "points": 15, "meaning": "以建议止损和 30% 上行目标估算；2:1 以下不得分"},
        {"name": "相对强度 RS", "points": 10, "meaning": "股票相对 SPY 的表现斜率，正值表示跑赢市场"},
        {"name": "成交量", "points": 10, "meaning": "最近上涨日与下跌日的平均成交量对比"},
        {"name": "入场质量", "points": 5, "meaning": "距离 52 周高点、50 日均线和理想突破区域的位置"},
        {"name": "VCP 加分", "points": 5, "meaning": "波动率收缩形态的次数、质量和突破量能"},
    ],
    "thresholds": {
        "buy": "Phase 2 + Minervini 至少通过 7/8 + 总分 ≥ 60（满分 125）",
        "sell": "Phase 3/4 + 总分 ≥ 60（60 中等、70 高、80 严重）",
        "note": "生产全市场扫描的真实买入阈值是 60；config.yaml 中的 70 属于旧版流程，不控制本页面的扫描。",
    },
    "terms": [
        {"name": "SMA 50/150/200", "meaning": "50、150、200 个交易日的简单移动平均线，用于判断短中长期趋势结构"},
        {"name": "Slope", "meaning": "均线近期变化斜率；正值代表均线向上，负值代表均线向下"},
        {"name": "RS", "meaning": "Relative Strength，相对 SPY 的强弱趋势；不是 RSI 超买超卖指标"},
        {"name": "R:R", "meaning": "Reward/Risk，预期收益与止损风险之比；例如 3:1 表示潜在收益约为风险的 3 倍"},
        {"name": "VCP", "meaning": "Volatility Contraction Pattern，波动率收缩形态，观察振幅和成交量逐步收敛"},
        {"name": "Market Breadth", "meaning": "市场宽度，即处于各阶段股票的比例；Phase 2 占比反映上涨参与度"},
        {"name": "Entry Quality", "meaning": "入场位置质量：良好、偏离或较差；用于提示是否追高，不等同于独立买入信号"},
        {"name": "Stop Loss", "meaning": "根据均线、近期低点和最大风险计算的参考止损位，需结合个人仓位管理"},
    ],
}


def _build_system_info():
    info = json.loads(json.dumps(SYSTEM_INFO, ensure_ascii=False))
    config = _read_schedule_config()
    scan_settings = read_scan_settings()
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    selected_days = "、".join(weekday_names[index] for index in config["weekdays"])
    info["schedule"].update(config)
    info["schedule"].update(
        {
            "cron": f"{config['minute']} {config['hour']} * * "
            + ",".join(str((day + 1) % 7) for day in config["weekdays"]),
            "description": f"{selected_days}，美东时间 {config['hour']:02d}:{config['minute']:02d}",
            "next_run": _next_schedule_times(config),
            "state": _read_json_file(SCHEDULER_STATE_FILE, {}),
        }
    )
    info["scan"] = {
        **scan_settings,
        "universe_label": UNIVERSE_MODES[scan_settings["universe_mode"]],
        "universe_options": [
            {"value": value, "label": label}
            for value, label in UNIVERSE_MODES.items()
        ],
    }
    # /api/system-info requires an authenticated Dashboard session. Include the
    # token so the password field can be revealed on explicit user request.
    telegram = public_telegram_settings(include_token=True)
    info["telegram"] = telegram
    if telegram["enabled"] and telegram["bot_token_configured"] and telegram["chat_id"]:
        enabled_types = []
        if telegram["notify_full"]:
            enabled_types.append("正式/定时扫描")
        if telegram["notify_test"]:
            enabled_types.append("测试扫描")
        info["runtime"]["telegram"] = "已启用：" + "、".join(enabled_types)
    else:
        info["runtime"]["telegram"] = "未启用或配置不完整"
    info["runtime"]["universe_size"] = info["scan"]["universe_label"]
    info["filters"][0]["value"] = info["scan"]["universe_label"]
    return info


def _read_json_file(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return default

_scan_process = None
_scan_guard = threading.Lock()
_login_attempts = {}
_login_guard = threading.Lock()

DEFAULT_SCHEDULE = {
    "enabled": True,
    "timezone": "America/New_York",
    "weekdays": [0, 1, 2, 3, 4],
    "hour": 18,
    "minute": 15,
}


def _read_schedule_config():
    config = dict(DEFAULT_SCHEDULE)
    if SCHEDULE_CONFIG_FILE.exists():
        try:
            saved = json.loads(SCHEDULE_CONFIG_FILE.read_text(encoding="utf-8"))
            config.update({key: saved[key] for key in DEFAULT_SCHEDULE if key in saved})
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return config


def _write_schedule_config(config):
    SCHEDULE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = SCHEDULE_CONFIG_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_file.replace(SCHEDULE_CONFIG_FILE)


def _next_schedule_times(config):
    if not config.get("enabled"):
        return {"new_york": None, "beijing": None}

    eastern = ZoneInfo("America/New_York")
    beijing = ZoneInfo("Asia/Shanghai")
    now_eastern = datetime.now(timezone.utc).astimezone(eastern)
    weekdays = set(config.get("weekdays", []))

    for offset in range(15):
        day = now_eastern.date() + timedelta(days=offset)
        if day.weekday() not in weekdays:
            continue
        candidate = datetime(
            day.year,
            day.month,
            day.day,
            int(config["hour"]),
            int(config["minute"]),
            tzinfo=eastern,
        )
        if candidate > now_eastern:
            return {
                "new_york": candidate.isoformat(),
                "beijing": candidate.astimezone(beijing).isoformat(),
            }
    return {"new_york": None, "beijing": None}


def _login_is_rate_limited(ip_address):
    now = time.time()
    with _login_guard:
        recent = [attempt for attempt in _login_attempts.get(ip_address, []) if now - attempt < 300]
        _login_attempts[ip_address] = recent
        return len(recent) >= 5


def _record_login_failure(ip_address):
    with _login_guard:
        _login_attempts.setdefault(ip_address, []).append(time.time())


def _clear_login_failures(ip_address):
    with _login_guard:
        _login_attempts.pop(ip_address, None)


def _read_scan_state():
    if not SCAN_STATE_FILE.exists():
        return {"status": "idle"}
    try:
        return json.loads(SCAN_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown", "error": "Could not read scan state"}


def _write_scan_state(state):
    _write_state_file(SCAN_STATE_FILE, state)


def _write_state_file(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = path.with_suffix(".tmp")
    temp_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp_file.replace(path)


def _process_is_running(process):
    return process is not None and process.poll() is None


def _monitor_scan(process, state, log_file):
    global _scan_process
    return_code = process.wait()
    log_file.close()

    with _scan_guard:
        final_state = dict(state)
        final_state["status"] = "completed" if return_code == 0 else "failed"
        final_state["completed_at"] = datetime.now().astimezone().isoformat()
        final_state["return_code"] = return_code
        if return_code != 0:
            final_state["error"] = "Scan process failed; check the scan log for details"
        _write_scan_state(final_state)
        if _scan_process is process:
            _scan_process = None


def _monitor_api_scan(process, state, log_file, state_file, result_file):
    global _scan_process
    return_code = process.wait()
    log_file.close()

    with _scan_guard:
        final_state = dict(state)
        final_state["status"] = "completed" if return_code == 0 else "failed"
        final_state["completed_at"] = datetime.now().astimezone().isoformat()
        final_state["return_code"] = return_code
        final_state["result_available"] = return_code == 0 and result_file.exists()
        if return_code != 0:
            final_state["error"] = "Screening process failed"
        elif not result_file.exists():
            final_state["status"] = "failed"
            final_state["error"] = "Screening completed without a result file"
        _write_state_file(state_file, final_state)
        if _scan_process is process:
            _scan_process = None


def _internal_api_auth_error():
    expected_token = os.getenv("SCREENER_API_TOKEN", "").strip()
    if not expected_token:
        return jsonify({"error": "Internal screening API is not configured"}), 503

    authorization = request.headers.get("Authorization", "")
    supplied_token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if not supplied_token:
        supplied_token = request.headers.get("X-API-Key", "").strip()
    if not secrets.compare_digest(supplied_token, expected_token):
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _normalize_api_tickers(value):
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise ValueError("tickers must be a non-empty JSON array")
    if len(value) > 600:
        raise ValueError("tickers supports at most 600 symbols per request")

    tickers = []
    seen = set()
    for raw_ticker in value:
        ticker = str(raw_ticker).strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker):
            raise ValueError(f"Invalid ticker: {raw_ticker}")
        if ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers


def parse_scan_file(filepath):
    """Parse a scan .txt file into structured data."""
    text = Path(filepath).read_text(encoding="utf-8")

    data = {
        "scan_date": "",
        "generated": "",
        "stats": {},
        "spy": {},
        "breadth": {},
        "regime": "",
        "buy_signals": [],
        "sell_signals": [],
    }

    # Header stats
    m = re.search(r"Scan Date:\s*(.+)", text)
    if m:
        data["scan_date"] = m.group(1).strip()

    m = re.search(r"Generated:\s*(.+)", text)
    if m:
        data["generated"] = m.group(1).strip()

    m = re.search(r"Total Universe:\s*([\d,]+)", text)
    if m:
        data["stats"]["total_universe"] = int(m.group(1).replace(",", ""))

    m = re.search(r"Analyzed:\s*([\d,]+)", text)
    if m:
        data["stats"]["analyzed"] = int(m.group(1).replace(",", ""))

    m = re.search(r"Processing Time:\s*([\d.]+)", text)
    if m:
        data["stats"]["processing_minutes"] = float(m.group(1))

    m = re.search(r"Error Rate:\s*([\d.]+)%", text)
    if m:
        data["stats"]["error_rate"] = float(m.group(1))

    m = re.search(r"Buy Signals:\s*(\d+)", text)
    if m:
        data["stats"]["buy_count"] = int(m.group(1))

    m = re.search(r"Sell Signals:\s*(\d+)", text)
    if m:
        data["stats"]["sell_count"] = int(m.group(1))

    # SPY
    m = re.search(r"Phase:\s*(\d)\s*-\s*(.+)", text)
    if m:
        data["spy"]["phase"] = int(m.group(1))
        data["spy"]["phase_name"] = m.group(2).strip()

    m = re.search(r"Trend:\s*(\w+)", text)
    if m:
        data["spy"]["trend"] = m.group(1).strip()

    m = re.search(r"Current Price:\s*\$([\d.]+)", text)
    if m:
        data["spy"]["price"] = float(m.group(1))

    m = re.search(r"Confidence:\s*(\d+)%", text)
    if m:
        data["spy"]["confidence"] = int(m.group(1))

    # Breadth
    for phase_num, label in [(1, "phase_1"), (2, "phase_2"), (3, "phase_3"), (4, "phase_4")]:
        m = re.search(
            rf"Phase {phase_num} \([^)]+\):\s*(\d+)\s*stocks\s*\(([\d.]+)%\)", text
        )
        if m:
            data["breadth"][f"{label}_count"] = int(m.group(1))
            data["breadth"][f"{label}_pct"] = float(m.group(2))

    m = re.search(r"Market Regime:\s*(.+)", text)
    if m:
        data["regime"] = m.group(1).strip()

    # Buy signals — extract each signal as header+details block
    buy_pattern = re.compile(
        r"BUY #(\d+):\s*(\w+)\s*\|\s*Score:\s*([\d.]+)/(\d+)"
        r".*?(?=BUY #\d+:|SELL #\d+:|={50,}.*(?:TOP SELL|END OF|ADDITIONAL)|\Z)",
        re.DOTALL,
    )
    for bm in buy_pattern.finditer(text):
        block = bm.group(0)
        signal = {
            "rank": int(bm.group(1)),
            "ticker": bm.group(2),
            "score": float(bm.group(3)),
            "max_score": int(bm.group(4)),
            "reasons": [],
        }

        m = re.search(r"Phase:\s*(\d)", block)
        if m:
            signal["phase"] = int(m.group(1))

        m = re.search(r"Entry Quality:\s*(\w+)", block)
        if m:
            signal["entry_quality"] = m.group(1)

        m = re.search(r"Stop Loss:\s*\$([\d.]+)", block)
        if m:
            signal["stop_loss"] = float(m.group(1))

        m = re.search(r"Risk/Reward:\s*([\d.]+):1\s*\(Risk \$([\d.]+),\s*Reward \$([\d.]+)\)", block)
        if m:
            signal["rr_ratio"] = float(m.group(1))
            signal["risk"] = float(m.group(2))
            signal["reward"] = float(m.group(3))

        m = re.search(r"RS:\s*([-\d.]+)", block)
        if m:
            signal["rs"] = float(m.group(1))

        m = re.search(r"Volume:\s*([\d.]+)x", block)
        if m:
            signal["volume_ratio"] = float(m.group(1))

        reasons = re.findall(r"[•]\s*(.+)", block)
        signal["reasons"] = [r.strip() for r in reasons[:7]]

        m = re.search(r"Overall Assessment:\s*\n(.+)", block)
        if m:
            signal["assessment"] = m.group(1).strip()

        data["buy_signals"].append(signal)

    # Sell signals
    sell_pattern = re.compile(
        r"SELL #(\d+):\s*(\w+)\s*\|\s*Score:\s*([\d.]+)/(\d+)"
        r".*?(?=SELL #\d+:|={50,}.*(?:END OF|ADDITIONAL)|\Z)",
        re.DOTALL,
    )
    for sm in sell_pattern.finditer(text):
        block = sm.group(0)
        signal = {
            "rank": int(sm.group(1)),
            "ticker": sm.group(2),
            "score": float(sm.group(3)),
            "max_score": int(sm.group(4)),
            "reasons": [],
        }

        m = re.search(r"Phase:\s*(\d)\s*\|\s*.*Severity:\s*(\w+)", block)
        if m:
            signal["phase"] = int(m.group(1))
            signal["severity"] = m.group(2)

        m = re.search(r"Breakdown:\s*\$([\d.]+)", block)
        if m:
            signal["breakdown_level"] = float(m.group(1))

        reasons = re.findall(r"[•]\s*(.+)", block)
        signal["reasons"] = [r.strip() for r in reasons[:5]]

        data["sell_signals"].append(signal)

    return data


def get_scan_files():
    """Return formal full-scan reports, newest first, with universe size."""
    reports = []
    if not SCAN_DIR.exists():
        return reports

    for path in sorted(SCAN_DIR.glob("optimized_scan_*.txt"), reverse=True):
        match = re.fullmatch(r"optimized_scan_(\d{8})_(\d{6})\.txt", path.name)
        if not match:
            continue

        total_universe = None
        try:
            with path.open("r", encoding="utf-8") as report:
                for _ in range(20):
                    line = report.readline()
                    if not line:
                        break
                    universe_match = re.search(r"Total Universe:\s*([\d,]+)", line)
                    if universe_match:
                        total_universe = int(universe_match.group(1).replace(",", ""))
                        break
        except OSError:
            continue

        reports.append(
            {
                "name": path.name,
                "path": path.name,
                "date": f"{match.group(1)}_{match.group(2)}",
                "total_universe": total_universe,
            }
        )

    return reports


APP_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64" fill="none">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="64" y2="64" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#1e293b"/>
      <stop offset="100%" stop-color="#0f172a"/>
    </linearGradient>
    <linearGradient id="breakoutGrad" x1="14" y1="46" x2="48" y2="14" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#3b82f6"/>
      <stop offset="50%" stop-color="#06b6d4"/>
      <stop offset="100%" stop-color="#22c55e"/>
    </linearGradient>
  </defs>

  <!-- Background rounded card -->
  <rect width="64" height="64" rx="14" fill="url(#bg)"/>
  <rect x="0.5" y="0.5" width="63" height="63" rx="13.5" stroke="#3b82f6" stroke-opacity="0.35" stroke-width="1"/>

  <!-- Resistance Level Line (Breakout barrier) -->
  <line x1="9" y1="26" x2="55" y2="26" stroke="#94a3b8" stroke-width="1.5" stroke-dasharray="3.5 2.5" stroke-opacity="0.55"/>

  <!-- Candlestick 1: Phase 1 Consolidation base -->
  <line x1="16" y1="41" x2="16" y2="49" stroke="#64748b" stroke-width="1.2" stroke-linecap="round"/>
  <rect x="14" y="43" width="4" height="5" rx="0.8" fill="#64748b"/>

  <!-- Candlestick 2: Higher Low -->
  <line x1="24" y1="36" x2="24" y2="46" stroke="#0ea5e9" stroke-width="1.2" stroke-linecap="round"/>
  <rect x="22" y="38" width="4" height="6" rx="0.8" fill="#0ea5e9"/>

  <!-- Candlestick 3: Testing Resistance -->
  <line x1="32" y1="27" x2="32" y2="40" stroke="#06b6d4" stroke-width="1.2" stroke-linecap="round"/>
  <rect x="30" y="29" width="4" height="8" rx="0.8" fill="#06b6d4"/>

  <!-- Candlestick 4: Massive Stage 2 Breakout Candle -->
  <line x1="40" y1="16" x2="40" y2="33" stroke="#22c55e" stroke-width="1.5" stroke-linecap="round"/>
  <rect x="38" y="19" width="4" height="11" rx="0.8" fill="#22c55e"/>

  <!-- Breakout dynamic trend curve -->
  <path d="M14 46 C21 45, 26 38, 32 31 S42 20, 48 14" stroke="url(#breakoutGrad)" stroke-width="3" stroke-linecap="round" fill="none"/>

  <!-- Arrowhead pointing to top-right -->
  <path d="M38 14 H48 V24" stroke="#22c55e" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" fill="none"/>

  <!-- Glow dot at resistance breakout point -->
  <circle cx="35" cy="26" r="2.2" fill="#22c55e"/>
</svg>"""

LOGIN_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录·强势突破股票筛选器</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
  :root { --bg:#0f1117; --surface:#1a1d27; --border:#2a2d3a; --text:#e4e6eb; --muted:#8b8fa3; --blue:#3b82f6; --red:#ef4444; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center; padding:20px; background:radial-gradient(circle at top, #18233b 0, var(--bg) 48%); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }
  .login-card { width:100%; max-width:400px; background:rgba(26,29,39,.96); border:1px solid var(--border); border-radius:16px; padding:32px; box-shadow:0 24px 70px rgba(0,0,0,.4); }
  .logo { width:48px; height:48px; display:grid; place-items:center; border-radius:12px; margin-bottom:18px; }
  h1 { font-size:22px; margin:0 0 8px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:24px; }
  label { display:block; color:var(--muted); font-size:12px; margin:14px 0 6px; }
  input { width:100%; padding:11px 12px; border-radius:8px; border:1px solid var(--border); background:var(--bg); color:var(--text); outline:none; }
  input:focus { border-color:var(--blue); }
  button { width:100%; margin-top:22px; padding:11px; border:0; border-radius:8px; background:var(--blue); color:white; font-weight:600; cursor:pointer; }
  .error { margin:14px 0 0; color:#fca5a5; background:rgba(239,68,68,.1); border:1px solid rgba(239,68,68,.25); border-radius:7px; padding:9px 10px; font-size:12px; }
</style>
</head>
<body>
  <form class="login-card" method="post" autocomplete="on">
    <div class="logo">{{ app_icon | safe }}</div>
    <h1>强势突破股票筛选器</h1>
    <div class="sub">登录后查看扫描结果、定时任务和选股规则</div>
    <label for="username">账号</label>
    <input id="username" name="username" type="text" required autofocus autocomplete="username">
    <label for="password">密码</label>
    <input id="password" name="password" type="password" required autocomplete="current-password">
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <button type="submit">登录</button>
  </form>
</body>
</html>
"""


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>强势突破股票筛选器</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0d1117;
    --surface: #161b26;
    --surface-hover: #1e2433;
    --surface-subtle: #131722;
    --border: #262d3d;
    --border-light: #363f54;
    --text: #e6edf3;
    --text-strong: #ffffff;
    --muted: #8d96a7;
    --muted-light: #b1b9c7;
    --green: #22c55e;
    --green-bg: rgba(34, 197, 94, 0.12);
    --green-border: rgba(34, 197, 94, 0.28);
    --red: #ef4444;
    --red-bg: rgba(239, 68, 68, 0.12);
    --yellow: #eab308;
    --yellow-bg: rgba(234, 179, 8, 0.12);
    --blue: #3b82f6;
    --blue-bg: rgba(59, 130, 246, 0.12);
    --blue-glow: rgba(59, 130, 246, 0.25);
    --purple: #a855f7;
    --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: var(--font-sans);
    background: var(--bg);
    color: var(--text);
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    line-height: 1.5;
  }

  .font-mono { font-family: var(--font-mono) !important; }

  /* Header */
  .header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 16px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
  }
  .header h1 { font-size: 19px; font-weight: 700; letter-spacing: -0.2px; color: var(--text-strong); }
  .header .meta { color: var(--muted); font-size: 13px; margin-top: 2px; }
  .header .actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }

  /* Buttons */
  .btn {
    padding: 8px 16px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
    transition: all 0.15s ease;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    user-select: none;
  }
  .btn:hover { border-color: var(--blue); color: #fff; background: var(--surface-hover); }
  .btn.primary { background: var(--blue); border-color: var(--blue); color: #fff; font-weight: 600; box-shadow: 0 2px 8px rgba(59, 130, 246, 0.3); }
  .btn.primary:hover { background: #2563eb; border-color: #2563eb; box-shadow: 0 4px 14px rgba(59, 130, 246, 0.45); }
  .btn.secondary { background: var(--surface-subtle); border-color: var(--border); }
  .btn.secondary:hover { border-color: var(--blue); color: #60a5fa; }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }

  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }

  /* Tab Navigation */
  .tab-nav {
    display: flex;
    gap: 10px;
    margin-bottom: 24px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 2px;
  }
  .tab-btn {
    border: 0;
    border-bottom: 2px solid transparent;
    background: transparent;
    color: var(--muted);
    padding: 12px 18px;
    cursor: pointer;
    font-size: 14px;
    font-weight: 600;
    transition: all 0.2s ease;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    border-radius: 6px 6px 0 0;
  }
  .tab-btn:hover { color: var(--text); background: rgba(255, 255, 255, 0.03); }
  .tab-btn.active { color: #60a5fa; border-bottom-color: var(--blue); background: rgba(59, 130, 246, 0.06); }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; animation: fadeIn 0.2s ease; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }

  /* Cards */
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 24px; }
  @media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.18);
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
    display: flex;
    flex-direction: column;
  }
  .card:hover { border-color: var(--border-light); }

  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;
    padding-bottom: 14px;
    border-bottom: 1px solid var(--border);
  }
  .card-header-left {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .card-icon-badge {
    width: 38px;
    height: 38px;
    border-radius: 10px;
    background: var(--blue-bg);
    color: #60a5fa;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    border: 1px solid rgba(59, 130, 246, 0.2);
  }
  .card-icon-badge.green { background: var(--green-bg); color: #4ade80; border-color: var(--green-border); }
  .card-icon-badge.yellow { background: var(--yellow-bg); color: #facc15; border-color: rgba(234, 179, 8, 0.28); }
  .card-icon-badge.purple { background: rgba(168, 85, 247, 0.12); color: #c084fc; border-color: rgba(168, 85, 247, 0.25); }

  .card-title-group h2 {
    font-size: 16px;
    font-weight: 700;
    color: var(--text-strong);
    letter-spacing: -0.2px;
    margin: 0;
  }
  .card-title-group p {
    font-size: 12px;
    color: var(--muted);
    margin-top: 3px;
  }

  /* Form Elements & Inputs */
  .config-form {
    display: flex;
    flex-direction: column;
    gap: 18px;
    flex: 1;
  }
  .form-row {
    display: grid;
    grid-template-columns: 140px 1fr;
    gap: 16px;
    align-items: flex-start;
  }
  @media (max-width: 600px) {
    .form-row { grid-template-columns: 1fr; gap: 8px; }
  }
  .form-label {
    font-size: 13px;
    font-weight: 600;
    color: #cbd5e1;
    margin-top: 7px;
    line-height: 1.4;
  }
  .form-label-desc {
    display: block;
    font-size: 11px;
    font-weight: 400;
    color: var(--muted);
    margin-top: 2px;
  }

  .form-control {
    width: 100%;
    max-width: 380px;
    color: var(--text);
    background: var(--surface-subtle);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 13px;
    font-family: inherit;
    transition: all 0.2s ease;
  }
  .form-control:focus {
    border-color: var(--blue);
    outline: none;
    box-shadow: 0 0 0 3px var(--blue-glow);
    background: var(--bg);
  }
  .form-control:disabled {
    opacity: 0.65;
    cursor: not-allowed;
    background: rgba(13, 17, 23, 0.6);
  }

  .form-control-static {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 9px 14px;
    border-radius: 8px;
    background: var(--surface-subtle);
    border: 1px solid var(--border);
    font-size: 13px;
    color: var(--text);
  }

  .field-hint {
    font-size: 12px;
    color: var(--muted);
    margin-top: 6px;
    line-height: 1.5;
  }
  .field-hint code {
    font-family: var(--font-mono);
    font-size: 11px;
    padding: 2px 5px;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.06);
    color: #93c5fd;
  }

  /* Toggle Switch */
  .switch-control {
    display: inline-flex;
    align-items: center;
    gap: 12px;
    cursor: pointer;
    user-select: none;
    padding: 4px 0;
  }
  .switch-control input {
    position: absolute;
    opacity: 0;
    width: 0;
    height: 0;
  }
  .switch-slider {
    position: relative;
    width: 44px;
    height: 24px;
    background: #202636;
    border: 1px solid #363f54;
    border-radius: 24px;
    transition: all 0.25s ease;
    flex-shrink: 0;
  }
  .switch-slider:before {
    position: absolute;
    content: "";
    height: 18px;
    width: 18px;
    left: 2px;
    bottom: 2px;
    background: #94a3b8;
    border-radius: 50%;
    transition: all 0.25s ease;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.4);
  }
  .switch-control input:checked + .switch-slider {
    background: #2563eb;
    border-color: #3b82f6;
    box-shadow: 0 0 10px rgba(59, 130, 246, 0.35);
  }
  .switch-control input:checked + .switch-slider:before {
    transform: translateX(20px);
    background: #ffffff;
  }
  .switch-text {
    font-size: 13px;
    font-weight: 600;
    color: var(--text);
  }

  /* Weekday Pill Buttons */
  .weekday-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .weekday-pill {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 7px 15px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 500;
    background: var(--surface-subtle);
    border: 1px solid var(--border);
    color: var(--muted);
    cursor: pointer;
    transition: all 0.2s ease;
    user-select: none;
  }
  .weekday-pill input {
    position: absolute;
    opacity: 0;
    width: 0;
    height: 0;
  }
  .weekday-pill:hover {
    border-color: #4b5563;
    color: var(--text);
  }
  .weekday-pill.active {
    background: rgba(59, 130, 246, 0.16);
    border-color: #3b82f6;
    color: #60a5fa;
    font-weight: 600;
    box-shadow: 0 0 10px rgba(59, 130, 246, 0.2);
  }
  .weekday-quick {
    display: flex;
    gap: 8px;
    margin-top: 8px;
  }
  .btn-xs {
    padding: 4px 10px;
    font-size: 11px;
    border-radius: 5px;
    border: 1px dashed var(--border);
    background: transparent;
    color: var(--muted);
    cursor: pointer;
    transition: all 0.15s;
  }
  .btn-xs:hover {
    border-color: var(--blue);
    color: #60a5fa;
    background: rgba(59, 130, 246, 0.08);
  }

  /* Secret Input with SVG Eye Toggle */
  .secret-input-wrap {
    display: flex;
    width: 100%;
    max-width: 380px;
  }
  .secret-input-wrap .form-control {
    border-radius: 8px 0 0 8px;
    font-family: var(--font-mono);
    letter-spacing: 0.5px;
    font-size: 13px;
    max-width: none;
  }
  .secret-toggle {
    width: 44px;
    border: 1px solid var(--border);
    border-left: 0;
    border-radius: 0 8px 8px 0;
    background: var(--surface-subtle);
    color: var(--muted);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.2s;
  }
  .secret-toggle:hover {
    color: var(--text);
    border-color: var(--blue);
    background: rgba(59, 130, 246, 0.08);
  }

  /* Time Picker Wrap */
  .time-picker-wrap {
    display: inline-flex;
    align-items: center;
    gap: 10px;
  }
  .time-input {
    font-family: var(--font-mono);
    font-size: 15px;
    font-weight: 700;
    letter-spacing: 1.5px;
    width: 140px;
    text-align: center;
    padding: 8px 12px;
  }
  .input-tag {
    font-size: 11px;
    padding: 4px 8px;
    border-radius: 5px;
    background: rgba(234, 179, 8, 0.12);
    color: #facc15;
    border: 1px solid rgba(234, 179, 8, 0.25);
  }

  /* Input with prefix */
  .input-with-prefix {
    position: relative;
    width: 100%;
    max-width: 380px;
  }
  .input-prefix {
    position: absolute;
    left: 12px;
    top: 50%;
    transform: translateY(-50%);
    color: var(--muted);
    font-family: var(--font-mono);
    font-weight: 700;
    pointer-events: none;
  }
  .input-with-prefix .form-control {
    padding-left: 32px;
    font-family: var(--font-mono);
    max-width: none;
  }

  /* Event Check Cards (Telegram Events) */
  .event-cards-group {
    display: flex;
    flex-direction: column;
    gap: 10px;
    max-width: 520px;
  }
  .event-card {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 12px 14px;
    background: var(--surface-subtle);
    border: 1px solid var(--border);
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.2s;
    user-select: none;
  }
  .event-card:hover {
    border-color: #3b4258;
    background: rgba(30, 36, 51, 0.6);
  }
  .event-card input {
    margin-top: 3px;
    width: 16px;
    height: 16px;
    accent-color: #3b82f6;
    cursor: pointer;
  }
  .event-info { flex: 1; }
  .event-title { font-size: 13px; font-weight: 600; color: var(--text-strong); }
  .event-desc { font-size: 12px; color: var(--muted); margin-top: 2px; line-height: 1.4; }

  /* Dual Clocks & Status Banner */
  .status-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    background: var(--surface-subtle);
    border: 1px solid var(--border);
    border-radius: 10px;
    margin-bottom: 18px;
  }
  .status-item {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 13px;
    font-weight: 600;
  }
  .status-sub {
    font-size: 12px;
    color: var(--muted);
    font-family: var(--font-mono);
  }
  .pulse-dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    display: inline-block;
  }
  .pulse-dot.green {
    background: #22c55e;
    box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.7);
    animation: pulseGreen 2s infinite;
  }
  .pulse-dot.yellow {
    background: #eab308;
    box-shadow: 0 0 0 0 rgba(234, 179, 8, 0.7);
    animation: pulseYellow 2s infinite;
  }
  .pulse-dot.red { background: #ef4444; }
  @keyframes pulseGreen {
    0% { box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.7); }
    70% { box-shadow: 0 0 0 6px rgba(34, 197, 94, 0); }
    100% { box-shadow: 0 0 0 0 rgba(34, 197, 94, 0); }
  }
  @keyframes pulseYellow {
    0% { box-shadow: 0 0 0 0 rgba(234, 179, 8, 0.7); }
    70% { box-shadow: 0 0 0 6px rgba(234, 179, 8, 0); }
    100% { box-shadow: 0 0 0 0 rgba(234, 179, 8, 0); }
  }

  .dual-clocks {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
    margin-bottom: 18px;
  }
  @media (max-width: 600px) { .dual-clocks { grid-template-columns: 1fr; } }

  .clock-card {
    background: var(--surface-subtle);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .clock-card.highlight {
    border-color: rgba(59, 130, 246, 0.35);
    background: linear-gradient(135deg, rgba(59, 130, 246, 0.08) 0%, var(--surface-subtle) 100%);
  }
  .clock-label {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 12px;
    font-weight: 600;
    color: var(--muted-light);
  }
  .clock-value {
    font-size: 15px;
    font-weight: 700;
    color: var(--text-strong);
    letter-spacing: 0.2px;
  }
  .clock-desc {
    font-size: 11px;
    color: var(--muted);
    line-height: 1.4;
  }

  /* Terminal Box */
  .terminal-card {
    background: #090b10;
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    margin-top: 14px;
  }
  .terminal-header {
    background: #141822;
    padding: 7px 12px;
    display: flex;
    align-items: center;
    gap: 6px;
    border-bottom: 1px solid var(--border);
  }
  .terminal-dots { display: flex; gap: 5px; }
  .terminal-dots span {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #373b4d;
  }
  .terminal-dots span:nth-child(1) { background: #ef4444; }
  .terminal-dots span:nth-child(2) { background: #eab308; }
  .terminal-dots span:nth-child(3) { background: #22c55e; }
  .terminal-label {
    font-size: 11px;
    font-family: var(--font-mono);
    color: var(--muted);
    margin-left: 6px;
  }
  .terminal-body {
    padding: 10px 14px;
    font-size: 12px;
    color: #93c5fd;
    overflow-wrap: anywhere;
    line-height: 1.6;
  }

  /* Card Footer Actions */
  .card-footer {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-top: auto;
    padding-top: 18px;
    border-top: 1px solid var(--border);
    flex-wrap: wrap;
  }
  .status-msg {
    font-size: 12px;
    padding: 5px 12px;
    border-radius: 6px;
    display: none;
    align-items: center;
    gap: 6px;
    transition: all 0.2s;
  }
  .status-msg.success {
    display: inline-flex;
    background: var(--green-bg);
    color: #4ade80;
    border: 1px solid var(--green-border);
  }
  .status-msg.error {
    display: inline-flex;
    background: var(--red-bg);
    color: #fca5a5;
    border: 1px solid rgba(239, 68, 68, 0.28);
  }
  .status-msg.loading {
    display: inline-flex;
    background: var(--blue-bg);
    color: #93c5fd;
    border: 1px solid rgba(59, 130, 246, 0.28);
  }

  /* Feature Note List */
  .feature-notes {
    display: flex;
    flex-direction: column;
    gap: 12px;
    list-style: none;
  }
  .feature-note-item {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 10px 12px;
    border-radius: 8px;
    background: var(--surface-subtle);
    border: 1px solid var(--border);
    font-size: 13px;
    color: var(--muted-light);
    line-height: 1.5;
  }
  .feature-note-icon {
    font-size: 15px;
    flex-shrink: 0;
    margin-top: 1px;
  }
  .feature-note-item strong { color: var(--text-strong); }
  .feature-note-item code {
    font-family: var(--font-mono);
    font-size: 11px;
    padding: 2px 5px;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.06);
    color: #93c5fd;
  }

  /* Badges */
  .badge { display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; line-height: 1.2; }
  .badge.good, .badge.green { background: var(--green-bg); color: var(--green); border: 1px solid var(--green-border); }
  .badge.extended, .badge.yellow { background: var(--yellow-bg); color: var(--yellow); border: 1px solid rgba(234, 179, 8, 0.28); }
  .badge.poor, .badge.red { background: var(--red-bg); color: var(--red); border: 1px solid rgba(239, 68, 68, 0.28); }
  .badge.blue { background: var(--blue-bg); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.28); }
  .badge.critical, .badge.high { background: rgba(239, 68, 68, 0.22); color: var(--red); }
  .badge.medium { background: var(--yellow-bg); color: var(--yellow); }

  /* Stats Row & Old Cards */
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
  .stat-card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
  .stat-card .value { font-size: 28px; font-weight: 700; }
  .stat-card .sub { color: var(--muted); font-size: 12px; margin-top: 4px; }
  .stat-card.green .value { color: var(--green); }
  .stat-card.red .value { color: var(--red); }
  .stat-card.blue .value { color: var(--blue); }
  .stat-card.yellow .value { color: var(--yellow); }

  .regime-badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; }
  .regime-badge.risk-on { background: var(--green-bg); color: var(--green); }
  .regime-badge.risk-off { background: var(--red-bg); color: var(--red); }
  .regime-badge.transitional { background: var(--yellow-bg); color: var(--yellow); }

  .chart-wrap { position: relative; height: 250px; }

  .signal-table { width: 100%; border-collapse: collapse; }
  .signal-table th { text-align: left; padding: 10px 12px; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid var(--border); }
  .signal-table td { padding: 12px; border-bottom: 1px solid var(--border); font-size: 13px; vertical-align: top; }
  .signal-table tr:hover { background: rgba(59, 130, 246, 0.04); }

  .ticker { font-weight: 700; font-size: 14px; color: var(--blue); }
  .score-bar { height: 6px; border-radius: 3px; background: var(--border); overflow: hidden; width: 80px; display: inline-block; vertical-align: middle; margin-left: 6px; }
  .score-bar .fill { height: 100%; border-radius: 3px; }
  .score-num { font-weight: 600; font-size: 13px; }

  .reasons-list { list-style: none; padding: 0; margin-top: 4px; }
  .reasons-list li { font-size: 11px; color: var(--muted); padding: 1px 0; }
  .expand-btn { background: none; border: none; color: var(--blue); cursor: pointer; font-size: 11px; padding: 2px 0; }
  .expand-btn:hover { text-decoration: underline; }

  .no-data { text-align: center; padding: 60px 20px; color: var(--muted); }
  .no-data h2 { font-size: 18px; margin-bottom: 8px; color: var(--text); }

  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--border); border-top-color: var(--blue); border-radius: 50%; animation: spin 0.6s linear infinite; margin-right: 6px; vertical-align: middle; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .toast { position: fixed; bottom: 24px; right: 24px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px 20px; font-size: 13px; box-shadow: 0 4px 20px rgba(0,0,0,0.4); z-index: 100; transition: opacity 0.3s; }
  .scan-status { color: var(--muted); font-size: 12px; align-self: center; white-space: nowrap; }
  .scan-select { background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 6px 10px; border-radius: 6px; font-size: 13px; }
  .section-title { font-size: 18px; margin: 30px 0 14px; }
  .info-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
  .info-list { padding-left: 20px; color: var(--muted); font-size: 13px; line-height: 1.75; }
  .info-list strong { color: var(--text); }
  .param-table { width: 100%; border-collapse: collapse; }
  .param-table th, .param-table td { text-align: left; padding: 10px; border-bottom: 1px solid var(--border); font-size: 12px; vertical-align: top; }
  .param-table th { color: var(--muted); }
  .code-block { background: var(--bg); border: 1px solid var(--border); border-radius: 7px; padding: 10px 12px; color: #cbd5e1; font: 12px var(--font-mono); overflow-wrap: anywhere; margin-top: 8px; }
  details.rule-group { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 12px; }
  details.rule-group summary { cursor: pointer; padding: 16px 20px; font-size: 14px; font-weight: 600; }
  details.rule-group .detail-content { padding: 0 20px 20px; }
  .notice { padding: 12px 14px; border-left: 3px solid var(--yellow); background: rgba(234,179,8,.08); color: var(--muted); border-radius: 4px; font-size: 12px; line-height: 1.7; }

  @media (max-width: 760px) {
    .header { padding: 14px 16px; align-items: flex-start; gap: 12px; flex-direction: column; }
    .container { padding: 16px; }
  }
</style>
</head>
<body>

<div class="header">
  <div style="display:flex;align-items:center;gap:12px">
    <div style="width:36px;height:36px;display:flex;align-items:center;justify-content:center;flex-shrink:0">{{ app_icon | safe }}</div>
    <div>
      <h1>强势突破股票筛选器</h1>
      <div class="meta" id="headerMeta">正在加载...</div>
    </div>
  </div>
  <div class="actions">
    <span class="scan-status" id="scanStatus">空闲</span>
    <select class="scan-select" id="scanSelect" onchange="loadScan(this.value)" aria-label="历史全量扫描记录"></select>
    <button class="btn" onclick="runScan(false)" id="testBtn">测试扫描（100只）</button>
    <button class="btn primary" onclick="runScan(true)" id="fullBtn">全量扫描</button>
    <form method="post" action="{{ url_for('logout') }}" style="margin:0">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <button class="btn" type="submit">退出</button>
    </form>
  </div>
</div>

<div class="container" id="content">
  <div class="tab-nav">
    <button class="tab-btn active" data-tab="dashboardTab" onclick="showTab('dashboardTab', this)">扫描结果</button>
    <button class="tab-btn" data-tab="scheduleTab" onclick="showTab('scheduleTab', this)">定时任务与 TG 推送</button>
    <button class="tab-btn" data-tab="rulesTab" onclick="showTab('rulesTab', this)">选股规则</button>
  </div>

  <section class="tab-panel active" id="dashboardTab">
    <div class="no-data" id="noData" style="display:none">
      <h2>暂无扫描数据</h2>
      <p>请先运行一次全量扫描</p>
    </div>
    <div id="dashboard" style="display:none">

    <div class="stats-row" id="statsRow"></div>

    <div class="grid-2">
      <div class="card">
        <h2>市场宽度</h2>
        <div class="chart-wrap"><canvas id="breadthChart"></canvas></div>
      </div>
      <div class="card">
        <h2>SPY 市场环境</h2>
        <div id="spyInfo"></div>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <h2 style="color:var(--green)">买入信号</h2>
      <div style="overflow-x:auto">
        <table class="signal-table" id="buyTable">
          <thead>
            <tr>
              <th>#</th>
              <th>代码</th>
              <th>评分</th>
              <th>入场质量</th>
              <th>参考止损</th>
              <th>收益风险比</th>
              <th>相对强度</th>
              <th>主要理由</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <h2 style="color:var(--red)">卖出信号</h2>
      <div style="overflow-x:auto">
        <table class="signal-table" id="sellTable">
          <thead>
            <tr>
              <th>#</th>
              <th>代码</th>
              <th>评分</th>
              <th>严重程度</th>
              <th>跌破位置</th>
              <th>主要理由</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </div>

    </div>
  </section>

  <section class="tab-panel" id="scheduleTab">
    <div id="scheduleInfo"><div class="card">正在加载定时任务配置...</div></div>
  </section>

  <section class="tab-panel" id="rulesTab">
    <div id="rulesInfo"><div class="card">正在加载选股规则...</div></div>
  </section>
</div>

<script>
let breadthChartInstance = null;
let previousScanStatus = null;
let systemInfoData = null;
const csrfToken = {{ csrf_token | tojson }};

const phaseNames = {1: 'Phase 1（筑底/盘整）', 2: 'Phase 2（上升趋势）', 3: 'Phase 3（派发/见顶）', 4: 'Phase 4（下降趋势）'};
const trendNames = {Bullish: '上涨', Bearish: '下跌', Consolidating: '盘整', Topping: '见顶', Unknown: '未知'};
const entryNames = {Good: '良好', Extended: '偏离', Poor: '较差'};
const severityNames = {critical: '严重', high: '高', medium: '中等', low: '低'};

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
}

function regimeName(value) {
  if ((value || '').includes('RISK-ON (Strong)')) return '风险偏好（强）';
  if ((value || '').includes('RISK-ON (Moderate)')) return '风险偏好（中等）';
  if ((value || '').includes('RISK-ON')) return '风险偏好（偏弱/混合）';
  if ((value || '').includes('RISK-OFF')) return '风险规避';
  return '过渡/不确定';
}

function translateReason(text) {
  let value = cleanEmoji(text);
  const replacements = [
    [/Strong Stage 2/g, '强势第二阶段'], [/Good Stage 2/g, '良好第二阶段'], [/Weak Stage 2/g, '偏弱第二阶段'],
    [/Very weak Stage 2/g, '很弱的第二阶段'], [/SMAs rising strongly/g, '均线强势向上'], [/SMAs rising moderately/g, '均线温和向上'],
    [/SMAs rising weakly/g, '均线微弱向上'], [/SMAs flat or declining/g, '均线走平或下降'], [/Over-extended/g, '过度偏离均线'],
    [/Moderately extended above 50 SMA/g, '相对 50 日均线偏离较大'], [/Revenue/g, '营收'], [/EPS/g, '每股收益'],
    [/Inventory drawing/g, '库存下降'], [/Inventory neutral/g, '库存稳定'], [/Inventory building rapidly/g, '库存快速增加'],
    [/Inventory building/g, '库存增加'], [/Volume heavier on up days/g, '上涨日成交量更大'], [/Volume slightly heavier on up days/g, '上涨日成交量略大'],
    [/Volume pattern neutral/g, '成交量结构中性'], [/Volume heavier on down days/g, '下跌日成交量更大'], [/Strong RS/g, '相对强度强'],
    [/Positive RS/g, '相对强度为正'], [/Neutral RS/g, '相对强度中性'], [/Weak RS rollover/g, '相对强度转弱'], [/Weak RS/g, '相对强度弱'],
    [/Declining RS/g, '相对强度下降'], [/Outstanding R\\/R/g, '收益风险比极佳'], [/Excellent R\\/R/g, '收益风险比优秀'],
    [/Good R\\/R/g, '收益风险比良好'], [/Acceptable R\\/R/g, '收益风险比可接受'], [/Poor R\\/R/g, '收益风险比较差'],
    [/Good entry zone/g, '处于良好入场区域'], [/Approaching entry zone/g, '接近理想入场区域'], [/Outside ideal entry zone/g, '不在理想入场区域'],
    [/In Phase 3 \\(Distribution\\)/g, '处于 Phase 3（派发/见顶）'], [/In Phase 4 \\(Downtrend\\)/g, '处于 Phase 4（下降趋势）'],
    [/Broke below 50 SMA by/g, '跌破 50 日均线'], [/50 SMA declining/g, '50 日均线下降'], [/High volume breakdown/g, '放量跌破'],
    [/Elevated volume/g, '成交量放大'], [/Moderate volume/g, '成交量中等'], [/Low volume breakdown/g, '缩量跌破'],
    [/RS still positive/g, '相对强度仍为正'], [/No fundamental data available/g, '没有可用基本面数据'],
    [/outperforming SPY/g, '跑赢 SPY'], [/underperforming SPY/g, '跑输 SPY'], [/volume confirmed/g, '成交量确认'], [/low volume/g, '成交量不足']
  ];
  replacements.forEach(([pattern, replacement]) => { value = value.replace(pattern, replacement); });
  return value;
}

async function loadSystemInfo() {
  const res = await fetch('api/system-info');
  systemInfoData = await res.json();
  renderSystemInfo(systemInfoData);
}

function rows(items, points = false) {
  return items.map(item => `<tr><td><strong>${item.name}</strong></td>${points ? `<td>${item.points}</td>` : `<td>${item.value || '-'}</td>`}<td>${item.meaning}</td></tr>`).join('');
}

function setSaveStatus(el, type, msg) {
  if (!el) return;
  el.className = 'status-msg ' + type;
  el.textContent = msg;
  el.style.opacity = '1';
  if (type === 'success') {
    setTimeout(() => { if (el && el.classList.contains('success')) el.style.opacity = '0'; }, 4000);
  }
}

function setWeekdays(days) {
  document.querySelectorAll('input[name="weekday"]').forEach(input => {
    const isChecked = days.includes(Number(input.value));
    input.checked = isChecked;
    const pill = input.closest('.weekday-pill');
    if (pill) pill.classList.toggle('active', isChecked);
  });
}

function updateSwitchLabel(checkbox, textId, onText, offText) {
  const el = document.getElementById(textId);
  if (el) el.textContent = checkbox.checked ? onText : offText;
}

const EYE_OPEN_SVG = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
const EYE_OFF_SVG = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;

function toggleTelegramToken() {
  const input = document.getElementById('telegramBotToken');
  const button = document.getElementById('telegramTokenToggle');
  const revealing = input.type === 'password';
  input.type = revealing ? 'text' : 'password';
  button.innerHTML = revealing ? EYE_OFF_SVG : EYE_OPEN_SVG;
  button.setAttribute('aria-label', revealing ? '隐藏 Bot Token' : '显示 Bot Token');
  button.title = revealing ? '隐藏 Bot Token' : '显示 Bot Token';
}

function renderSystemInfo(info) {
  const schedule = info.schedule;
  const scan = info.scan;
  const telegram = info.telegram;
  const runtime = info.runtime;
  const weekdayNames = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
  const timeValue = `${String(schedule.hour).padStart(2, '0')}:${String(schedule.minute).padStart(2, '0')}`;
  const nextNewYork = schedule.next_run && schedule.next_run.new_york ? new Date(schedule.next_run.new_york).toLocaleString('zh-CN', {timeZone: 'America/New_York', hour12: false}) : '已暂停或暂无计划';
  const nextBeijing = schedule.next_run && schedule.next_run.beijing ? new Date(schedule.next_run.beijing).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) : '已暂停或暂无计划';
  const schedulerState = schedule.state || {};
  const schedulerStatusNames = {running: '运行中', completed: '已完成', failed: '执行失败'};

  const isTgReady = telegram.enabled && telegram.bot_token_configured && telegram.chat_id;

  document.getElementById('scheduleInfo').innerHTML = `
    <!-- Top Row: Schedule Config & Next Run -->
    <div class="grid-2">
      <!-- Schedule Config Card -->
      <div class="card">
        <div class="card-header">
          <div class="card-header-left">
            <div class="card-icon-badge">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            </div>
            <div class="card-title-group">
              <h2>定时任务配置</h2>
              <p>自动化收盘后全市场筛选与阶段研判计划</p>
            </div>
          </div>
          <span class="badge ${schedule.enabled ? 'green' : 'yellow'}">${schedule.enabled ? '自动扫描生效中' : '自动扫描已暂停'}</span>
        </div>

        <div class="config-form">
          <div class="form-row">
            <div class="form-label">
              任务开关
              <span class="form-label-desc">定时自动化执行</span>
            </div>
            <div>
              <label class="switch-control">
                <input type="checkbox" id="scheduleEnabled" ${schedule.enabled ? 'checked' : ''} onchange="updateSwitchLabel(this, 'scheduleEnabledText', '定时任务已启用', '定时任务已暂停')">
                <span class="switch-slider"></span>
                <span class="switch-text" id="scheduleEnabledText">${schedule.enabled ? '定时任务已启用' : '定时任务已暂停'}</span>
              </label>
            </div>
          </div>

          <div class="form-row">
            <div class="form-label">
              基准时区
              <span class="form-label-desc">美股数据归档地</span>
            </div>
            <div>
              <div class="form-control-static">
                <span>🇺🇸</span>
                <strong>America/New_York</strong>
                <span style="color:var(--muted);font-size:12px">（美东时间，以收盘数据为准）</span>
              </div>
            </div>
          </div>

          <div class="form-row">
            <div class="form-label">
              美东执行时间
              <span class="form-label-desc">盘后触发时点</span>
            </div>
            <div>
              <div class="time-picker-wrap">
                <input class="form-control time-input" type="time" id="scheduleTime" value="${timeValue}">
                <span class="input-tag">盘后推荐 18:00–19:00</span>
              </div>
              <div class="field-hint">美股于美东 16:00 收盘，建议设置在 18:15 之后以等待上游复权与财报结算完毕。</div>
            </div>
          </div>

          <div class="form-row">
            <div class="form-label">
              执行星期
              <span class="form-label-desc">交易日循环</span>
            </div>
            <div>
              <div class="weekday-pills">
                ${weekdayNames.map((name, index) => `
                  <label class="weekday-pill ${schedule.weekdays.includes(index) ? 'active' : ''}">
                    <input type="checkbox" name="weekday" value="${index}" ${schedule.weekdays.includes(index) ? 'checked' : ''} onchange="this.parentElement.classList.toggle('active', this.checked)">
                    <span>${name}</span>
                  </label>
                `).join('')}
              </div>
              <div class="weekday-quick">
                <button type="button" class="btn-xs" onclick="setWeekdays([0,1,2,3,4])">常用：工作日（周一至周五）</button>
                <button type="button" class="btn-xs" onclick="setWeekdays([0,1,2,3,4,5,6])">全选（每天）</button>
                <button type="button" class="btn-xs" onclick="setWeekdays([])">清空</button>
              </div>
            </div>
          </div>
        </div>

        <div class="card-footer">
          <button class="btn primary" onclick="saveSchedule()">保存定时任务</button>
          <span id="scheduleSaveStatus" class="status-msg"></span>
        </div>
      </div>

      <!-- Next Run Status Card -->
      <div class="card">
        <div class="card-header">
          <div class="card-header-left">
            <div class="card-icon-badge purple">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
            </div>
            <div class="card-title-group">
              <h2>下次运行与调度看板</h2>
              <p>双时区联动与调度器常驻状态</p>
            </div>
          </div>
        </div>

        <div class="status-banner">
          <div class="status-item">
            <span class="pulse-dot ${schedulerState.status === 'running' ? 'yellow' : schedulerState.status === 'failed' ? 'red' : 'green'}"></span>
            <span>调度服务状态：<strong>${schedulerStatusNames[schedulerState.status] || (schedule.enabled ? '正常就绪中' : '定时已暂停')}</strong></span>
          </div>
          <span class="status-sub">${schedulerState.last_run ? '上次：' + schedulerState.last_run : '无缝守护中'}</span>
        </div>

        <div class="dual-clocks">
          <div class="clock-card">
            <div class="clock-label">
              <span>🇺🇸 美东时间 (New York)</span>
              <span class="badge blue">美东基准</span>
            </div>
            <div class="clock-value font-mono">${nextNewYork}</div>
            <div class="clock-desc">服务器以美东为准，夏冬令时全自动换算</div>
          </div>

          <div class="clock-card highlight">
            <div class="clock-label">
              <span>🇨🇳 北京时间 (Beijing)</span>
              <span class="badge green">对应本地</span>
            </div>
            <div class="clock-value font-mono">${nextBeijing}</div>
            <div class="clock-desc">${schedule.beijing_time || '夏令时次日 06:15 ；冬令时次日 07:15'}</div>
          </div>
        </div>

        <div class="notice" style="margin-top:2px;font-size:12px;line-height:1.6">
          <strong>时差说明：</strong>无论美国切换冬令时还是夏令时，定时任务始终精准锁定在美股收盘后的同一美东时刻，无需手动重新计算时差。
        </div>

        <div class="terminal-card">
          <div class="terminal-header">
            <div class="terminal-dots"><span></span><span></span><span></span></div>
            <span class="terminal-label">cron background execution runner</span>
          </div>
          <div class="terminal-body font-mono">${schedule.command}</div>
        </div>
      </div>
    </div>

    <!-- Middle Row: Telegram Config & Push Tips -->
    <div class="grid-2">
      <!-- Telegram Config Card -->
      <div class="card">
        <div class="card-header">
          <div class="card-header-left">
            <div class="card-icon-badge" style="background:rgba(14,165,233,0.12);color:#38bdf8;border-color:rgba(14,165,233,0.25)">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
            </div>
            <div class="card-title-group">
              <h2>Telegram 推送配置</h2>
              <p>扫描完成即刻推送突破选股信号到 Telegram</p>
            </div>
          </div>
          <span class="badge ${isTgReady ? 'green' : 'yellow'}">${isTgReady ? '已就绪' : '待完善'}</span>
        </div>

        <div class="config-form">
          <div class="form-row">
            <div class="form-label">
              推送总开关
              <span class="form-label-desc">TG 机器人总控</span>
            </div>
            <div>
              <label class="switch-control">
                <input type="checkbox" id="telegramEnabled" ${telegram.enabled ? 'checked' : ''} onchange="updateSwitchLabel(this, 'telegramEnabledText', 'TG 推送已开启', 'TG 推送已暂停')">
                <span class="switch-slider"></span>
                <span class="switch-text" id="telegramEnabledText">${telegram.enabled ? 'TG 推送已开启' : 'TG 推送已暂停'}</span>
              </label>
            </div>
          </div>

          <div class="form-row">
            <div class="form-label">
              Bot Token
              <span class="form-label-desc">机器人访问密钥</span>
            </div>
            <div>
              <div class="secret-input-wrap">
                <input class="form-control" id="telegramBotToken" type="password" autocomplete="new-password" value="${escapeHtml(telegram.bot_token || '')}" placeholder="格式如 123456789:ABCdef...">
                <button class="secret-toggle" id="telegramTokenToggle" type="button" onclick="toggleTelegramToken()" aria-label="显示 Bot Token" title="显示 Bot Token">
                  ${EYE_OPEN_SVG}
                </button>
              </div>
              <div class="field-hint">从 Telegram <a href="https://t.me/BotFather" target="_blank" rel="noopener" style="color:#60a5fa;text-decoration:none">@BotFather</a> 创建获取；前端已进行安全加密隐藏。</div>
            </div>
          </div>

          <div class="form-row">
            <div class="form-label">
              Chat ID
              <span class="form-label-desc">接收目标会话</span>
            </div>
            <div>
              <div class="input-with-prefix">
                <span class="input-prefix">#</span>
                <input class="form-control" id="telegramChatId" type="text" value="${escapeHtml(telegram.chat_id)}" placeholder="例如 123456789 或群组 -1001234567890">
              </div>
              <div class="field-hint">接收通知的个人 Chat ID 或群组 ID（群组通常以 <code>-100</code> 开头）。</div>
            </div>
          </div>

          <div class="form-row">
            <div class="form-label">
              通知触发场景
              <span class="form-label-desc">细分触发时机</span>
            </div>
            <div>
              <div class="event-cards-group">
                <label class="event-card">
                  <input type="checkbox" id="telegramNotifyFull" ${telegram.notify_full ? 'checked' : ''}>
                  <div class="event-info">
                    <div class="event-title">正式扫描 / 每日定时任务推送</div>
                    <div class="event-desc">每晚收盘全量扫描完成后，推送市场环境、SPY 阶段与 Top N 突破买入/卖出信号。</div>
                  </div>
                </label>
                <label class="event-card">
                  <input type="checkbox" id="telegramNotifyTest" ${telegram.notify_test ? 'checked' : ''}>
                  <div class="event-info">
                    <div class="event-title">测试扫描推送（100 只样本）</div>
                    <div class="event-desc">手动启动 100 只样本测试扫描时也推送消息（调试时使用，默认建议关闭）。</div>
                  </div>
                </label>
              </div>
            </div>
          </div>
        </div>

        <div class="card-footer">
          <button class="btn primary" onclick="saveTelegramSettings(false)">保存 TG 配置</button>
          <button class="btn secondary" onclick="saveTelegramSettings(true)">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:middle;margin-right:2px"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
            保存并测试发送
          </button>
          <span id="telegramSaveStatus" class="status-msg"></span>
        </div>
      </div>

      <!-- Telegram Notes Card -->
      <div class="card">
        <div class="card-header">
          <div class="card-header-left">
            <div class="card-icon-badge green">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
            </div>
            <div class="card-title-group">
              <h2>推送说明与最佳实践</h2>
              <p>运行逻辑与参数配置规范</p>
            </div>
          </div>
        </div>

        <ul class="feature-notes">
          <li class="feature-note-item">
            <span class="feature-note-icon">📢</span>
            <div>
              <strong>全量与定时任务统一</strong>：正式全量扫描和收盘后的定时扫描均共用“正式/定时扫描”开关与同一份优质突破榜单模板。
            </div>
          </li>
          <li class="feature-note-item">
            <span class="feature-note-icon">🛡️</span>
            <div>
              <strong>测试扫描防骚扰</strong>：测试扫描（100只）默认不推送 Telegram，避免调试时频繁打扰频道成员。
            </div>
          </li>
          <li class="feature-note-item">
            <span class="feature-note-icon">⚡</span>
            <div>
              <strong>持久化动态热生效</strong>：配置保存在 <code>data/config/telegram.json</code>，保存后无需重启服务容器即可立即生效。
            </div>
          </li>
          <li class="feature-note-item">
            <span class="feature-note-icon">🔒</span>
            <div>
              <strong>敏感凭据加密保护</strong>：Bot Token 仅限在当前登录会话内读取，未授权请求严禁访问，离手后可再次点击眼睛隐藏。
            </div>
          </li>
          <li class="feature-note-item">
            <span class="feature-note-icon">🤖</span>
            <div>
              <strong>内部 API 静音执行</strong>：外部或自动化集成调用任务受统一策略保护，默认不向 TG 推送多余噪音。
            </div>
          </li>
        </ul>

        <div class="notice" style="margin-top:auto;font-size:12px;line-height:1.6">
          <strong>测试提示：</strong>点击「保存并测试发送」后，系统会立即向填写的 Chat ID 发送一条联通性测试消息，若遇到报错请检查 Bot 是否已加入群组或私聊中点击了 Start。
        </div>
      </div>
    </div>

    <!-- Bottom Row: Scan Settings & Price Cache -->
    <div class="grid-2">
      <!-- Scan Config Card -->
      <div class="card">
        <div class="card-header">
          <div class="card-header-left">
            <div class="card-icon-badge">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>
            </div>
            <div class="card-title-group">
              <h2>股票池与输出设置</h2>
              <p>全局扫描范围与筛选呈现上限</p>
            </div>
          </div>
        </div>

        <div class="config-form">
          <div class="form-row">
            <div class="form-label">
              股票池范围
              <span class="form-label-desc">初筛标的范围</span>
            </div>
            <div>
              <select class="form-control" id="universeMode">
                ${scan.universe_options.map(option => `<option value="${option.value}" ${option.value === scan.universe_mode ? 'selected' : ''}>${option.label}</option>`).join('')}
              </select>
              <div class="field-hint">默认标普 500 + 纳斯达克 100；全量选股耗时适中且流动性与突破质量极佳。</div>
            </div>
          </div>

          <div class="form-row">
            <div class="form-label">
              每类展示数量
              <span class="form-label-desc">榜单截取上限</span>
            </div>
            <div>
              <input class="form-control" id="resultLimit" type="number" min="5" max="100" step="1" value="${scan.result_limit}">
              <div class="field-hint">Dashboard 与 Telegram 仅展示 Top N 精选信号（5–100），完整排序沉淀于后台审计 JSON。</div>
            </div>
          </div>
        </div>

        <div class="card-footer">
          <button class="btn primary" onclick="saveScanSettings()">保存扫描配置</button>
          <span id="scanSettingsSaveStatus" class="status-msg"></span>
        </div>
      </div>

      <!-- Price Cache Card -->
      <div class="card">
        <div class="card-header">
          <div class="card-header-left">
            <div class="card-icon-badge green">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
            </div>
            <div class="card-title-group">
              <h2>行情与数据缓存</h2>
              <p>增量同步与滚动五年日线机制</p>
            </div>
          </div>
        </div>

        <div class="dual-clocks">
          <div class="clock-card">
            <div class="clock-label">
              <span>增量缓存模式</span>
              <span class="badge ${scan.price_cache_enabled ? 'green' : 'red'}">${scan.price_cache_enabled ? '已启用' : '已停用'}</span>
            </div>
            <div class="clock-value font-mono">滚动五年日线</div>
            <div class="clock-desc">初次建仓拉取完整数据，日常只补单日增量</div>
          </div>

          <div class="clock-card">
            <div class="clock-label">
              <span>完整校准周期</span>
              <span class="badge blue">自动校验</span>
            </div>
            <div class="clock-value font-mono">每 ${scan.price_cache_full_refresh_days} 天</div>
            <div class="clock-desc">自动检测拆股与分红复权，修补异常间断</div>
          </div>
        </div>

        <div class="notice" style="margin-top:auto;font-size:12px;line-height:1.6">
          <strong>缓存保护机制：</strong>首次扫描建立五年数据用于回撤过滤，日常只需增量补最新交易日（耗时显著降低）。切换股票池不会清除已有的历史缓存。
        </div>
      </div>
    </div>

    <!-- System Specs Card -->
    <div class="card">
      <div class="card-header">
        <div class="card-header-left">
          <div class="card-icon-badge purple">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>
          </div>
          <div class="card-title-group">
            <h2>扫描引擎运行规范</h2>
            <p>并发控制与频率限制底层状态</p>
          </div>
        </div>
      </div>

      <div class="info-grid">
        <div class="clock-card">
          <div class="clock-label">股票池覆盖</div>
          <div class="clock-value" style="font-size:13px">${runtime.universe_size}</div>
          <div class="clock-desc">测试样本：${runtime.test_size} 只</div>
        </div>
        <div class="clock-card">
          <div class="clock-label">全量预估耗时</div>
          <div class="clock-value" style="font-size:13px">${runtime.full_scan_time}</div>
          <div class="clock-desc">受全局请求延时保护</div>
        </div>
        <div class="clock-card">
          <div class="clock-label">并发调度控制</div>
          <div class="clock-value font-mono" style="font-size:13px">${runtime.workers} 工作线程</div>
          <div class="clock-desc">请求节奏：${runtime.request_delay}</div>
        </div>
        <div class="clock-card">
          <div class="clock-label">基本面缓存</div>
          <div class="clock-value" style="font-size:13px">${runtime.fundamental_cache}</div>
          <div class="clock-desc">排重锁：${schedule.overlap_protection}</div>
        </div>
      </div>
    </div>`;

  document.getElementById('rulesInfo').innerHTML = `
    <div class="notice" style="margin-bottom:16px"><strong>行情缓存机制：</strong>五年数据用于“最大回撤超过 90%”的历史风险过滤，最近约 252 个交易日用于技术分析。系统首次建立五年缓存，之后只增量补最近交易日，并每 ${scan.price_cache_full_refresh_days} 天完整校验历史；切换股票池不会删除任何已有缓存。</div>
    <details class="rule-group" open>
      <summary>股票池初筛与市场环境门槛</summary>
      <div class="detail-content">
        <table class="param-table"><thead><tr><th>项目</th><th>当前参数</th><th>意义</th></tr></thead><tbody>${rows(info.filters)}</tbody></table>
        <h3 style="font-size:13px;margin:18px 0 8px">买入信号的市场开关</h3>
        <ol class="info-list">${info.market_gate.map(x => `<li>${x}</li>`).join('')}</ol>
      </div>
    </details>
    <details class="rule-group">
      <summary>Phase 四阶段定义</summary>
      <div class="detail-content"><table class="param-table"><thead><tr><th>阶段</th><th>名称</th><th>意义</th></tr></thead><tbody>${info.phases.map(x => `<tr><td>Phase ${x.phase}</td><td>${x.name}</td><td>${x.meaning}</td></tr>`).join('')}</tbody></table></div>
    </details>
    <details class="rule-group">
      <summary>Minervini 趋势模板（必须至少通过 7/8）</summary>
      <div class="detail-content"><ol class="info-list">${info.minervini.map(x => `<li>${x}</li>`).join('')}</ol></div>
    </details>
    <details class="rule-group" open>
      <summary>买入评分组成和真实阈值</summary>
      <div class="detail-content">
        <table class="param-table"><thead><tr><th>评分项</th><th>最高分</th><th>参数意义</th></tr></thead><tbody>${rows(info.buy_scores, true)}</tbody></table>
        <div class="notice" style="margin-top:14px"><strong>买入条件：</strong>${info.thresholds.buy}<br><strong>卖出条件：</strong>${info.thresholds.sell}<br>${info.thresholds.note}</div>
      </div>
    </details>
    <details class="rule-group">
      <summary>技术参数词典</summary>
      <div class="detail-content"><table class="param-table"><thead><tr><th>参数</th><th>含义</th></tr></thead><tbody>${info.terms.map(x => `<tr><td><strong>${x.name}</strong></td><td>${x.meaning}</td></tr>`).join('')}</tbody></table></div>
    </details>
  `;
}

function showTab(tabId, button) {
  document.querySelectorAll('.tab-panel').forEach(panel => panel.classList.toggle('active', panel.id === tabId));
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.toggle('active', btn === button));
  if (tabId === 'dashboardTab' && breadthChartInstance) breadthChartInstance.resize();
}

async function saveSchedule() {
  const weekdays = Array.from(document.querySelectorAll('input[name="weekday"]:checked')).map(input => Number(input.value));
  const [hour, minute] = document.getElementById('scheduleTime').value.split(':').map(Number);
  const statusEl = document.getElementById('scheduleSaveStatus');
  if (weekdays.length === 0) {
    setSaveStatus(statusEl, 'error', '请至少选择一个有效执行日');
    return;
  }
  setSaveStatus(statusEl, 'loading', '正在保存定时任务配置...');
  try {
    const response = await fetch('api/schedule', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken},
      body: JSON.stringify({enabled: document.getElementById('scheduleEnabled').checked, weekdays, hour, minute})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '保存失败');
    systemInfoData = result;
    renderSystemInfo(result);
    setSaveStatus(document.getElementById('scheduleSaveStatus'), 'success', '定时任务已更新');
    showToast('定时任务配置已更新，无需重启容器。');
  } catch (error) {
    setSaveStatus(document.getElementById('scheduleSaveStatus'), 'error', error.message);
  }
}

async function saveTelegramSettings(sendTest) {
  const statusEl = document.getElementById('telegramSaveStatus');
  const payload = {
    enabled: document.getElementById('telegramEnabled').checked,
    bot_token: document.getElementById('telegramBotToken').value,
    chat_id: document.getElementById('telegramChatId').value.trim(),
    notify_full: document.getElementById('telegramNotifyFull').checked,
    notify_test: document.getElementById('telegramNotifyTest').checked
  };
  setSaveStatus(statusEl, 'loading', sendTest ? '正在保存并发送测试推送...' : '正在保存配置...');
  try {
    const response = await fetch('api/telegram-settings', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken},
      body: JSON.stringify(payload)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '保存失败');
    systemInfoData = result;
    renderSystemInfo(result);
    if (sendTest) {
      const testResponse = await fetch('api/telegram-settings/test', {
        method: 'POST',
        headers: {'X-CSRF-Token': csrfToken}
      });
      const testResult = await testResponse.json();
      if (!testResponse.ok) throw new Error(testResult.error || '测试消息发送失败');
      setSaveStatus(document.getElementById('telegramSaveStatus'), 'success', '配置已保存，测试消息送达成功');
      showToast('Telegram 配置已保存，测试消息已送达！');
    } else {
      setSaveStatus(document.getElementById('telegramSaveStatus'), 'success', 'TG 推送配置已更新');
      showToast('Telegram 推送配置已更新，无需重启容器。');
    }
  } catch (error) {
    const currentStatus = document.getElementById('telegramSaveStatus');
    if (currentStatus) setSaveStatus(currentStatus, 'error', error.message);
  }
}

async function saveScanSettings() {
  const statusEl = document.getElementById('scanSettingsSaveStatus');
  const resultLimit = Number(document.getElementById('resultLimit').value);
  if (!Number.isInteger(resultLimit) || resultLimit < 5 || resultLimit > 100) {
    setSaveStatus(statusEl, 'error', '展示数量必须在 5–100 之间');
    return;
  }
  setSaveStatus(statusEl, 'loading', '正在保存扫描配置...');
  try {
    const response = await fetch('api/scan-settings', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken},
      body: JSON.stringify({
        universe_mode: document.getElementById('universeMode').value,
        result_limit: resultLimit,
        price_cache_enabled: true,
        price_cache_full_refresh_days: 30
      })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '保存失败');
    systemInfoData = result;
    renderSystemInfo(result);
    setSaveStatus(document.getElementById('scanSettingsSaveStatus'), 'success', '扫描配置已更新');
    showToast('扫描配置已更新，将从下一次扫描生效。');
  } catch (error) {
    setSaveStatus(document.getElementById('scanSettingsSaveStatus'), 'error', error.message);
  }
}

async function loadScanList() {
  const select = document.getElementById('scanSelect');
  const res = await fetch('api/scans');
  const scans = await res.json();
  if (!res.ok || !Array.isArray(scans) || scans.length === 0) {
    select.innerHTML = '<option value="">暂无全量扫描记录</option>';
    select.disabled = true;
    document.getElementById('noData').style.display = '';
    document.getElementById('dashboard').style.display = 'none';
    return;
  }

  select.disabled = false;
  select.innerHTML = scans.map((scan) => {
    const date = scan.date.replace('_', ' ');
    const universe = scan.total_universe == null ? '数量未知' : `${scan.total_universe.toLocaleString()}只`;
    return `<option value="${scan.path}">${date} · ${universe}</option>`;
  }).join('');
  await loadScan(scans[0].path);
}

async function loadScan(path) {
  if (!path) return;
  const res = await fetch('api/scan?path=' + encodeURIComponent(path));
  const data = await res.json();
  if (!res.ok || data.error) {
    document.getElementById('noData').style.display = '';
    document.getElementById('dashboard').style.display = 'none';
    return;
  }
  renderDashboard(data);
}

function renderDashboard(d) {
  document.getElementById('noData').style.display = 'none';
  document.getElementById('dashboard').style.display = '';
  document.getElementById('headerMeta').textContent =
    `扫描日期：${d.scan_date}｜已分析 ${(d.stats.analyzed || 0).toLocaleString()} 只｜${regimeName(d.regime)}`;

  // Stats cards
  const buyCount = d.stats.buy_count || d.buy_signals.length;
  const sellCount = d.stats.sell_count || d.sell_signals.length;
  const topScore = d.buy_signals.length > 0 ? d.buy_signals[0].score : '-';

  document.getElementById('statsRow').innerHTML = `
    <div class="stat-card green">
      <div class="label">买入信号</div>
      <div class="value">${buyCount}</div>
      <div class="sub">确认处于 Phase 2 的上升趋势</div>
    </div>
    <div class="stat-card red">
      <div class="label">卖出信号</div>
      <div class="value">${sellCount}</div>
      <div class="sub">Phase 3/4 转弱或跌破</div>
    </div>
    <div class="stat-card blue">
      <div class="label">最高买入评分</div>
      <div class="value">${topScore}<span style="font-size:14px;color:var(--muted)">/125</span></div>
      <div class="sub">${d.buy_signals.length > 0 ? d.buy_signals[0].ticker : '-'}</div>
    </div>
    <div class="stat-card">
      <div class="label">本次分析</div>
      <div class="value">${(d.stats.analyzed || 0).toLocaleString()}</div>
      <div class="sub">股票池 ${(d.stats.total_universe || 0).toLocaleString()} 只｜耗时 ${d.stats.processing_minutes || '?'} 分钟</div>
    </div>
    <div class="stat-card ${d.spy && d.spy.phase === 2 ? 'green' : d.spy && d.spy.phase === 4 ? 'red' : 'yellow'}">
      <div class="label">SPY 阶段</div>
      <div class="value">${d.spy.phase || '?'}</div>
      <div class="sub">${trendNames[d.spy.trend] || d.spy.trend || ''}｜$${(d.spy.price || 0).toFixed(2)}</div>
    </div>
  `;

  // Breadth chart
  const b = d.breadth;
  if (breadthChartInstance) breadthChartInstance.destroy();
  const ctx = document.getElementById('breadthChart').getContext('2d');
  breadthChartInstance = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['Phase 1（筑底）', 'Phase 2（上升）', 'Phase 3（派发）', 'Phase 4（下降）'],
      datasets: [{
        data: [b.phase_1_pct || 0, b.phase_2_pct || 0, b.phase_3_pct || 0, b.phase_4_pct || 0],
        backgroundColor: ['#eab308', '#22c55e', '#f97316', '#ef4444'],
        borderColor: '#1a1d27',
        borderWidth: 3
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: 'right', labels: { color: '#8b8fa3', font: { size: 11 }, padding: 12 } },
        tooltip: { callbacks: { label: (c) => `${c.label}：${c.parsed}%（${b[`phase_${c.dataIndex+1}_count`] || 0} 只）` } }
      }
    }
  });

  // SPY info
  const regimeClass = d.regime.includes('RISK-ON') ? 'risk-on' : d.regime.includes('RISK-OFF') ? 'risk-off' : 'transitional';
  document.getElementById('spyInfo').innerHTML = `
    <div style="margin-bottom:16px">
      <span class="regime-badge ${regimeClass}">${regimeName(d.regime)}</span>
    </div>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
      <div>
        <div style="color:var(--muted);font-size:11px;margin-bottom:2px">阶段</div>
        <div style="font-size:20px;font-weight:700">${phaseNames[d.spy.phase] || '?'}</div>
      </div>
      <div>
        <div style="color:var(--muted);font-size:11px;margin-bottom:2px">判断置信度</div>
        <div style="font-size:20px;font-weight:700">${d.spy.confidence || '?'}%</div>
      </div>
      <div>
        <div style="color:var(--muted);font-size:11px;margin-bottom:2px">SPY 价格</div>
        <div style="font-size:20px;font-weight:700">$${(d.spy.price || 0).toFixed(2)}</div>
      </div>
      <div>
        <div style="color:var(--muted);font-size:11px;margin-bottom:2px">趋势</div>
        <div style="font-size:20px;font-weight:700;color:${d.spy.trend === 'Bullish' ? 'var(--green)' : d.spy.trend === 'Bearish' ? 'var(--red)' : 'var(--yellow)'}">${trendNames[d.spy.trend] || d.spy.trend || '?'}</div>
      </div>
    </div>
    <div style="margin-top:20px;padding:12px;background:var(--bg);border-radius:8px;font-size:12px;color:var(--muted)">
      ${d.regime.includes('RISK-ON')
        ? '市场环境允许寻找突破机会，重点关注 Phase 2 且相对强度较高的股票。'
        : d.regime.includes('RISK-OFF')
        ? '当前偏防御，应控制仓位、收紧止损，并谨慎对待新的突破信号。'
        : '市场处于混合或过渡阶段，应提高筛选标准，只关注质量最高的候选。'}
    </div>
  `;

  // Buy signals table
  const buyBody = document.querySelector('#buyTable tbody');
  if (d.buy_signals.length === 0) {
    buyBody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:30px">本次没有满足条件的买入信号</td></tr>';
  } else {
    buyBody.innerHTML = d.buy_signals.map(s => {
      const pct = (s.score / (s.max_score || 125)) * 100;
      const barColor = pct >= 80 ? 'var(--green)' : pct >= 60 ? 'var(--blue)' : 'var(--yellow)';
      const entryClass = (s.entry_quality || '').toLowerCase();
      const topReasons = (s.reasons || []).slice(0, 3).map(r => `<li>${translateReason(r)}</li>`).join('');
      const extraReasons = (s.reasons || []).slice(3);
      const extraHTML = extraReasons.length > 0
        ? `<div class="extra-reasons" style="display:none">${extraReasons.map(r => `<li>${translateReason(r)}</li>`).join('')}</div><button class="expand-btn" data-count="${extraReasons.length}" onclick="toggleReasons(this)">展开其余 ${extraReasons.length} 条</button>`
        : '';

      return `<tr>
        <td>${s.rank}</td>
        <td><span class="ticker">${s.ticker}</span></td>
        <td>
          <span class="score-num">${s.score}</span>
          <div class="score-bar"><div class="fill" style="width:${pct}%;background:${barColor}"></div></div>
        </td>
        <td><span class="badge ${entryClass}">${entryNames[s.entry_quality] || s.entry_quality || '-'}</span></td>
        <td>${s.stop_loss ? '$' + s.stop_loss.toFixed(2) : '-'}</td>
        <td style="color:${(s.rr_ratio||0) >= 3 ? 'var(--green)' : (s.rr_ratio||0) >= 2 ? 'var(--text)' : 'var(--yellow)'}">${s.rr_ratio ? s.rr_ratio.toFixed(1) + ':1' : '-'}</td>
        <td style="color:${(s.rs||0) > 0.1 ? 'var(--green)' : (s.rs||0) > 0 ? 'var(--text)' : 'var(--red)'}">${s.rs != null ? s.rs.toFixed(3) : '-'}</td>
        <td><ul class="reasons-list">${topReasons}</ul>${extraHTML}</td>
      </tr>`;
    }).join('');
  }

  // Sell signals table
  const sellBody = document.querySelector('#sellTable tbody');
  if (d.sell_signals.length === 0) {
    sellBody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:30px">本次没有满足条件的卖出信号</td></tr>';
  } else {
    sellBody.innerHTML = d.sell_signals.map(s => {
      const sevClass = (s.severity || 'medium').toLowerCase();
      const reasons = (s.reasons || []).map(r => `<li>${translateReason(r)}</li>`).join('');
      return `<tr>
        <td>${s.rank}</td>
        <td><span class="ticker">${s.ticker}</span></td>
        <td><span class="score-num">${s.score}</span></td>
        <td><span class="badge ${sevClass}">${severityNames[sevClass] || s.severity || '?'}</span></td>
        <td>${s.breakdown_level ? '$' + s.breakdown_level.toFixed(2) : '-'}</td>
        <td><ul class="reasons-list">${reasons}</ul></td>
      </tr>`;
    }).join('');
  }
}

function cleanEmoji(text) {
  return text.replace(/[🟢🔴🟡⭐⚠✓🚨]/g, '').trim();
}

function toggleReasons(btn) {
  const extra = btn.previousElementSibling;
  if (extra.style.display === 'none') {
    extra.style.display = '';
    btn.textContent = '收起';
  } else {
    extra.style.display = 'none';
    btn.textContent = `展开其余 ${btn.dataset.count} 条`;
  }
}

async function runScan(full) {
  const scan = systemInfoData && systemInfoData.scan ? systemInfoData.scan : {};
  const universeLabel = scan.universe_label || '当前配置的股票池';
  const expectedTime = scan.universe_mode === 'all_us' ? '通常需要 60–90 分钟' : '首次通常需要 10–20 分钟，后续增量扫描通常更快';
  const confirmation = full
    ? `确认开始全量扫描吗？将扫描“${universeLabel}”，${expectedTime}，期间不能启动其他扫描。`
    : `确认开始测试扫描吗？将扫描“${universeLabel}”的前 100 只，测试结果不会替换正式 Dashboard，也默认不推送 Telegram。`;
  if (!window.confirm(confirmation)) return;

  const btn = full ? document.getElementById('fullBtn') : document.getElementById('testBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>正在启动...';

  try {
    const res = await fetch('api/run-scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({full: full})
    });
    const result = await res.json();
    if (res.ok && result.success) {
      showToast(full ? '全量扫描已在后台启动。' : '测试扫描已在后台启动。');
      previousScanStatus = 'running';
      await refreshScanStatus();
    } else {
      showToast('无法启动扫描：' + translateError(result.error));
      await refreshScanStatus();
    }
  } catch (e) {
    showToast('请求出错：' + e.message);
    setScanButtons(false);
  }
}

function setScanButtons(running, full = false) {
  const testBtn = document.getElementById('testBtn');
  const fullBtn = document.getElementById('fullBtn');
  testBtn.disabled = running;
  fullBtn.disabled = running;
  testBtn.textContent = running && !full ? '测试扫描中...' : '测试扫描（100只）';
  fullBtn.textContent = running && full ? '全量扫描中...' : '全量扫描';
}

function translateError(error) {
  const mapping = {
    'A scan is already running': '已有扫描正在运行',
    'A scheduled or manual scan is already running': '定时或手动扫描正在运行',
    'unknown error': '未知错误'
  };
  return mapping[error] || error || '未知错误';
}

async function refreshScanStatus() {
  try {
    const res = await fetch('api/scan-status');
    const state = await res.json();
    const statusEl = document.getElementById('scanStatus');

    if (state.status === 'running') {
      setScanButtons(true, state.full === true);
      const kind = state.full ? '全量扫描' : '测试扫描';
      statusEl.innerHTML = '<span class="spinner"></span>' + kind + '运行中';
    } else {
      setScanButtons(false);
      statusEl.textContent = state.status === 'failed' ? '上次扫描失败' : '空闲';

      if (previousScanStatus === 'running') {
        if (state.status === 'completed') {
          if (state.full === true) {
            showToast('全量扫描完成，正在加载最新结果...');
            await loadScanList();
          } else {
            showToast('测试扫描完成，正式 Dashboard 结果未被替换。');
          }
        } else if (state.status === 'failed') {
          showToast('扫描失败，请检查扫描日志。');
        }
      }
    }

    previousScanStatus = state.status;
  } catch (e) {
    document.getElementById('scanStatus').textContent = '状态不可用';
  }
}

function showToast(msg) {
  let t = document.getElementById('toast');
  if (!t) { t = document.createElement('div'); t.id = 'toast'; t.className = 'toast'; document.body.appendChild(t); }
  t.textContent = msg;
  t.style.opacity = '1';
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.style.opacity = '0'; }, 5000);
}

loadScanList();
loadSystemInfo();
refreshScanStatus();
setInterval(refreshScanStatus, 5000);
</script>
</body>
</html>
"""


@app.before_request
def require_login():
    if (
        request.path.startswith("/internal/")
        or request.path.startswith("/api/v1/screen")
        or request.path == "/api/v1/capabilities"
    ):
        return _internal_api_auth_error()
    if request.endpoint in {"login", "healthz", "favicon"}:
        return None
    if session.get("authenticated"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "请先登录"}), 401
    return redirect(url_for("login", next=request.full_path if request.query_string else request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        ip_address = request.headers.get("X-Real-IP") or request.remote_addr or "unknown"
        if _login_is_rate_limited(ip_address):
            error = "尝试次数过多，请 5 分钟后再试"
        else:
            expected_username = os.getenv("DASHBOARD_USERNAME", "stock")
            password_hash = os.getenv("DASHBOARD_PASSWORD_HASH", "")
            username = request.form.get("username", "")
            password = request.form.get("password", "")

            if password_hash and secrets.compare_digest(username, expected_username) and check_password_hash(password_hash, password):
                _clear_login_failures(ip_address)
                session.clear()
                session["authenticated"] = True
                session["username"] = username
                session["csrf_token"] = secrets.token_urlsafe(32)
                session.permanent = True
                next_url = request.args.get("next", "")
                if not next_url.startswith("/") or next_url.startswith("//"):
                    next_url = url_for("index")
                return redirect(next_url)

            _record_login_failure(ip_address)
            error = "账号或密码不正确"

    return render_template_string(LOGIN_HTML, error=error, app_icon=APP_ICON_SVG)


@app.route("/favicon.ico")
@app.route("/favicon.svg")
def favicon():
    return Response(APP_ICON_SVG, mimetype="image/svg+xml")


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/logout", methods=["POST"])
def logout():
    if not secrets.compare_digest(request.form.get("csrf_token", ""), session.get("csrf_token", "")):
        return "Invalid CSRF token", 400
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML, csrf_token=session["csrf_token"], app_icon=APP_ICON_SVG)


@app.route("/api/system-info")
def api_system_info():
    return jsonify(_build_system_info())


@app.route("/api/schedule", methods=["PUT"])
def api_schedule():
    if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session.get("csrf_token", "")):
        return jsonify({"error": "CSRF 校验失败"}), 400

    body = request.get_json(silent=True) or {}
    try:
        enabled = body.get("enabled") is True
        weekdays = sorted({int(day) for day in body.get("weekdays", [])})
        hour = int(body.get("hour"))
        minute = int(body.get("minute"))
    except (TypeError, ValueError):
        return jsonify({"error": "定时任务参数格式不正确"}), 400

    if not weekdays or any(day < 0 or day > 6 for day in weekdays):
        return jsonify({"error": "请至少选择一个有效执行日"}), 400
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return jsonify({"error": "执行时间不正确"}), 400

    config = {
        "enabled": enabled,
        "timezone": "America/New_York",
        "weekdays": weekdays,
        "hour": hour,
        "minute": minute,
    }
    _write_schedule_config(config)
    return jsonify(_build_system_info())


@app.route("/api/telegram-settings", methods=["PUT"])
def api_telegram_settings():
    if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session.get("csrf_token", "")):
        return jsonify({"error": "CSRF 校验失败"}), 400

    body = request.get_json(silent=True) or {}
    settings = read_telegram_settings()
    settings.update(
        {
            "enabled": body.get("enabled") is True,
            "chat_id": body.get("chat_id", ""),
            "notify_full": body.get("notify_full") is True,
            "notify_test": body.get("notify_test") is True,
        }
    )
    new_token = str(body.get("bot_token", "")).strip()
    if new_token:
        settings["bot_token"] = new_token

    try:
        write_telegram_settings(settings)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(_build_system_info())


@app.route("/api/telegram-settings/test", methods=["POST"])
def api_telegram_settings_test():
    if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session.get("csrf_token", "")):
        return jsonify({"error": "CSRF 校验失败"}), 400
    from src.notifications.telegram_notifier import TelegramNotifier

    if not TelegramNotifier().send_test_message():
        return jsonify({"error": "测试消息发送失败，请检查 Bot Token、Chat ID 和机器人权限"}), 502
    return jsonify({"success": True})


@app.route("/api/scan-settings", methods=["PUT"])
def api_scan_settings():
    if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session.get("csrf_token", "")):
        return jsonify({"error": "CSRF 校验失败"}), 400

    body = request.get_json(silent=True) or {}
    settings = read_scan_settings()
    settings.update(body)
    try:
        write_scan_settings(settings)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(_build_system_info())


@app.route("/api/scan")
def api_scan():
    requested_path = request.args.get("path", "")
    if requested_path:
        requested_name = Path(requested_path).name
        if requested_name != requested_path or not re.fullmatch(
            r"optimized_scan_\d{8}_\d{6}\.txt", requested_name
        ):
            return jsonify({"error": "Invalid scan path"}), 400
        path = (SCAN_DIR / requested_name).resolve()
        if path.parent != SCAN_DIR.resolve():
            return jsonify({"error": "Invalid scan path"}), 400
    else:
        path = (SCAN_DIR / "latest_optimized_scan.txt").resolve()

    if not path.exists():
        return jsonify({"error": "File not found"}), 404

    return jsonify(parse_scan_file(path))


@app.route("/api/scans")
def api_scans():
    return jsonify(get_scan_files())


@app.route("/api/run-scan", methods=["POST"])
def api_run_scan():
    global _scan_process
    body = request.json or {}
    full = body.get("full") is True

    with _scan_guard:
        if _process_is_running(_scan_process):
            return jsonify({"success": False, "error": "A scan is already running"}), 409

        lock_check = subprocess.run(
            ["/usr/bin/flock", "-n", str(SCAN_LOCK_FILE), "true"],
            capture_output=True,
        )
        if lock_check.returncode != 0:
            return jsonify({"success": False, "error": "A scheduled or manual scan is already running"}), 409

        python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
        cmd = ["/usr/bin/flock", "-n", str(SCAN_LOCK_FILE), python, *SCAN_COMMAND_ARGS]
        if not full:
            cmd.append("--test-mode")

        SCAN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = SCAN_LOG_DIR / f"web_scan_{timestamp}.log"
        log_file = log_path.open("w", encoding="utf-8")

        try:
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=str(PROJECT_ROOT),
                start_new_session=True,
            )
        except Exception:
            log_file.close()
            raise

        state = {
            "status": "running",
            "full": full,
            "pid": process.pid,
            "started_at": datetime.now().astimezone().isoformat(),
            "log": log_path.name,
        }
        _scan_process = process
        _write_scan_state(state)

        monitor = threading.Thread(
            target=_monitor_scan,
            args=(process, state, log_file),
            daemon=True,
        )
        monitor.start()

    return jsonify({"success": True, **state}), 202


@app.route("/api/scan-status")
def api_scan_status():
    with _scan_guard:
        state = _read_scan_state()
        if _process_is_running(_scan_process):
            state["status"] = "running"
        return jsonify(state)


@app.route("/internal/v1/screenings", methods=["POST"])
@app.route("/api/v1/screen", methods=["POST"])
def internal_create_screening():
    global _scan_process
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    standard_api = request.path == "/api/v1/screen"
    parameters = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
    if standard_api and str(body.get("strategy", "stage2_breakout")) != "stage2_breakout":
        return jsonify({"error": "strategy must be stage2_breakout"}), 400
    if standard_api and str(body.get("market", "US")).upper() != "US":
        return jsonify({"error": "stock-screener currently supports market US only"}), 400

    try:
        tickers = _normalize_api_tickers(body.get("tickers", parameters.get("tickers")))
        result_limit = int(
            body.get("limit", body.get("result_limit", read_scan_settings()["result_limit"]))
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

    if result_limit < 1 or result_limit > 100:
        return jsonify({"error": "result_limit must be between 1 and 100"}), 400
    universe = body.get("universe", parameters.get("universe"))
    if tickers is None and universe != "configured":
        return jsonify(
            {"error": "Provide tickers or explicitly set universe to configured"}
        ), 400

    with _scan_guard:
        if _process_is_running(_scan_process):
            return jsonify({"error": "A scan is already running"}), 409

        lock_check = subprocess.run(
            ["/usr/bin/flock", "-n", str(SCAN_LOCK_FILE), "true"],
            capture_output=True,
        )
        if lock_check.returncode != 0:
            return jsonify({"error": "A scheduled or manual scan is already running"}), 409

        job_id = uuid.uuid4().hex
        job_dir = API_SCAN_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        state_file = job_dir / "state.json"
        result_file = job_dir / "latest_api_scan.json"
        log_path = job_dir / "scan.log"

        python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
        cmd = [
            "/usr/bin/flock",
            "-n",
            str(SCAN_LOCK_FILE),
            python,
            *SCAN_COMMAND_ARGS,
            "--api-job",
            job_id,
            "--no-telegram",
            "--output-dir",
            str(job_dir),
            "--result-limit",
            str(result_limit),
        ]
        if tickers is not None:
            tickers_file = job_dir / "tickers.json"
            tickers_file.write_text(
                json.dumps(tickers, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            cmd.extend(["--tickers-file", str(tickers_file)])

        log_file = log_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=str(PROJECT_ROOT),
                start_new_session=True,
            )
        except Exception:
            log_file.close()
            raise

        state = {
            "job_id": job_id,
            "status": "running",
            "source": "fin_twt",
            "notifications": False,
            "pid": process.pid,
            "started_at": datetime.now().astimezone().isoformat(),
            "request": {
                "universe": "custom" if tickers is not None else "configured",
                "ticker_count": len(tickers) if tickers is not None else None,
                "tickers": tickers,
                "result_limit": result_limit,
            },
        }
        _scan_process = process
        _write_state_file(state_file, state)

        monitor = threading.Thread(
            target=_monitor_api_scan,
            args=(process, state, log_file, state_file, result_file),
            daemon=True,
        )
        monitor.start()

    if standard_api:
        return jsonify(running_envelope(job_id, state, f"/api/v1/screen/{job_id}")), 202
    return jsonify({**state, "status_url": f"/internal/v1/screenings/{job_id}"}), 202


@app.route("/internal/v1/screenings/<job_id>")
@app.route("/api/v1/screen/<job_id>")
def internal_get_screening(job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return jsonify({"error": "Invalid job ID"}), 400

    job_dir = API_SCAN_DIR / job_id
    state_file = job_dir / "state.json"
    if not state_file.exists():
        return jsonify({"error": "Screening job not found"}), 404

    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return jsonify({"error": "Could not read screening job state"}), 500

    result_file = job_dir / "latest_api_scan.json"
    if state.get("status") == "completed" and result_file.exists():
        try:
            result = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return jsonify({"error": "Could not read screening result"}), 500

        result_limit = int(state.get("request", {}).get("result_limit", 20))
        result["buy_signals"] = result.get("buy_signals", [])[:result_limit]
        result["sell_signals"] = result.get("sell_signals", [])[:result_limit]
        if request.path.startswith("/api/v1/screen/"):
            return jsonify(completed_envelope(job_id, state, result))
        state["result"] = result

    if request.path.startswith("/api/v1/screen/"):
        if state.get("status") == "failed":
            return jsonify(failed_envelope(job_id, state))
        return jsonify(running_envelope(job_id, state, f"/api/v1/screen/{job_id}"))

    return jsonify(state)


@app.route("/api/v1/capabilities")
def screener_capabilities():
    return jsonify(
        {
            "schema_versions": ["1.0"],
            "provider": "stock-screener",
            "markets": ["US"],
            "strategies": ["stage2_breakout"],
            "execution": "async",
        }
    )


def open_browser(port):
    webbrowser.open(f"http://localhost:{port}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="强势突破股票筛选器 Dashboard")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--scan", action="store_true", help="Run a test scan before launching")
    parser.add_argument("--full", action="store_true", help="With --scan, run full scan instead of test")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    if args.scan:
        print("Running scan before dashboard launch...")
        python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
        cmd = [python, "run_optimized_scan.py"]
        if not args.full:
            cmd.append("--test-mode")
        subprocess.run(cmd, cwd=str(Path(__file__).parent))

    if not args.no_browser:
        Timer(1.5, open_browser, [args.port]).start()

    print(f"\n  Dashboard running at http://localhost:{args.port}\n")
    app.run(host="0.0.0.0", port=args.port, debug=False)
