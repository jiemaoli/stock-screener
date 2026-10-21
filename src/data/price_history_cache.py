"""Rolling five-year Yahoo Finance price cache.

The first request stores five years of adjusted daily history. Normal daily
runs fetch only a short overlapping window, merge it atomically, and retain a
rolling five-year history. A periodic full refresh repairs historical changes
caused by splits, adjustments, or upstream corrections.
"""

import logging
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


class RollingPriceHistoryCache:
    """Persist and incrementally refresh adjusted daily price history."""

    def __init__(
        self,
        cache_dir: str = "./data/cache/price_history_5y",
        full_refresh_days: int = 30,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.full_refresh_days = full_refresh_days

    def _cache_path(self, ticker: str) -> Path:
        safe_ticker = ticker.replace("/", "-").replace("\\", "-")
        return self.cache_dir / f"{safe_ticker}.pkl"

    @staticmethod
    def _normalize(data: pd.DataFrame) -> pd.DataFrame:
        if data is None or data.empty:
            return pd.DataFrame()
        normalized = data.copy()
        normalized.index = pd.to_datetime(normalized.index)
        if normalized.index.tz is not None:
            normalized.index = normalized.index.tz_localize(None)
        normalized.index.name = "Date"
        normalized = normalized[~normalized.index.duplicated(keep="last")]
        return normalized.sort_index()

    def _load(self, ticker: str) -> Optional[dict]:
        path = self._cache_path(ticker)
        if not path.exists():
            return None
        try:
            with path.open("rb") as cache_file:
                cached = pickle.load(cache_file)
            if not isinstance(cached, dict) or "data" not in cached:
                raise ValueError("unsupported cache format")
            cached["data"] = self._normalize(cached["data"])
            if cached["data"].empty:
                raise ValueError("empty cache")
            return cached
        except Exception as exc:
            logger.warning("%s: ignoring invalid price cache: %s", ticker, exc)
            return None

    def _save(self, ticker: str, data: pd.DataFrame, full_refreshed_at: str) -> None:
        path = self._cache_path(ticker)
        temp_path = path.with_suffix(".tmp")
        payload = {
            "data": self._normalize(data),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "full_refreshed_at": full_refreshed_at,
        }
        with temp_path.open("wb") as cache_file:
            pickle.dump(payload, cache_file, protocol=pickle.HIGHEST_PROTOCOL)
        temp_path.replace(path)

    @staticmethod
    def _needs_full_refresh(cached: Optional[dict], full_refresh_days: int) -> bool:
        if not cached:
            return True
        try:
            refreshed_at = datetime.fromisoformat(cached["full_refreshed_at"])
            if refreshed_at.tzinfo is None:
                refreshed_at = refreshed_at.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) - refreshed_at >= timedelta(days=full_refresh_days)
        except (KeyError, TypeError, ValueError):
            return True

    @staticmethod
    def _trim_to_five_years(data: pd.DataFrame) -> pd.DataFrame:
        if data.empty:
            return data
        # Keep a small buffer so calendar boundaries never remove a needed
        # trading session before the next monthly full validation.
        cutoff = data.index.max() - pd.DateOffset(years=5) - pd.Timedelta(days=10)
        return data.loc[data.index >= cutoff]

    @staticmethod
    def _download(ticker: str, period: str) -> pd.DataFrame:
        return yf.Ticker(ticker).history(
            period=period,
            interval="1d",
            auto_adjust=True,
            actions=True,
        )

    def fetch(self, ticker: str) -> pd.DataFrame:
        """Return rolling five-year history, using an incremental update when safe."""
        cached = self._load(ticker)
        needs_full = self._needs_full_refresh(cached, self.full_refresh_days)

        if needs_full:
            try:
                full_data = self._normalize(self._download(ticker, "5y"))
                if full_data.empty:
                    raise ValueError("full download returned no rows")
                full_refreshed_at = datetime.now(timezone.utc).isoformat()
                full_data = self._trim_to_five_years(full_data)
                self._save(ticker, full_data, full_refreshed_at)
                return full_data
            except Exception:
                if cached:
                    logger.exception("%s: full refresh failed; using cached history", ticker)
                    return cached["data"]
                raise

        cached_data = cached["data"]
        latest_date = cached_data.index.max().date()
        calendar_gap = (datetime.now(timezone.utc).date() - latest_date).days
        incremental_period = "10d" if calendar_gap <= 10 else "1mo"

        try:
            new_data = self._normalize(self._download(ticker, incremental_period))
            if new_data.empty:
                logger.warning("%s: incremental update returned no rows; using cache", ticker)
                return cached_data

            # A split can change the adjusted value of the complete history.
            # Refresh immediately instead of joining incompatible price bases.
            split_rows = pd.DataFrame()
            if "Stock Splits" in new_data.columns:
                split_rows = new_data.loc[new_data["Stock Splits"].fillna(0) != 0]
            full_refresh_date = datetime.fromisoformat(cached["full_refreshed_at"]).date()
            has_new_split = (
                not split_rows.empty
                and any(split_date.date() >= full_refresh_date for split_date in split_rows.index)
            )
            if has_new_split:
                full_data = self._normalize(self._download(ticker, "5y"))
                if not full_data.empty:
                    full_refreshed_at = datetime.now(timezone.utc).isoformat()
                    full_data = self._trim_to_five_years(full_data)
                    self._save(ticker, full_data, full_refreshed_at)
                    return full_data

            merged = pd.concat([cached_data, new_data], axis=0)
            merged = self._trim_to_five_years(self._normalize(merged))
            self._save(ticker, merged, cached["full_refreshed_at"])
            return merged
        except Exception:
            logger.exception("%s: incremental update failed; using cached history", ticker)
            return cached_data

