"""Persistent scan settings shared by the dashboard and scan workers."""

import json
from pathlib import Path
from typing import Any, Dict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "data" / "config" / "scan_settings.json"

DEFAULT_SCAN_SETTINGS = {
    "universe_mode": "sp500_nasdaq100",
    "result_limit": 20,
    "price_cache_enabled": True,
    "price_cache_full_refresh_days": 30,
}

UNIVERSE_MODES = {
    "sp500_nasdaq100": "标普 500 + 纳斯达克 100（并集去重）",
    "sp500": "仅标普 500",
    "nasdaq100": "仅纳斯达克 100",
    "all_us": "全部美股（NASDAQ / NYSE / AMEX）",
}


def validate_scan_settings(value: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize user-editable scan settings."""
    mode = str(value.get("universe_mode", DEFAULT_SCAN_SETTINGS["universe_mode"]))
    if mode not in UNIVERSE_MODES:
        raise ValueError("不支持的股票池模式")

    try:
        result_limit = int(value.get("result_limit", DEFAULT_SCAN_SETTINGS["result_limit"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("结果数量必须是整数") from exc
    if result_limit < 5 or result_limit > 100:
        raise ValueError("结果数量必须在 5–100 之间")

    try:
        refresh_days = int(
            value.get(
                "price_cache_full_refresh_days",
                DEFAULT_SCAN_SETTINGS["price_cache_full_refresh_days"],
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("完整校验周期必须是整数") from exc
    if refresh_days < 7 or refresh_days > 90:
        raise ValueError("完整校验周期必须在 7–90 天之间")

    return {
        "universe_mode": mode,
        "result_limit": result_limit,
        "price_cache_enabled": value.get("price_cache_enabled", True) is True,
        "price_cache_full_refresh_days": refresh_days,
    }


def read_scan_settings(path: Path = CONFIG_FILE) -> Dict[str, Any]:
    """Read settings, retaining safe defaults for missing/invalid files."""
    settings = dict(DEFAULT_SCAN_SETTINGS)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            settings.update({key: saved[key] for key in DEFAULT_SCAN_SETTINGS if key in saved})
        return validate_scan_settings(settings)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return dict(DEFAULT_SCAN_SETTINGS)


def write_scan_settings(value: Dict[str, Any], path: Path = CONFIG_FILE) -> Dict[str, Any]:
    """Validate and atomically persist settings."""
    settings = validate_scan_settings(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)
    return settings
