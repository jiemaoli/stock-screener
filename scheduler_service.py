#!/usr/bin/env python3
"""Persistent, UI-configurable scheduler for production full scans."""

import json
import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).parent.resolve()
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_FILE = DATA_DIR / "config" / "schedule.json"
STATE_FILE = DATA_DIR / "logs" / "scheduler_state.json"
LOG_FILE = DATA_DIR / "logs" / "scheduler.log"
LOCK_FILE = DATA_DIR / ".scan.lock"

DEFAULT_CONFIG = {
    "enabled": True,
    "timezone": "America/New_York",
    "weekdays": [0, 1, 2, 3, 4],
    "hour": 18,
    "minute": 15,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - scheduler - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def load_config():
    config = dict(DEFAULT_CONFIG)
    saved = read_json(CONFIG_FILE, {})
    if isinstance(saved, dict):
        config.update({key: saved[key] for key in DEFAULT_CONFIG if key in saved})
    return config


def scheduled_key(now, config):
    return f"{now.date().isoformat()}-{config['hour']:02d}{config['minute']:02d}"


def should_run(now, config, state):
    if not config.get("enabled", True):
        return False
    if now.weekday() not in set(config.get("weekdays", [])):
        return False
    if now.hour != int(config["hour"]) or now.minute != int(config["minute"]):
        return False
    return state.get("last_scheduled_key") != scheduled_key(now, config)


def run_full_scan(now, config, state):
    key = scheduled_key(now, config)
    state.update(
        {
            "last_scheduled_key": key,
            "status": "running",
            "started_at": now.isoformat(),
        }
    )
    write_json(STATE_FILE, state)

    command = [
        "/usr/bin/flock",
        "-n",
        str(LOCK_FILE),
        "/usr/local/bin/python",
        "run_optimized_scan.py",
        "--conservative",
        "--git-storage",
        "--clear-progress",
    ]
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Starting scheduled full scan for %s", key)
    with LOG_FILE.open("a", encoding="utf-8") as log_file:
        result = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

    state.update(
        {
            "status": "completed" if result.returncode == 0 else "failed",
            "completed_at": datetime.now(ZoneInfo(config["timezone"])).isoformat(),
            "return_code": result.returncode,
        }
    )
    write_json(STATE_FILE, state)
    logger.info("Scheduled scan finished with return code %s", result.returncode)


def main():
    logger.info("UI-configurable scheduler started")
    while True:
        try:
            config = load_config()
            timezone = ZoneInfo(config.get("timezone", "America/New_York"))
            now = datetime.now(timezone)
            state = read_json(STATE_FILE, {})
            if should_run(now, config, state):
                run_full_scan(now, config, state)
        except Exception:
            logger.exception("Scheduler loop failed")
        time.sleep(20)


if __name__ == "__main__":
    main()
