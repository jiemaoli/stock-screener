from pathlib import Path
import sys
import types

import pandas as pd
import pytest

try:
    import yfinance  # noqa: F401
except ModuleNotFoundError:
    # These tests mock network downloads; keep them runnable in lightweight
    # local environments that do not install the production data provider.
    sys.modules["yfinance"] = types.SimpleNamespace()

from src.config.scan_settings import read_scan_settings, write_scan_settings
from src.data.price_history_cache import RollingPriceHistoryCache
from src.data.universe_fetcher import USStockUniverseFetcher


def test_scan_settings_round_trip(tmp_path):
    config_path = tmp_path / "scan_settings.json"
    saved = write_scan_settings(
        {
            "universe_mode": "all_us",
            "result_limit": 35,
            "price_cache_enabled": True,
            "price_cache_full_refresh_days": 30,
        },
        path=config_path,
    )

    assert saved["universe_mode"] == "all_us"
    assert read_scan_settings(config_path) == saved


def test_scan_settings_reject_invalid_limit(tmp_path):
    with pytest.raises(ValueError, match="5–100"):
        write_scan_settings(
            {
                "universe_mode": "sp500_nasdaq100",
                "result_limit": 101,
                "price_cache_enabled": True,
                "price_cache_full_refresh_days": 30,
            },
            path=tmp_path / "scan_settings.json",
        )


def test_index_universe_is_cached_without_replacing_all_us(tmp_path, monkeypatch):
    fetcher = USStockUniverseFetcher(cache_dir=str(tmp_path))
    monkeypatch.setattr(
        fetcher,
        "_fetch_index_constituents",
        lambda: {
            "sp500": ["AAPL", "BRK-B"],
            "nasdaq100": ["AAPL", "MSFT"],
            "sp500_nasdaq100": ["AAPL", "BRK-B", "MSFT"],
        },
    )

    assert fetcher.fetch_by_mode("sp500_nasdaq100") == ["AAPL", "BRK-B", "MSFT"]
    assert fetcher.index_cache_file.exists()
    assert fetcher.cache_file != fetcher.index_cache_file


def test_rolling_price_cache_full_then_incremental(tmp_path, monkeypatch):
    cache = RollingPriceHistoryCache(str(tmp_path), full_refresh_days=30)
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=1300)
    full_data = pd.DataFrame(
        {"Close": range(1300), "Volume": [1_000_000] * 1300, "Stock Splits": [0] * 1300},
        index=dates,
    )
    recent_dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=10)
    recent_data = pd.DataFrame(
        {"Close": range(2000, 2010), "Volume": [1_000_000] * 10, "Stock Splits": [0] * 10},
        index=recent_dates,
    )
    calls = []

    def fake_download(ticker, period):
        calls.append((ticker, period))
        return full_data if period == "5y" else recent_data

    monkeypatch.setattr(cache, "_download", fake_download)

    first = cache.fetch("AAPL")
    second = cache.fetch("AAPL")

    assert calls == [("AAPL", "5y"), ("AAPL", "10d")]
    assert len(first) >= 1250
    assert second.index.is_unique
    assert second.index.is_monotonic_increasing
    assert second.loc[recent_dates[-1], "Close"] == 2009
