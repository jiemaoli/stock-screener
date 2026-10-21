"""Telegram notifications for optimized stock scan results."""

import html
import logging
import os
from pathlib import Path
from typing import Dict, List

import requests
from dotenv import load_dotenv

from src.screening.benchmark import classify_market_regime

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Send concise buy/sell ticker summaries through a Telegram bot."""

    def __init__(self):
        load_dotenv()
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.notify_test = os.getenv("TELEGRAM_NOTIFY_TEST", "false").lower() == "true"

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def _send_message(self, text: str) -> bool:
        response = requests.post(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        response.raise_for_status()
        return True

    @staticmethod
    def _ticker_lines(label: str, signals: List[Dict]) -> List[str]:
        if not signals:
            return [f"<b>{label} (0)</b>: none"]

        tickers = [html.escape(str(signal.get("ticker", "?"))) for signal in signals]
        lines = [f"<b>{label} ({len(tickers)})</b>"]
        current = []
        current_length = 0
        for ticker in tickers:
            added = len(ticker) + (2 if current else 0)
            if current and current_length + added > 800:
                lines.append(", ".join(current))
                current = []
                current_length = 0
            current.append(ticker)
            current_length += added
        if current:
            lines.append(", ".join(current))
        return lines

    def send_scan_summary(
        self,
        buy_signals: List[Dict],
        sell_signals: List[Dict],
        spy_analysis: Dict,
        breadth: Dict,
        report_path: Path,
        is_test: bool = False,
        result_limit: int = 20,
    ) -> bool:
        if not self.configured:
            logger.info("Telegram notifications disabled: bot token/chat ID not configured")
            return False
        if is_test and not self.notify_test:
            logger.info("Telegram notification skipped for test scan")
            return False

        regime = html.escape(classify_market_regime(spy_analysis, breadth))
        title = "TEST SCAN" if is_test else "DAILY FULL SCAN"
        lines = [
            f"<b>Stock Screener · {title}</b>",
            f"Market: {regime}",
            f"SPY Phase: {spy_analysis.get('phase', '?')}",
            f"Phase 2 breadth: {breadth.get('phase_2_pct', 0):.1f}%",
            "",
        ]
        lines.extend(self._ticker_lines(f"BUY Top {result_limit}", buy_signals[:result_limit]))
        if len(buy_signals) > result_limit:
            lines.append(f"符合条件共 {len(buy_signals)} 个")
        lines.append("")
        lines.extend(self._ticker_lines(f"SELL Top {result_limit}", sell_signals[:result_limit]))
        if len(sell_signals) > result_limit:
            lines.append(f"符合条件共 {len(sell_signals)} 个")
        lines.extend(["", f"Report: <code>{html.escape(Path(report_path).name)}</code>"])

        chunks = []
        current = ""
        for line in lines:
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) > 3500:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)

        try:
            for chunk in chunks:
                self._send_message(chunk)
            logger.info("Telegram scan summary sent (%d message(s))", len(chunks))
            return True
        except requests.RequestException as exc:
            logger.error("Telegram notification failed: %s", exc)
            return False
