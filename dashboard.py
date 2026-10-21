#!/usr/bin/env python3
"""Interactive web dashboard for Stock Screener results.

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
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from threading import Timer

from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

PROJECT_ROOT = Path(__file__).parent.resolve()
SCAN_DIR = PROJECT_ROOT / "data" / "daily_scans"
VENV_PYTHON = PROJECT_ROOT / "venv" / "bin" / "python"
SCAN_LOG_DIR = PROJECT_ROOT / "data" / "logs"
SCAN_STATE_FILE = SCAN_LOG_DIR / "scan_job.json"
SCAN_LOCK_FILE = PROJECT_ROOT / "data" / ".scan.lock"
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
        "universe_size": "约 3,700–3,800 只美股",
        "test_size": 100,
        "full_scan_time": "通常 60–90 分钟，数据源限流或重试时可能更久",
        "workers": 2,
        "request_delay": "全局价格请求之间至少间隔 1.0 秒",
        "price_refresh": "每次重新获取每只股票 5 年日线；最近约 252 个交易日用于技术分析",
        "fundamental_cache": "财报季缓存 7 天，非财报季缓存 90 天",
        "telegram": "全量和定时扫描完成后通知；测试扫描默认不通知",
    },
    "filters": [
        {"name": "上市标的", "value": "NASDAQ / NYSE / AMEX 普通股", "meaning": "排除 ETF、基金、债券、权证、优先股和特殊代码"},
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

_scan_process = None
_scan_guard = threading.Lock()


def _read_scan_state():
    if not SCAN_STATE_FILE.exists():
        return {"status": "idle"}
    try:
        return json.loads(SCAN_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown", "error": "Could not read scan state"}


def _write_scan_state(state):
    SCAN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = SCAN_STATE_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp_file.replace(SCAN_STATE_FILE)


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
    """Get list of available scan files, newest first."""
    if not SCAN_DIR.exists():
        return []
    files = sorted(SCAN_DIR.glob("optimized_scan_*.txt"), reverse=True)
    return [{"name": f.stem, "path": f.name, "date": f.stem.replace("optimized_scan_", "")} for f in files]


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>美股选股系统</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --border: #2a2d3a;
    --text: #e4e6eb;
    --muted: #8b8fa3;
    --green: #22c55e;
    --red: #ef4444;
    --yellow: #eab308;
    --blue: #3b82f6;
    --purple: #a855f7;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); }

  .header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 16px 32px; display: flex; align-items: center; justify-content: space-between; }
  .header h1 { font-size: 20px; font-weight: 600; }
  .header .meta { color: var(--muted); font-size: 13px; }
  .header .actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }

  .btn { padding: 8px 16px; border-radius: 6px; border: 1px solid var(--border); background: var(--surface); color: var(--text); cursor: pointer; font-size: 13px; transition: all 0.15s; }
  .btn:hover { border-color: var(--blue); }
  .btn.primary { background: var(--blue); border-color: var(--blue); color: #fff; }
  .btn.primary:hover { opacity: 0.9; }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }

  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }

  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
  .stat-card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
  .stat-card .value { font-size: 28px; font-weight: 700; }
  .stat-card .sub { color: var(--muted); font-size: 12px; margin-top: 4px; }
  .stat-card.green .value { color: var(--green); }
  .stat-card.red .value { color: var(--red); }
  .stat-card.blue .value { color: var(--blue); }
  .stat-card.yellow .value { color: var(--yellow); }

  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  @media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }

  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
  .card h2 { font-size: 15px; font-weight: 600; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }

  .regime-badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; }
  .regime-badge.risk-on { background: rgba(34,197,94,0.15); color: var(--green); }
  .regime-badge.risk-off { background: rgba(239,68,68,0.15); color: var(--red); }
  .regime-badge.transitional { background: rgba(234,179,8,0.15); color: var(--yellow); }

  .chart-wrap { position: relative; height: 250px; }

  .signal-table { width: 100%; border-collapse: collapse; }
  .signal-table th { text-align: left; padding: 10px 12px; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid var(--border); }
  .signal-table td { padding: 12px; border-bottom: 1px solid var(--border); font-size: 13px; vertical-align: top; }
  .signal-table tr:hover { background: rgba(59,130,246,0.04); }

  .ticker { font-weight: 700; font-size: 14px; color: var(--blue); }
  .score-bar { height: 6px; border-radius: 3px; background: var(--border); overflow: hidden; width: 80px; display: inline-block; vertical-align: middle; margin-left: 6px; }
  .score-bar .fill { height: 100%; border-radius: 3px; }
  .score-num { font-weight: 600; font-size: 13px; }

  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  .badge.good { background: rgba(34,197,94,0.15); color: var(--green); }
  .badge.extended { background: rgba(234,179,8,0.15); color: var(--yellow); }
  .badge.poor { background: rgba(239,68,68,0.15); color: var(--red); }
  .badge.critical { background: rgba(239,68,68,0.25); color: var(--red); }
  .badge.high { background: rgba(239,68,68,0.15); color: var(--red); }
  .badge.medium { background: rgba(234,179,8,0.15); color: var(--yellow); }

  .reasons-list { list-style: none; padding: 0; margin-top: 4px; }
  .reasons-list li { font-size: 11px; color: var(--muted); padding: 1px 0; }

  .expand-btn { background: none; border: none; color: var(--blue); cursor: pointer; font-size: 11px; padding: 2px 0; }
  .expand-btn:hover { text-decoration: underline; }

  .no-data { text-align: center; padding: 60px 20px; color: var(--muted); }
  .no-data h2 { font-size: 18px; margin-bottom: 8px; color: var(--text); }

  .scan-select { background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 6px 10px; border-radius: 6px; font-size: 13px; }

  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--border); border-top-color: var(--blue); border-radius: 50%; animation: spin 0.6s linear infinite; margin-right: 6px; vertical-align: middle; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .toast { position: fixed; bottom: 24px; right: 24px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px 20px; font-size: 13px; box-shadow: 0 4px 20px rgba(0,0,0,0.4); z-index: 100; transition: opacity 0.3s; }
  .scan-status { color: var(--muted); font-size: 12px; align-self: center; white-space: nowrap; }
  .section-title { font-size: 18px; margin: 30px 0 14px; }
  .info-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
  .info-list { padding-left: 20px; color: var(--muted); font-size: 13px; line-height: 1.75; }
  .info-list strong { color: var(--text); }
  .param-table { width: 100%; border-collapse: collapse; }
  .param-table th, .param-table td { text-align: left; padding: 10px; border-bottom: 1px solid var(--border); font-size: 12px; vertical-align: top; }
  .param-table th { color: var(--muted); }
  .code-block { background: var(--bg); border: 1px solid var(--border); border-radius: 7px; padding: 10px 12px; color: #cbd5e1; font: 12px ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; margin-top: 8px; }
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
  <div>
    <h1>美股选股系统</h1>
    <div class="meta" id="headerMeta">正在加载...</div>
  </div>
  <div class="actions">
    <span class="scan-status" id="scanStatus">空闲</span>
    <select class="scan-select" id="scanSelect" onchange="loadScan(this.value)"></select>
    <button class="btn" onclick="runScan(false)" id="testBtn">测试扫描（100只）</button>
    <button class="btn primary" onclick="runScan(true)" id="fullBtn">全量扫描</button>
  </div>
</div>

<div class="container" id="content">
  <div class="no-data" id="noData" style="display:none">
    <h2>暂无扫描数据</h2>
    <p>请先运行一次测试扫描</p>
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

    <h2 class="section-title">系统运行机制与选股规则</h2>
    <div id="systemInfo"><div class="card">正在加载系统配置...</div></div>

  </div>
</div>

<script>
let breadthChartInstance = null;
let previousScanStatus = null;
let systemInfoData = null;

const phaseNames = {1: 'Phase 1（筑底/盘整）', 2: 'Phase 2（上升趋势）', 3: 'Phase 3（派发/见顶）', 4: 'Phase 4（下降趋势）'};
const trendNames = {Bullish: '上涨', Bearish: '下跌', Consolidating: '盘整', Topping: '见顶', Unknown: '未知'};
const entryNames = {Good: '良好', Extended: '偏离', Poor: '较差'};
const severityNames = {critical: '严重', high: '高', medium: '中等', low: '低'};

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

function renderSystemInfo(info) {
  const schedule = info.schedule;
  const runtime = info.runtime;
  document.getElementById('systemInfo').innerHTML = `
    <div class="info-grid" style="margin-bottom:16px">
      <div class="card">
        <h2>定时任务</h2>
        <ul class="info-list">
          <li><strong>执行时间：</strong>${schedule.description}</li>
          <li><strong>北京时间：</strong>${schedule.beijing_time}</li>
          <li><strong>Cron：</strong><code>${schedule.cron}</code></li>
          <li><strong>时区：</strong>${schedule.timezone}</li>
          <li><strong>防重复：</strong>${schedule.overlap_protection}</li>
        </ul>
        <div class="code-block">${schedule.command}</div>
      </div>
      <div class="card">
        <h2>扫描运行方式</h2>
        <ul class="info-list">
          <li><strong>股票池：</strong>${runtime.universe_size}</li>
          <li><strong>测试扫描：</strong>${runtime.test_size} 只</li>
          <li><strong>全量耗时：</strong>${runtime.full_scan_time}</li>
          <li><strong>并发：</strong>${runtime.workers} 个工作线程，但价格请求受全局限速控制</li>
          <li><strong>请求节奏：</strong>${runtime.request_delay}</li>
          <li><strong>价格数据：</strong>${runtime.price_refresh}</li>
          <li><strong>基本面缓存：</strong>${runtime.fundamental_cache}</li>
          <li><strong>Telegram：</strong>${runtime.telegram}</li>
        </ul>
      </div>
    </div>
    <div class="notice" style="margin-bottom:16px"><strong>为什么每天全量扫描都很慢：</strong>目前不是只补当天行情，而是每次逐只重新下载 5 年价格历史并执行回撤检查。基本面缓存不会消除这些价格请求，所以每天通常仍需 60–90 分钟。要明显提速，需要改成批量下载或价格历史增量更新。</div>
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

async function loadScanList() {
  const res = await fetch('api/scans');
  const scans = await res.json();
  const sel = document.getElementById('scanSelect');
  sel.innerHTML = '';
  if (scans.length === 0) {
    sel.innerHTML = '<option>暂无扫描记录</option>';
    document.getElementById('noData').style.display = '';
    document.getElementById('dashboard').style.display = 'none';
    return;
  }
  scans.forEach((s, i) => {
    const opt = document.createElement('option');
    opt.value = s.path;
    opt.textContent = s.date.replace('_', ' ');
    sel.appendChild(opt);
  });
  loadScan(scans[0].path);
}

async function loadScan(path) {
  const res = await fetch('api/scan?path=' + encodeURIComponent(path));
  const data = await res.json();
  if (data.error) {
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
  const confirmation = full
    ? '确认开始全量扫描吗？将扫描约 3,700–3,800 只美股，通常需要 60–90 分钟，期间不能启动其他扫描。'
    : '确认开始测试扫描吗？将扫描股票池前 100 只，通常需要约 2 分钟，测试结果默认不推送 Telegram。';
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
          showToast('扫描完成，正在加载最新结果...');
          await loadScanList();
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


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/scans")
def api_scans():
    return jsonify(get_scan_files())


@app.route("/api/system-info")
def api_system_info():
    return jsonify(SYSTEM_INFO)


@app.route("/api/scan")
def api_scan():
    requested_path = request.args.get("path", "")
    if requested_path:
        path = (SCAN_DIR / requested_path).resolve()
        scan_root = SCAN_DIR.resolve()
        if path.parent != scan_root or not path.name.startswith("optimized_scan_") or path.suffix != ".txt":
            return jsonify({"error": "Invalid scan path"}), 400
    else:
        path = (SCAN_DIR / "latest_optimized_scan.txt").resolve()

    if not path.exists():
        return jsonify({"error": "File not found"})

    return jsonify(parse_scan_file(path))


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


def open_browser(port):
    webbrowser.open(f"http://localhost:{port}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stock Screener Dashboard")
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
