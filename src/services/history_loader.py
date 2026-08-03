"""DB-first K-line history loader for Agent tools.

Provides:
- ContextVar-based frozen target_date propagation across threads
- ``load_history_df``: read from DB first, DataFetcherManager fallback
- Real-time quote integration during trading hours

Fixes #1066 – eliminates 45+ redundant HTTP requests per stock in Agent mode.
"""
from __future__ import annotations

import contextvars
import logging
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)
_CACHE_MIN_RECORDS = 30
# 交易时段内跳过缓存的时间范围（A股）
# 上午: 09:15-11:35, 下午: 12:55-15:10
_TRADING_HOURS_START = 9  # 上午开始
_TRADING_HOURS_END = 15   # 下午结束
_LUNCH_START = 11         # 午休开始
_LUNCH_END = 13           # 午休结束

# ---------------------------------------------------------------------------
# Frozen target date (ContextVar) – set once per stock in pipeline, read by
# all agent tool threads via copy_context().run().
# ---------------------------------------------------------------------------
_frozen_target_date: contextvars.ContextVar[Optional[date]] = contextvars.ContextVar(
    "_frozen_target_date", default=None,
)


def set_frozen_target_date(d: date) -> contextvars.Token:
    return _frozen_target_date.set(d)


def get_frozen_target_date() -> Optional[date]:
    return _frozen_target_date.get()


def reset_frozen_target_date(token: contextvars.Token) -> None:
    _frozen_target_date.reset(token)


# ---------------------------------------------------------------------------
# Trading hours detection
# ---------------------------------------------------------------------------
def _is_in_trading_hours(stock_code: str) -> bool:
    """
    Check if we should fetch fresh network data (vs using DB cache).
    
    Returns True on trading days (including after market close), ensuring:
    - During trading hours: Skip cache, fetch fresh data + merge real-time quote
    - After market close (POSTMARKET): Skip cache, fetch fresh data for today's complete bar
    - Only use cache on NON_TRADING days (weekends, holidays)
    
    Returns False only for:
    - NON_TRADING days (weekends, holidays) when we can safely use cached data
    - Unknown phases
    - Non-A shares (HK/US/JP/KR/TW) to preserve existing behavior
    
    Key insight: POSTMARKET on a trading day still needs fresh data because
    today's complete daily bar may not be in the cache yet.
    """
    from src.core.trading_calendar import MarketPhase, infer_market_phase
    
    try:
        # Determine market from stock code
        normalized = str(stock_code or "").strip().upper()
        
        # For non-A shares, preserve existing behavior (use cached data)
        if normalized.startswith("HK") or "." in normalized:
            suffix = normalized.split(".")[-1] if "." in normalized else ""
            if suffix in ("HK", "T", "KS", "KQ", "TW", "TWO") or normalized.startswith("HK"):
                return False
        
        # Get current market phase for A-shares
        market_phase = infer_market_phase("cn")
        
        # On NON_TRADING days (weekends, holidays), we can safely use cached data
        # On UNKNOWN phase (error), be conservative and use cached data
        if market_phase in {MarketPhase.NON_TRADING, MarketPhase.UNKNOWN}:
            logger.debug("_is_in_trading_hours(%s): NON_TRADING/UNKNOWN, using cache", stock_code)
            return False
        
        # On all trading day phases, fetch fresh network data:
        # - PREMARKET: 09:00-09:30 - need fresh data
        # - INTRADAY: 09:30-11:30, 13:00-14:57 - need fresh data + real-time
        # - LUNCH_BREAK: 11:30-13:00 - need fresh data
        # - CLOSING_AUCTION: 14:57-15:00 - need fresh data + real-time
        # - POSTMARKET: 15:00-15:30+ - need fresh data for today's complete bar
        logger.debug("_is_in_trading_hours(%s): Phase=%s, fetching fresh data", stock_code, market_phase)
        return True
    except Exception as e:
        logger.debug("_is_in_trading_hours(%s) failed: %s", stock_code, e)
        return False


def _merge_realtime_into_daily(df: pd.DataFrame, stock_code: str, fetcher_manager: Any) -> pd.DataFrame:
    """
    Merge real-time quote into daily data during trading hours.
    
    This adds or updates the current trading day's bar with real-time
    data when the market is open, ensuring the latest price is used
    for screening/analysis.
    """
    try:
        # Get real-time quote
        quote = fetcher_manager.get_realtime_quote(stock_code)
        if quote is None or quote.price is None or quote.price <= 0:
            return df
        
        # Get current date in exchange timezone
        from src.core.trading_calendar import get_market_now
        market_now = get_market_now("cn")
        today_str = market_now.strftime("%Y-%m-%d")
        
        df = df.copy()
        
        # Convert date column to string for comparison
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date']).dt.strftime("%Y-%m-%d")
        
        # Find or create today's row
        today_mask = df['date'] == today_str
        has_today = today_mask.any()
        
        # Build today's data from real-time quote
        today_data = {
            'code': stock_code,
            'date': today_str,
            'open': quote.open_price or quote.price,
            'high': quote.high or quote.price,
            'low': quote.low or quote.price,
            'close': quote.price,
            'volume': quote.volume or 0,
            'amount': quote.amount or 0,
            'pct_chg': quote.change_pct or 0,
        }
        
        if has_today:
            # Update existing row with real-time data
            for key, value in today_data.items():
                if key in df.columns:
                    df.loc[today_mask, key] = value
            logger.debug(
                "_merge_realtime_into_daily(%s): Updated today's bar with real-time quote",
                stock_code
            )
        else:
            # Append new row for today
            new_row = pd.DataFrame([today_data])
            df = pd.concat([df, new_row], ignore_index=True)
            logger.debug(
                "_merge_realtime_into_daily(%s): Added today's bar from real-time quote",
                stock_code
            )
        
        # Sort by date
        if 'date' in df.columns:
            df = df.sort_values('date', ascending=True).reset_index(drop=True)
        
        return df
    except Exception as e:
        logger.warning(
            "_merge_realtime_into_daily(%s) failed: %s",
            stock_code, e
        )
        return df


# ---------------------------------------------------------------------------
# Internal DataFetcherManager singleton (fallback only)
# ---------------------------------------------------------------------------
_fetcher_singleton = None
_fetcher_lock = Lock()


def _get_fetcher_manager():
    global _fetcher_singleton
    if _fetcher_singleton is None:
        with _fetcher_lock:
            if _fetcher_singleton is None:
                from data_provider import DataFetcherManager
                _fetcher_singleton = DataFetcherManager()
    return _fetcher_singleton


# ---------------------------------------------------------------------------
# DB-first history loader
# ---------------------------------------------------------------------------
def _history_code_candidates(stock_code: str) -> Tuple[List[str], str]:
    from data_provider.base import canonical_stock_code, normalize_stock_code

    raw_code = str(stock_code or "").strip()
    normalized_code = canonical_stock_code(normalize_stock_code(raw_code))
    candidates: List[str] = []
    for candidate in (canonical_stock_code(raw_code), normalized_code):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates, normalized_code


def _coerce_bar_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return date.min
    if hasattr(value, "date"):
        try:
            coerced = value.date()
            return coerced if isinstance(coerced, date) else date.min
        except Exception:
            return date.min
    return date.min


def _bar_date(bar: Any) -> date:
    row_date = _coerce_bar_date(getattr(bar, "date", None))
    if row_date != date.min:
        return row_date
    if hasattr(bar, "to_dict"):
        try:
            return _coerce_bar_date((bar.to_dict() or {}).get("date"))
        except Exception:
            return date.min
    return date.min


def _select_best_bars(db, stock_code: str, start: date, end: date) -> Tuple[Optional[str], list]:
    candidates, normalized_code = _history_code_candidates(stock_code)
    best_code = None
    best_bars = []
    best_key = None

    for candidate in candidates:
        bars = list(db.get_data_range(candidate, start, end) or [])
        if not bars:
            continue
        latest_date = max(_bar_date(bar) for bar in bars)
        key = (latest_date, len(bars), candidate == normalized_code)
        if best_key is None or key > best_key:
            best_key = key
            best_code = candidate
            best_bars = bars

    return best_code, best_bars


def load_history_df(
    stock_code: str,
    days: int = 60,
    target_date: Optional[date] = None,
) -> Tuple[Optional[pd.DataFrame], str]:
    """Load K-line history, DB first with DataFetcherManager fallback.

    During trading hours (09:00-15:30 on trading days):
      - Skips DB cache entirely
      - Fetches fresh data from network
      - Merges real-time quote data for the current trading day

    After market close:
      - First checks DB cache
      - If cache doesn't have today's complete data, fetches from network
      - Ensures always using the latest complete daily data

    Returns ``(df, source)`` where *source* is ``"db_cache"`` on DB hit or the
    actual provider name on network fallback. Returns ``(None, "none")`` when
    both paths fail.
    """
    from src.storage import get_db

    # Determine if we should use real-time data (trading hours for A-shares)
    use_realtime = _is_in_trading_hours(stock_code)

    # Resolve effective end date
    if target_date is not None:
        end = target_date
    else:
        frozen = get_frozen_target_date()
        end = frozen if frozen else date.today()

    # Calendar-day buffer: ~1.8x trading days + margin for long holidays
    start = end - timedelta(days=int(days * 1.8) + 10)

    # --- 1. DB lookup (conditional - depends on trading hours and cache freshness) ------
    cache_can_be_used = False
    if not use_realtime:
        try:
            db = get_db()
            _code, bars = _select_best_bars(db, stock_code, start, end)
            required_records = max(min(days, _CACHE_MIN_RECORDS), 1)
            latest_date = max((_bar_date(bar) for bar in bars), default=date.min)
            
            # Only use cache if it has today's complete data (for post-market)
            # This ensures we always get the latest daily data after market close
            if bars and latest_date >= end and len(bars) >= required_records:
                df = pd.DataFrame([b.to_dict() for b in bars])
                logger.debug(
                    "load_history_df(%s): %d bars from DB (requested %d), latest=%s",
                    stock_code, len(df), days, latest_date,
                )
                cache_can_be_used = True
                return df, "db_cache"
            else:
                logger.debug(
                    "load_history_df(%s): DB cache stale or incomplete (latest=%s, needs=%s), fetching fresh",
                    stock_code, latest_date, end,
                )
        except Exception as e:
            logger.debug("load_history_df(%s): DB read failed: %s", stock_code, e)
    else:
        logger.debug(
            "load_history_df(%s): Skipping DB cache during trading hours",
            stock_code
        )

    # --- 2. Network fetch via singleton DataFetcherManager -------------
    try:
        manager = _get_fetcher_manager()
        df, source = manager.get_daily_data(stock_code, days=days)
        if df is not None and not df.empty:
            # During trading hours, merge real-time quote into daily data
            if use_realtime:
                df = _merge_realtime_into_daily(df, stock_code, manager)
                return df, f"{source}_with_realtime"
            return df, source
    except Exception as e:
        logger.warning("load_history_df(%s): DataFetcherManager failed: %s", stock_code, e)

    return None, "none"
