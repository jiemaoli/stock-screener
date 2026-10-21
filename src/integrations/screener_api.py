"""Shared screening API envelope used by fin_twt."""

from typing import Any, Dict, List


SCHEMA_VERSION = "1.0"
PROVIDER = {
    "id": "stock-screener",
    "name": "Intelligent Stock Screener",
    "version": "1.0",
}


def _number(value: Any) -> Any:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _candidate(signal: Dict[str, Any], classification: str, generated_at: str, rank: int) -> Dict[str, Any]:
    details = signal.get("details") if isinstance(signal.get("details"), dict) else {}
    is_buy = classification == "buy_candidate"
    stop_loss = _number(signal.get("stop_loss"))
    risks: List[str] = []
    if stop_loss is not None:
        risks.append(f"The setup is invalidated near {stop_loss:.2f}.")
    if not risks:
        risks.append("Price structure can change before the content is reviewed.")
    risks.append("The screening result is a research candidate, not an execution instruction.")
    return {
        "symbol": str(signal.get("ticker") or "").upper(),
        "name": str(signal.get("ticker") or "").upper(),
        "classification": classification,
        "direction": "bullish" if is_buy else "bearish",
        "setup_type": "stage2_breakout" if is_buy else "breakdown",
        "timeframe": "swing",
        "rank": rank,
        "score": _number(signal.get("score")),
        "score_max": 125 if is_buy else 100,
        "price": _number(signal.get("current_price")),
        "data_as_of": generated_at,
        "summary": (
            "Confirmed Stage 2 trend candidate with technical, relative-strength and risk filters."
            if is_buy
            else "Distribution or breakdown candidate requiring risk review."
        ),
        "reasons": [str(item) for item in signal.get("reasons", [])],
        "risks": risks,
        "details": {
            "phase": signal.get("phase"),
            "entry_quality": signal.get("entry_quality"),
            "breakout_price": _number(signal.get("breakout_price")),
            "stop_loss": stop_loss,
            "target": _number(details.get("reward_target")),
            "risk_reward_ratio": _number(signal.get("risk_reward_ratio")),
            "minervini_template_score": _number(signal.get("minervini_template_score")),
            "minervini_criteria_passed": signal.get("minervini_criteria_passed"),
            "severity": signal.get("severity"),
            "breakdown_level": _number(signal.get("breakdown_level")),
            "provider_metrics": details,
        },
    }


def running_envelope(job_id: str, state: Dict[str, Any], status_url: str) -> Dict[str, Any]:
    request_data = state.get("request") if isinstance(state.get("request"), dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": PROVIDER,
        "strategy": "stage2_breakout",
        "run_id": job_id,
        "generated_at": state.get("started_at"),
        "market": "US",
        "status": state.get("status", "running"),
        "status_url": status_url,
        "summary": {
            "candidate_count": 0,
            "requested_limit": request_data.get("result_limit"),
            "ticker_count": request_data.get("ticker_count"),
        },
        "candidates": [],
    }


def completed_envelope(job_id: str, state: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    generated_at = str(result.get("generated_at") or state.get("completed_at") or state.get("started_at") or "")
    buys = list(result.get("buy_signals") or [])
    sells = list(result.get("sell_signals") or [])
    candidates = [
        *[_candidate(signal, "buy_candidate", generated_at, index) for index, signal in enumerate(buys, 1)],
        *[_candidate(signal, "sell_candidate", generated_at, index) for index, signal in enumerate(sells, 1)],
    ]
    statistics = result.get("statistics") if isinstance(result.get("statistics"), dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": PROVIDER,
        "strategy": "stage2_breakout",
        "run_id": job_id,
        "generated_at": generated_at,
        "market": "US",
        "status": "completed",
        "summary": {
            **statistics,
            "candidate_count": len(candidates),
            "buy_candidate_count": len(buys),
            "sell_candidate_count": len(sells),
            "market_regime": result.get("spy_analysis"),
            "breadth": result.get("breadth"),
        },
        "candidates": candidates,
    }


def failed_envelope(job_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    envelope = running_envelope(job_id, state, f"/api/v1/screen/{job_id}")
    envelope["status"] = "failed"
    envelope["error"] = state.get("error") or "Screening failed"
    return envelope
