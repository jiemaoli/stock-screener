"""Persistent Telegram notification settings with environment fallback."""

import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).parents[2]
DEFAULT_PATH = PROJECT_ROOT / "data" / "config" / "telegram.json"


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _environment_defaults() -> dict:
    load_dotenv(PROJECT_ROOT / ".env")
    return {
        "enabled": _env_bool("TELEGRAM_ENABLED", True),
        "bot_token": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        "chat_id": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        "notify_full": _env_bool("TELEGRAM_NOTIFY_FULL", True),
        "notify_test": _env_bool("TELEGRAM_NOTIFY_TEST", False),
    }


def read_telegram_settings(path: Path = DEFAULT_PATH) -> dict:
    """Read persistent settings, falling back to the existing .env values."""
    settings = _environment_defaults()
    try:
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            settings.update({key: saved[key] for key in settings if key in saved})
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return settings


def validate_telegram_settings(settings: dict) -> dict:
    """Normalize and validate settings before they are persisted."""
    normalized = {
        "enabled": settings.get("enabled") is True,
        "bot_token": str(settings.get("bot_token", "")).strip(),
        "chat_id": str(settings.get("chat_id", "")).strip(),
        "notify_full": settings.get("notify_full") is True,
        "notify_test": settings.get("notify_test") is True,
    }

    token = normalized["bot_token"]
    if token and not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,}", token):
        raise ValueError("Bot Token 格式不正确")
    if len(normalized["chat_id"]) > 100:
        raise ValueError("Chat ID 长度不能超过 100 个字符")
    if normalized["enabled"] and (not token or not normalized["chat_id"]):
        raise ValueError("启用 Telegram 推送时必须配置 Bot Token 和 Chat ID")
    if not normalized["notify_full"] and not normalized["notify_test"]:
        normalized["enabled"] = False
    return normalized


def write_telegram_settings(settings: dict, path: Path = DEFAULT_PATH) -> dict:
    """Validate and atomically persist Telegram settings."""
    normalized = validate_telegram_settings(settings)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:
        os.chmod(temp_path, 0o600)
    except OSError:
        pass
    temp_path.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return normalized


def public_telegram_settings(settings: dict | None = None) -> dict:
    """Return UI-safe settings without exposing the bot token."""
    settings = settings or read_telegram_settings()
    return {
        "enabled": settings["enabled"],
        "chat_id": settings["chat_id"],
        "notify_full": settings["notify_full"],
        "notify_test": settings["notify_test"],
        "bot_token_configured": bool(settings["bot_token"]),
    }
