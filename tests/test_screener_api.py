from src.integrations.screener_api import completed_envelope, running_envelope


def test_running_envelope_uses_shared_contract():
    result = running_envelope(
        "abc",
        {"status": "running", "started_at": "2026-10-22T00:00:00Z", "request": {"result_limit": 5}},
        "/api/v1/screen/abc",
    )
    assert result["schema_version"] == "1.0"
    assert result["provider"]["id"] == "stock-screener"
    assert result["candidates"] == []


def test_completed_envelope_maps_buy_signal():
    result = completed_envelope(
        "abc",
        {"status": "completed"},
        {
            "generated_at": "2026-10-22T00:00:00Z",
            "statistics": {"total_analyzed": 100},
            "buy_signals": [
                {
                    "ticker": "NVDA",
                    "current_price": 190,
                    "score": 90,
                    "phase": 2,
                    "stop_loss": 175,
                    "risk_reward_ratio": 2.5,
                    "reasons": ["Strong relative strength"],
                    "details": {"reward_target": 225},
                }
            ],
            "sell_signals": [],
        },
    )
    candidate = result["candidates"][0]
    assert candidate["classification"] == "buy_candidate"
    assert candidate["setup_type"] == "stage2_breakout"
    assert candidate["details"]["stop_loss"] == 175.0
