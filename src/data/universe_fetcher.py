"""Fetch and maintain the universe of all publicly traded US stocks.

This module fetches the complete list of US-listed stocks from multiple sources
and maintains a daily-updated universe for screening.
"""

import logging
import pickle
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Dict, List, Set

import pandas as pd
import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class USStockUniverseFetcher:
    """Fetches and maintains the universe of all US-listed stocks."""

    def __init__(self, cache_dir: str = "./data/cache"):
        """Initialize the universe fetcher.

        Args:
            cache_dir: Directory for caching universe data
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / "us_stock_universe.pkl"
        self.index_cache_file = self.cache_dir / "index_stock_universes.pkl"
        logger.info("USStockUniverseFetcher initialized")

    @staticmethod
    def _normalize_index_symbols(symbols) -> List[str]:
        """Normalize index symbols to Yahoo Finance ticker notation."""
        normalized = {
            str(symbol).strip().upper().replace(".", "-")
            for symbol in symbols
            if str(symbol).strip()
        }
        return sorted(symbol for symbol in normalized if symbol != "NAN")

    def _fetch_index_constituents(self) -> Dict[str, List[str]]:
        """Fetch current S&P 500 and Nasdaq-100 constituent lists."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (compatible; stock-screener/1.0; "
                "+https://github.com/jiemaoli/my-stock-screener)"
            )
        }

        sp_response = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers=headers,
            timeout=30,
        )
        sp_response.raise_for_status()
        sp_tables = pd.read_html(StringIO(sp_response.text))
        sp500 = self._normalize_index_symbols(sp_tables[0]["Symbol"].tolist())

        ndx_response = requests.get(
            "https://en.wikipedia.org/wiki/Nasdaq-100",
            headers=headers,
            timeout=30,
        )
        ndx_response.raise_for_status()
        ndx_tables = pd.read_html(StringIO(ndx_response.text))
        constituent_table = next(
            table for table in ndx_tables
            if "Ticker" in table.columns and len(table) >= 90
        )
        nasdaq100 = self._normalize_index_symbols(constituent_table["Ticker"].tolist())

        if len(sp500) < 450 or len(nasdaq100) < 90:
            raise ValueError(
                f"Unexpected index sizes: S&P 500={len(sp500)}, Nasdaq-100={len(nasdaq100)}"
            )

        return {
            "sp500": sp500,
            "nasdaq100": nasdaq100,
            "sp500_nasdaq100": sorted(set(sp500) | set(nasdaq100)),
        }

    def fetch_index_universe(self, mode: str, force_refresh: bool = False) -> List[str]:
        """Return a cached or freshly downloaded large-cap index universe."""
        if mode not in {"sp500", "nasdaq100", "sp500_nasdaq100"}:
            raise ValueError(f"Unsupported index universe mode: {mode}")

        cached_data = None
        if self.index_cache_file.exists():
            try:
                with self.index_cache_file.open("rb") as cache_file:
                    cached_data = pickle.load(cache_file)
            except (OSError, pickle.PickleError, EOFError, TypeError):
                logger.warning("Index universe cache is invalid; fetching a fresh copy")

        cache_is_fresh = False
        if cached_data:
            try:
                fetched_at = datetime.fromisoformat(cached_data["fetch_date"])
                cache_is_fresh = datetime.now() - fetched_at < timedelta(days=1)
            except (KeyError, TypeError, ValueError):
                pass

        if cached_data and cache_is_fresh and not force_refresh:
            symbols = cached_data.get(mode, [])
            if symbols:
                logger.info("Loaded %d %s symbols from cache", len(symbols), mode)
                return symbols

        try:
            index_data = self._fetch_index_constituents()
            cache_data = {
                **index_data,
                "fetch_date": datetime.now().isoformat(),
            }
            temp_file = self.index_cache_file.with_suffix(".tmp")
            with temp_file.open("wb") as cache_file:
                pickle.dump(cache_data, cache_file)
            temp_file.replace(self.index_cache_file)
            logger.info(
                "Fetched index universes: S&P 500=%d, Nasdaq-100=%d, union=%d",
                len(index_data["sp500"]),
                len(index_data["nasdaq100"]),
                len(index_data["sp500_nasdaq100"]),
            )
            return index_data[mode]
        except Exception as exc:
            if cached_data and cached_data.get(mode):
                logger.warning(
                    "Failed to refresh %s universe (%s); using stale cache with %d symbols",
                    mode,
                    exc,
                    len(cached_data[mode]),
                )
                return cached_data[mode]
            raise RuntimeError(f"Failed to fetch {mode} universe and no cache is available") from exc

    def fetch_by_mode(self, mode: str, force_refresh: bool = False) -> List[str]:
        """Fetch a configured universe without removing the legacy all-US option."""
        if mode == "all_us":
            return self.fetch_universe(force_refresh=force_refresh)
        return self.fetch_index_universe(mode, force_refresh=force_refresh)

    def _fetch_from_fmp(self) -> List[Dict]:
        """Fetch stock list from Financial Modeling Prep (free tier).

        Note: This requires a free API key from financialmodelingprep.com
        Falls back to other sources if not available.
        """
        # This is a fallback - will use other sources
        return []

    def _fetch_nasdaq_listed(self) -> pd.DataFrame:
        """Fetch NASDAQ-listed stocks from NASDAQ FTP.

        Returns:
            DataFrame with NASDAQ stocks
        """
        try:
            url = "ftp://ftp.nasdaqtrader.com/symboldirectory/nasdaqlisted.txt"
            df = pd.read_csv(url, sep='|')
            df = df[df['Symbol'].notna()]
            df = df[df['Test Issue'] == 'N']  # Exclude test issues
            df = df[['Symbol', 'Security Name']].copy()
            df.columns = ['symbol', 'name']
            logger.info(f"Fetched {len(df)} NASDAQ stocks")
            return df
        except Exception as e:
            logger.error(f"Error fetching NASDAQ stocks: {e}")
            return pd.DataFrame()

    def _fetch_other_listed(self) -> pd.DataFrame:
        """Fetch non-NASDAQ listed stocks (NYSE, AMEX, etc).

        Returns:
            DataFrame with other exchange stocks
        """
        try:
            url = "ftp://ftp.nasdaqtrader.com/symboldirectory/otherlisted.txt"
            df = pd.read_csv(url, sep='|')
            df = df[df['ACT Symbol'].notna()]
            df = df[df['Test Issue'] == 'N']  # Exclude test issues
            df = df[['ACT Symbol', 'Security Name']].copy()
            df.columns = ['symbol', 'name']
            logger.info(f"Fetched {len(df)} NYSE/AMEX stocks")
            return df
        except Exception as e:
            logger.error(f"Error fetching NYSE/AMEX stocks: {e}")
            return pd.DataFrame()

    def _filter_stocks(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter out unwanted tickers.

        Removes:
        - Tickers with special characters ($, ^, ., etc.)
        - Test symbols
        - Warrants, rights, units
        - Preferred shares
        - ETFs and funds (heuristic)

        Args:
            df: DataFrame with symbols

        Returns:
            Filtered DataFrame
        """
        if df.empty:
            return df

        initial_count = len(df)

        # Remove symbols with special characters
        df = df[~df['symbol'].str.contains(r'[\$\^\.\-]', regex=True, na=False)]

        # Remove common suffixes for warrants, rights, units
        suffixes = ['W', 'R', 'U', 'WS', 'WT']
        for suffix in suffixes:
            df = df[~df['symbol'].str.endswith(suffix, na=False)]

        # Remove preferred shares (usually have letters after symbol)
        # Keep only symbols that are 1-5 uppercase letters
        df = df[df['symbol'].str.match(r'^[A-Z]{1,5}$', na=False)]

        # Remove obvious ETFs and funds (heuristic based on name)
        etf_keywords = [
            'ETF', 'FUND', 'TRUST', 'INDEX', 'PORTFOLIO',
            'SHARES', 'NOTES', 'BOND', 'TREASURY'
        ]
        name_upper = df['name'].str.upper()
        for keyword in etf_keywords:
            df = df[~name_upper.str.contains(keyword, na=False)]

        filtered_count = len(df)
        logger.info(f"Filtered {initial_count - filtered_count} stocks, kept {filtered_count}")

        return df

    def fetch_universe(self, force_refresh: bool = False) -> List[str]:
        """Fetch the complete universe of US-listed stocks.

        Args:
            force_refresh: Force refresh even if cached data is recent

        Returns:
            List of stock ticker symbols
        """
        # Check cache
        if not force_refresh and self.cache_file.exists():
            cache_age = datetime.now() - datetime.fromtimestamp(
                self.cache_file.stat().st_mtime
            )

            if cache_age < timedelta(days=1):
                logger.info("Loading universe from cache")
                with open(self.cache_file, 'rb') as f:
                    cached_data = pickle.load(f)
                logger.info(f"Loaded {len(cached_data['symbols'])} symbols from cache")
                return cached_data['symbols']

        logger.info("Fetching fresh universe from exchanges...")

        # Fetch from multiple sources
        nasdaq_df = self._fetch_nasdaq_listed()
        other_df = self._fetch_other_listed()

        # Combine
        all_stocks = pd.concat([nasdaq_df, other_df], ignore_index=True)

        if all_stocks.empty:
            logger.error("Failed to fetch any stocks")
            return []

        # Remove duplicates
        all_stocks = all_stocks.drop_duplicates(subset=['symbol'])

        # Filter unwanted symbols
        all_stocks = self._filter_stocks(all_stocks)

        # Sort by symbol
        all_stocks = all_stocks.sort_values('symbol').reset_index(drop=True)

        symbols = all_stocks['symbol'].tolist()

        # Cache the results
        cache_data = {
            'symbols': symbols,
            'fetch_date': datetime.now().isoformat(),
            'count': len(symbols),
            'metadata': {
                'nasdaq_count': len(nasdaq_df),
                'other_count': len(other_df),
                'filtered_count': len(symbols)
            }
        }

        with open(self.cache_file, 'wb') as f:
            pickle.dump(cache_data, f)

        logger.info(f"Cached {len(symbols)} symbols")
        logger.info(f"Universe composition: {cache_data['metadata']}")

        return symbols

    def get_universe_info(self) -> Dict:
        """Get information about the cached universe.

        Returns:
            Dict with universe metadata
        """
        if not self.cache_file.exists():
            return {
                'cached': False,
                'count': 0
            }

        with open(self.cache_file, 'rb') as f:
            cached_data = pickle.load(f)

        cache_age = datetime.now() - datetime.fromtimestamp(
            self.cache_file.stat().st_mtime
        )

        return {
            'cached': True,
            'count': cached_data['count'],
            'fetch_date': cached_data['fetch_date'],
            'cache_age_hours': cache_age.total_seconds() / 3600,
            'metadata': cached_data.get('metadata', {})
        }
