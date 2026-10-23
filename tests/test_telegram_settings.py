import sys
import types

import pytest

try:
    import yfinance  # noqa: F401
except ModuleNotFoundError:
    sys.modules["yfinance"] = types.SimpleNamespace()

from src.config.telegram_settings import (
    public_telegram_settings,
    read_telegram_settings,
    write_telegram_settings,
)


VALID_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


def test_telegram_settings_round_trip_and_hide_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    path = tmp_path / "telegram.json"

    saved = write_telegram_settings(
        {
            "enabled": True,
            "bot_token": VALID_TOKEN,
            "chat_id": "-1001234567890",
            "notify_full": True,
            "notify_test": False,
        },
        path=path,
    )

    assert read_telegram_settings(path) == saved
    public = public_telegram_settings(saved)
    assert public["bot_token_configured"] is True
    assert "bot_token" not in public
    authenticated = public_telegram_settings(saved, include_token=True)
    assert authenticated["bot_token"] == VALID_TOKEN


def test_telegram_settings_fall_back_to_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", VALID_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100999")
    monkeypatch.setenv("TELEGRAM_NOTIFY_FULL", "true")
    monkeypatch.setenv("TELEGRAM_NOTIFY_TEST", "true")

    settings = read_telegram_settings(tmp_path / "missing.json")

    assert settings["bot_token"] == VALID_TOKEN
    assert settings["chat_id"] == "-100999"
    assert settings["notify_test"] is True


def test_telegram_settings_reject_incomplete_enabled_config(tmp_path):
    with pytest.raises(ValueError, match="必须配置"):
        write_telegram_settings(
            {
                "enabled": True,
                "bot_token": "",
                "chat_id": "",
                "notify_full": True,
                "notify_test": False,
            },
            path=tmp_path / "telegram.json",
        )


def test_notifier_honors_full_and_test_switches(monkeypatch):
    import src.notifications.telegram_notifier as notifier_module

    settings = {
        "enabled": True,
        "bot_token": VALID_TOKEN,
        "chat_id": "-100123",
        "notify_full": False,
        "notify_test": True,
    }
    monkeypatch.setattr(notifier_module, "read_telegram_settings", lambda: settings)
    notifier = notifier_module.TelegramNotifier()
    sent = []
    monkeypatch.setattr(notifier, "_send_message", lambda text: sent.append(text) or True)

    common = {
        "buy_signals": [],
        "sell_signals": [],
        "spy_analysis": {},
        "breadth": {},
        "report_path": "report.txt",
        "result_limit": 20,
    }
    assert notifier.send_scan_summary(**common, is_test=False) is False
    assert sent == []
    assert notifier.send_scan_summary(**common, is_test=True) is True
    assert len(sent) == 1


def test_dashboard_telegram_update_keeps_existing_token(monkeypatch):
    import dashboard

    existing = {
        "enabled": True,
        "bot_token": VALID_TOKEN,
        "chat_id": "-100123",
        "notify_full": True,
        "notify_test": False,
    }
    saved = []
    monkeypatch.setattr(dashboard, "read_telegram_settings", lambda: dict(existing))
    monkeypatch.setattr(
        dashboard,
        "write_telegram_settings",
        lambda settings: saved.append(dict(settings)) or settings,
    )
    monkeypatch.setattr(
        dashboard,
        "_build_system_info",
        lambda: {"telegram": public_telegram_settings(existing)},
    )

    client = dashboard.app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["csrf_token"] = "csrf-test"

    response = client.put(
        "/api/telegram-settings",
        headers={"X-CSRF-Token": "csrf-test"},
        json={
            "enabled": True,
            "bot_token": "",
            "chat_id": "-100456",
            "notify_full": True,
            "notify_test": True,
        },
    )

    assert response.status_code == 200
    assert saved[0]["bot_token"] == VALID_TOKEN
    assert saved[0]["chat_id"] == "-100456"
    assert "bot_token" not in response.get_json()["telegram"]


def test_dashboard_contains_token_reveal_control():
    import dashboard

    assert 'id="telegramTokenToggle"' in dashboard.DASHBOARD_HTML
    assert "function toggleTelegramToken()" in dashboard.DASHBOARD_HTML
    assert "telegram.bot_token || ''" in dashboard.DASHBOARD_HTML
