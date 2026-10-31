# -*- coding: utf-8 -*-
"""
Global Capital Flow Intelligence (GCFI)
Core Data Collector & Capital Flow Calculation Engine
"""

import warnings
warnings.filterwarnings("ignore")

import logging
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, date
from typing import Dict, List, Tuple, Any

from gcfi.gcfi_config import (
    ETF_UNIVERSE,
    MACRO_UNIVERSE,
    DEFAULT_HISTORY_PERIOD,
    FLOW_WINDOW_SHORT,
    FLOW_WINDOW_LONG,
)
from gcfi.db import (
    get_connection,
    save_etf_flow_records,
    save_macro_metrics,
)

logger = logging.getLogger("GCFI.Collector")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

class CapitalFlowCollector:
    """Collects market data, computes ETF flows, and stores time-series metrics."""

    def __init__(self, period: str = DEFAULT_HISTORY_PERIOD):
        self.period = period
        self.etf_tickers = list(ETF_UNIVERSE.keys())
        self.macro_tickers = list(MACRO_UNIVERSE.keys())

    def fetch_etf_snapshots(self) -> Dict[str, Dict[str, Any]]:
        """
        Fetch latest real point-in-time ETF fundamental metrics:
        Shares Outstanding, Total Assets (AUM), NAV, and Previous Close.
        """
        logger.info(f"Fetching point-in-time fundamental snapshots for {len(self.etf_tickers)} ETFs...")
        snapshots = {}
        for ticker in self.etf_tickers:
            try:
                obj = yf.Ticker(ticker)
                info = obj.info
                shares = info.get("sharesOutstanding")
                aum = info.get("totalAssets")
                nav = info.get("navPrice")
                prev_close = info.get("previousClose")

                # If sharesOutstanding is null in info, calculate implied shares if AUM & NAV exist
                is_implied = False
                if (shares is None or shares <= 0) and aum and nav and nav > 0:
                    shares = int(aum / nav)
                    is_implied = True

                snapshots[ticker] = {
                    "shares": shares,
                    "aum": aum,
                    "nav": nav,
                    "prev_close": prev_close,
                    "is_implied": is_implied,
                    "timestamp": datetime.now().isoformat(),
                }
                logger.info(
                    f"[{ticker:5}] Shares: {str(shares):>12} {'(Implied)' if is_implied else '(Reported)'} | "
                    f"AUM: ${aum/1e9:6.2f}B | NAV: ${nav if nav else 0:6.2f}" if aum and nav else f"[{ticker:5}] Data received."
                )
            except Exception as e:
                logger.warning(f"Failed to fetch info snapshot for {ticker}: {e}")
                snapshots[ticker] = {
                    "shares": None,
                    "aum": None,
                    "nav": None,
                    "prev_close": None,
                    "is_implied": False,
                    "timestamp": datetime.now().isoformat(),
                }
        return snapshots

    def fetch_historical_ohlcv(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Fetch historical daily OHLCV for all ETFs and Macro symbols.
        Returns (etf_df, macro_df).
        """
        logger.info(f"Downloading historical daily data (period={self.period})...")
        all_tickers = self.etf_tickers + self.macro_tickers
        raw_df = yf.download(all_tickers, period=self.period, interval="1d", progress=False)

        # Split into ETF and Macro
        etf_df = pd.DataFrame()
        macro_df = pd.DataFrame()

        # Handle MultiIndex columns (Price, Ticker)
        for col_name in ["Open", "High", "Low", "Close", "Volume"]:
            if col_name in raw_df.columns.levels[0]:
                sub_df = raw_df[col_name]
                for t in self.etf_tickers:
                    if t in sub_df.columns:
                        etf_df[(col_name.lower(), t)] = sub_df[t]
                for m in self.macro_tickers:
                    if m in sub_df.columns:
                        macro_df[(col_name.lower(), m)] = sub_df[m]

        etf_df.columns = pd.MultiIndex.from_tuples(etf_df.columns)
        macro_df.columns = pd.MultiIndex.from_tuples(macro_df.columns)

        return etf_df.dropna(how="all"), macro_df.dropna(how="all")

    def process_and_store_flows(self, etf_df: pd.DataFrame, snapshots: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Compute daily capital flows, rolling metrics, and store in database.
        Strictly distinguishes MEASURED (creation/redemption) vs ESTIMATED (CMF order flow proxy).
        """
        logger.info("Computing daily ETF capital flows and flow momentum...")
        conn = get_connection()
        all_records = []

        dates = [d.strftime("%Y-%m-%d") for d in etf_df.index]
        last_date = dates[-1]

        for ticker in self.etf_tickers:
            try:
                close_series = etf_df[("close", ticker)].ffill().bfill()
                high_series = etf_df[("high", ticker)].ffill().bfill()
                low_series = etf_df[("low", ticker)].ffill().bfill()
                open_series = etf_df[("open", ticker)].ffill().bfill()
                vol_series = etf_df[("volume", ticker)].fillna(0)

                # Fetch past historical shares stored in DB to check for real creation/redemption
                db_rows = conn.execute(
                    "SELECT date, shares_outstanding, nav, aum FROM daily_etf_flows WHERE ticker = ? ORDER BY date ASC",
                    (ticker,),
                ).fetchall()
                db_history = {row["date"]: dict(row) for row in db_rows}

                # Calculate returns
                ret_1d = close_series.pct_change(1)
                ret_5d = close_series.pct_change(5)
                ret_20d = close_series.pct_change(20)

                # Calculate Chaikin Money Flow (CMF) Dollar Volume Multiplier
                # Multiplier = ((Close - Low) - (High - Close)) / (High - Low)
                hl_range = high_series - low_series
                hl_range = hl_range.replace(0, np.nan)
                cmf_multiplier = ((close_series - low_series) - (high_series - close_series)) / hl_range
                cmf_multiplier = cmf_multiplier.fillna(0.0).clip(-1.0, 1.0)
                dollar_volume = vol_series * close_series

                # Historical Estimated Net Flow ($)
                # Proxy represents institutional net capital flow pressure in millions of dollars
                est_net_flow = cmf_multiplier * dollar_volume * 0.20  # ~20% institutional creation/order-flow participation

                ticker_snapshot = snapshots.get(ticker, {})
                current_shares = ticker_snapshot.get("shares")
                current_aum = ticker_snapshot.get("aum")
                current_nav = ticker_snapshot.get("nav") or float(close_series.iloc[-1])

                # Construct time-series records for this ticker
                ticker_records = []
                for i, dt in enumerate(dates):
                    c = float(close_series.iloc[i])
                    o = float(open_series.iloc[i])
                    h = float(high_series.iloc[i])
                    l = float(low_series.iloc[i])
                    v = int(vol_series.iloc[i])
                    r1 = float(ret_1d.iloc[i]) if not np.isnan(ret_1d.iloc[i]) else 0.0
                    r5 = float(ret_5d.iloc[i]) if not np.isnan(ret_5d.iloc[i]) else 0.0
                    r20 = float(ret_20d.iloc[i]) if not np.isnan(ret_20d.iloc[i]) else 0.0

                    # Check if today is the latest date and we have snapshot data
                    is_latest = (dt == last_date)
                    shares_val = current_shares if is_latest else (db_history.get(dt, {}).get("shares_outstanding"))
                    nav_val = current_nav if is_latest else (db_history.get(dt, {}).get("nav") or c)
                    aum_val = current_aum if is_latest else (db_history.get(dt, {}).get("aum"))

                    # Determine Flow & Flow Type
                    flow_type = "ESTIMATED_CMF"
                    flow_val = float(est_net_flow.iloc[i])

                    # If we have consecutive measured shares in DB:
                    prev_dt = dates[i - 1] if i > 0 else None
                    prev_shares = db_history.get(prev_dt, {}).get("shares_outstanding") if prev_dt else None

                    if shares_val is not None and prev_shares is not None and shares_val > 0 and prev_shares > 0:
                        delta_shares = shares_val - prev_shares
                        flow_val = float(delta_shares * nav_val)
                        flow_type = "MEASURED"

                    ticker_records.append({
                        "date": dt,
                        "ticker": ticker,
                        "open": round(o, 4),
                        "high": round(h, 4),
                        "low": round(l, 4),
                        "close": round(c, 4),
                        "volume": v,
                        "shares_outstanding": shares_val,
                        "nav": round(nav_val, 4) if nav_val else None,
                        "aum": round(aum_val, 2) if aum_val else None,
                        "net_flow": round(flow_val, 2),
                        "flow_type": flow_type,
                        "return_1d": round(r1, 6),
                        "return_5d": round(r5, 6),
                        "return_20d": round(r20, 6),
                    })

                # Compute rolling net flows (5-day, 20-day) and flow acceleration
                flow_series = pd.Series([r["net_flow"] for r in ticker_records])
                nf5_series = flow_series.rolling(FLOW_WINDOW_SHORT, min_periods=1).sum()
                nf20_series = flow_series.rolling(FLOW_WINDOW_LONG, min_periods=1).sum()
                # Flow Acceleration: (NF5 / 5) - (NF20 / 20)
                fa_series = (nf5_series / FLOW_WINDOW_SHORT) - (nf20_series / FLOW_WINDOW_LONG)

                for i, r in enumerate(ticker_records):
                    r["net_flow_5d"] = round(float(nf5_series.iloc[i]), 2)
                    r["net_flow_20d"] = round(float(nf20_series.iloc[i]), 2)
                    r["flow_acceleration"] = round(float(fa_series.iloc[i]), 2)

                all_records.extend(ticker_records)

            except Exception as e:
                logger.error(f"Error processing flows for {ticker}: {e}", exc_info=True)

        conn.close()

        # Batch save to SQLite
        if all_records:
            save_etf_flow_records(all_records)
            logger.info(f"Saved {len(all_records)} daily ETF flow records into SQLite DB.")
        return all_records

    def process_and_store_macro(self, macro_df: pd.DataFrame, etf_df: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        Process macro variables:
        - 10Y Treasury Yield (^TNX)
        - VIX Volatility (^VIX)
        - Dollar Index (DX-Y.NYB)
        - WTI Crude Oil (CL=F)
        - High Yield Spread Proxy (HYG / TLT relative return or price ratio)
        - Yield Curve Slope Proxy (TLT / SHY ratio)
        """
        logger.info("Computing macro metrics and credit/curve proxies...")
        macro_records = []

        # Align macro_df and etf_df to common dates
        common_idx = etf_df.index.intersection(macro_df.index)
        if common_idx.empty:
            common_idx = etf_df.index
        macro_aligned = macro_df.reindex(common_idx).ffill().bfill()
        etf_aligned = etf_df.reindex(common_idx).ffill().bfill()

        dates = [d.strftime("%Y-%m-%d") for d in common_idx]

        tnx_close = macro_aligned[("close", "^TNX")].ffill().bfill() if ("close", "^TNX") in macro_aligned.columns else pd.Series(0, index=common_idx)
        vix_close = macro_aligned[("close", "^VIX")].ffill().bfill() if ("close", "^VIX") in macro_aligned.columns else pd.Series(0, index=common_idx)
        dxy_close = macro_aligned[("close", "DX-Y.NYB")].ffill().bfill() if ("close", "DX-Y.NYB") in macro_aligned.columns else pd.Series(0, index=common_idx)
        cl_close = macro_aligned[("close", "CL=F")].ffill().bfill() if ("close", "CL=F") in macro_aligned.columns else pd.Series(0, index=common_idx)

        # ETF cross proxies
        hyg_close = etf_aligned[("close", "HYG")].ffill().bfill() if ("close", "HYG") in etf_aligned.columns else pd.Series(1, index=common_idx)
        tlt_close = etf_aligned[("close", "TLT")].ffill().bfill() if ("close", "TLT") in etf_aligned.columns else pd.Series(1, index=common_idx)
        shy_close = etf_aligned[("close", "SHY")].ffill().bfill() if ("close", "SHY") in etf_aligned.columns else pd.Series(1, index=common_idx)

        hy_spread_proxy = hyg_close / tlt_close  # Rising ratio = Credit tightening/Risk-on; Falling = Credit stress
        curve_slope_proxy = tlt_close / shy_close  # Long bond vs Short bond ratio

        for i, dt in enumerate(dates):
            record = {
                "date": dt,
                "us10y_yield": round(float(tnx_close.iloc[i]), 4),
                "vix_index": round(float(vix_close.iloc[i]), 4),
                "dxy_index": round(float(dxy_close.iloc[i]), 4),
                "wti_oil": round(float(cl_close.iloc[i]), 4),
                "hy_spread_proxy": round(float(hy_spread_proxy.iloc[i]), 4),
                "curve_slope_proxy": round(float(curve_slope_proxy.iloc[i]), 4),
            }
            macro_records.append(record)

        if macro_records:
            save_macro_metrics(macro_records)
            logger.info(f"Saved {len(macro_records)} daily macro records into SQLite DB.")
        return macro_records

    def run_pipeline(self) -> Dict[str, Any]:
        """Execute full collection pipeline for Phase 1."""
        snapshots = self.fetch_etf_snapshots()
        etf_df, macro_df = self.fetch_historical_ohlcv()
        flow_records = self.process_and_store_flows(etf_df, snapshots)
        macro_records = self.process_and_store_macro(macro_df, etf_df)

        return {
            "snapshots_count": len(snapshots),
            "flow_records_count": len(flow_records),
            "macro_records_count": len(macro_records),
            "latest_date": [d.strftime("%Y-%m-%d") for d in etf_df.index][-1] if not etf_df.empty else None,
        }
