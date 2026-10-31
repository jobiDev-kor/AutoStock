# -*- coding: utf-8 -*-
"""
Global Capital Flow Intelligence (GCFI)
Database Access Layer & Schema Initialization (SQLite)
"""

import sqlite3
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime
from gcfi.gcfi_config import DB_PATH, ETF_UNIVERSE

logger = logging.getLogger("GCFI.DB")

def get_connection(db_path=DB_PATH) -> sqlite3.Connection:
    """Create a connection with WAL mode and row factory."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def init_db(db_path=DB_PATH):
    """Initialize GCFI tables and default asset universe."""
    conn = get_connection(db_path)
    cur = conn.cursor()

    # 1. Asset Universe Table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS asset_universe (
        ticker TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        asset_class TEXT NOT NULL,
        benchmark INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 2. Daily ETF Flows Table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_etf_flows (
        date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        volume INTEGER,
        shares_outstanding INTEGER,
        nav REAL,
        aum REAL,
        net_flow REAL,
        net_flow_5d REAL,
        net_flow_20d REAL,
        flow_acceleration REAL,
        flow_type TEXT DEFAULT 'ESTIMATED',
        return_1d REAL,
        return_5d REAL,
        return_20d REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (date, ticker),
        FOREIGN KEY (ticker) REFERENCES asset_universe(ticker)
    );
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_etf_flows_ticker ON daily_etf_flows(ticker);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_etf_flows_date ON daily_etf_flows(date);")

    # 3. Daily Macro Metrics Table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_macro_metrics (
        date TEXT PRIMARY KEY,
        us10y_yield REAL,
        vix_index REAL,
        dxy_index REAL,
        wti_oil REAL,
        hy_spread_proxy REAL,
        curve_slope_proxy REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 4. Market Regimes Table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS market_regimes (
        date TEXT PRIMARY KEY,
        regime TEXT NOT NULL,
        risk_score REAL,
        liquidity_score REAL,
        credit_score REAL,
        leading_asset TEXT,
        lagging_asset TEXT,
        trigger_reasons TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 5. US Futures Signals Table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS futures_signals (
        date TEXT NOT NULL,
        symbol TEXT NOT NULL,
        direction TEXT NOT NULL,
        confidence REAL,
        flow_support REAL,
        regime_alignment INTEGER,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (date, symbol)
    );
    """)

    # 6. Cross-Asset Correlations Table (Phase 2)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cross_asset_correlations (
        date TEXT NOT NULL,
        pair TEXT NOT NULL,
        window INTEGER NOT NULL,
        return_corr REAL,
        flow_corr REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (date, pair, window)
    );
    """)

    # 7. Capital Rotation Summary Table (Phase 2)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS capital_rotation_summary (
        date TEXT PRIMARY KEY,
        equity_total_flow_5d REAL,
        bond_total_flow_5d REAL,
        equity_vs_bond_ratio REAL,
        cash_flight_flow_5d REAL,
        risk_vs_cash_ratio REAL,
        hy_vs_tlt_flow_ratio REAL,
        gold_vs_dollar_flow_ratio REAL,
        source_assets TEXT,
        destination_assets TEXT,
        flow_transition_map TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 8. Futures AI Predictions Table (Phase 4)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS futures_ai_predictions (
        date TEXT NOT NULL,
        symbol TEXT NOT NULL,
        horizon TEXT NOT NULL,
        direction TEXT NOT NULL,
        confidence REAL,
        bullish_votes TEXT,
        top_drivers_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (date, symbol, horizon)
    );
    """)

    # Populate asset_universe
    for ticker, info in ETF_UNIVERSE.items():
        cur.execute("""
        INSERT INTO asset_universe (ticker, name, asset_class, benchmark, active)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(ticker) DO UPDATE SET
            name=excluded.name,
            asset_class=excluded.asset_class,
            benchmark=excluded.benchmark;
        """, (ticker, info["name"], info["asset_class"], 1 if info["benchmark"] else 0))

    conn.commit()
    conn.close()
    logger.info("GCFI Database schema initialized successfully.")

def save_etf_flow_records(records: List[Dict[str, Any]], db_path=DB_PATH):
    """Batch UPSERT daily ETF flow records."""
    conn = get_connection(db_path)
    cur = conn.cursor()

    query = """
    INSERT INTO daily_etf_flows (
        date, ticker, open, high, low, close, volume,
        shares_outstanding, nav, aum, net_flow,
        net_flow_5d, net_flow_20d, flow_acceleration,
        flow_type, return_1d, return_5d, return_20d
    ) VALUES (
        :date, :ticker, :open, :high, :low, :close, :volume,
        :shares_outstanding, :nav, :aum, :net_flow,
        :net_flow_5d, :net_flow_20d, :flow_acceleration,
        :flow_type, :return_1d, :return_5d, :return_20d
    )
    ON CONFLICT(date, ticker) DO UPDATE SET
        open=excluded.open,
        high=excluded.high,
        low=excluded.low,
        close=excluded.close,
        volume=excluded.volume,
        shares_outstanding=excluded.shares_outstanding,
        nav=excluded.nav,
        aum=excluded.aum,
        net_flow=excluded.net_flow,
        net_flow_5d=excluded.net_flow_5d,
        net_flow_20d=excluded.net_flow_20d,
        flow_acceleration=excluded.flow_acceleration,
        flow_type=excluded.flow_type,
        return_1d=excluded.return_1d,
        return_5d=excluded.return_5d,
        return_20d=excluded.return_20d;
    """
    cur.executemany(query, records)
    conn.commit()
    conn.close()

def save_macro_metrics(records: List[Dict[str, Any]], db_path=DB_PATH):
    """Batch UPSERT daily macro metrics."""
    conn = get_connection(db_path)
    cur = conn.cursor()

    query = """
    INSERT INTO daily_macro_metrics (
        date, us10y_yield, vix_index, dxy_index, wti_oil, hy_spread_proxy, curve_slope_proxy
    ) VALUES (
        :date, :us10y_yield, :vix_index, :dxy_index, :wti_oil, :hy_spread_proxy, :curve_slope_proxy
    )
    ON CONFLICT(date) DO UPDATE SET
        us10y_yield=excluded.us10y_yield,
        vix_index=excluded.vix_index,
        dxy_index=excluded.dxy_index,
        wti_oil=excluded.wti_oil,
        hy_spread_proxy=excluded.hy_spread_proxy,
        curve_slope_proxy=excluded.curve_slope_proxy;
    """
    cur.executemany(query, records)
    conn.commit()
    conn.close()

def save_market_regime(record: Dict[str, Any], db_path=DB_PATH):
    """Save detected market regime for a date."""
    conn = get_connection(db_path)
    cur = conn.cursor()

    query = """
    INSERT INTO market_regimes (
        date, regime, risk_score, liquidity_score, credit_score,
        leading_asset, lagging_asset, trigger_reasons
    ) VALUES (
        :date, :regime, :risk_score, :liquidity_score, :credit_score,
        :leading_asset, :lagging_asset, :trigger_reasons
    )
    ON CONFLICT(date) DO UPDATE SET
        regime=excluded.regime,
        risk_score=excluded.risk_score,
        liquidity_score=excluded.liquidity_score,
        credit_score=excluded.credit_score,
        leading_asset=excluded.leading_asset,
        lagging_asset=excluded.lagging_asset,
        trigger_reasons=excluded.trigger_reasons;
    """
    cur.execute(query, record)
    conn.commit()
    conn.close()

def save_ai_predictions(records: List[Dict[str, Any]], db_path=DB_PATH):
    """Save AI Quant Futures predictions."""
    conn = get_connection(db_path)
    cur = conn.cursor()

    query = """
    INSERT INTO futures_ai_predictions (
        date, symbol, horizon, direction, confidence, bullish_votes, top_drivers_json
    ) VALUES (
        :date, :symbol, :horizon, :direction, :confidence, :bullish_votes, :top_drivers_json
    )
    ON CONFLICT(date, symbol, horizon) DO UPDATE SET
        direction=excluded.direction,
        confidence=excluded.confidence,
        bullish_votes=excluded.bullish_votes,
        top_drivers_json=excluded.top_drivers_json;
    """
    cur.executemany(query, records)
    conn.commit()
    conn.close()


def save_futures_signals(records: List[Dict[str, Any]], db_path=DB_PATH):
    """Save ES/NQ signals."""
    conn = get_connection(db_path)
    cur = conn.cursor()

    query = """
    INSERT INTO futures_signals (
        date, symbol, direction, confidence, flow_support, regime_alignment, notes
    ) VALUES (
        :date, :symbol, :direction, :confidence, :flow_support, :regime_alignment, :notes
    )
    ON CONFLICT(date, symbol) DO UPDATE SET
        direction=excluded.direction,
        confidence=excluded.confidence,
        flow_support=excluded.flow_support,
        regime_alignment=excluded.regime_alignment,
        notes=excluded.notes;
    """
    cur.executemany(query, records)
    conn.commit()
    conn.close()

def save_correlations(records: List[Dict[str, Any]], db_path=DB_PATH):
    """Save cross-asset correlation metrics."""
    conn = get_connection(db_path)
    cur = conn.cursor()

    query = """
    INSERT INTO cross_asset_correlations (
        date, pair, window, return_corr, flow_corr
    ) VALUES (
        :date, :pair, :window, :return_corr, :flow_corr
    )
    ON CONFLICT(date, pair, window) DO UPDATE SET
        return_corr=excluded.return_corr,
        flow_corr=excluded.flow_corr;
    """
    cur.executemany(query, records)
    conn.commit()
    conn.close()

def save_rotation_summary(record: Dict[str, Any], db_path=DB_PATH):
    """Save capital rotation summary record."""
    conn = get_connection(db_path)
    cur = conn.cursor()

    query = """
    INSERT INTO capital_rotation_summary (
        date, equity_total_flow_5d, bond_total_flow_5d, equity_vs_bond_ratio,
        cash_flight_flow_5d, risk_vs_cash_ratio, hy_vs_tlt_flow_ratio,
        gold_vs_dollar_flow_ratio, source_assets, destination_assets,
        flow_transition_map
    ) VALUES (
        :date, :equity_total_flow_5d, :bond_total_flow_5d, :equity_vs_bond_ratio,
        :cash_flight_flow_5d, :risk_vs_cash_ratio, :hy_vs_tlt_flow_ratio,
        :gold_vs_dollar_flow_ratio, :source_assets, :destination_assets,
        :flow_transition_map
    )
    ON CONFLICT(date) DO UPDATE SET
        equity_total_flow_5d=excluded.equity_total_flow_5d,
        bond_total_flow_5d=excluded.bond_total_flow_5d,
        equity_vs_bond_ratio=excluded.equity_vs_bond_ratio,
        cash_flight_flow_5d=excluded.cash_flight_flow_5d,
        risk_vs_cash_ratio=excluded.risk_vs_cash_ratio,
        hy_vs_tlt_flow_ratio=excluded.hy_vs_tlt_flow_ratio,
        gold_vs_dollar_flow_ratio=excluded.gold_vs_dollar_flow_ratio,
        source_assets=excluded.source_assets,
        destination_assets=excluded.destination_assets,
        flow_transition_map=excluded.flow_transition_map;
    """
    cur.execute(query, record)
    conn.commit()
    conn.close()

def save_ai_predictions(records: List[Dict[str, Any]], db_path=DB_PATH):
    """Save AI Quant Futures predictions."""
    conn = get_connection(db_path)
    cur = conn.cursor()

    query = """
    INSERT INTO futures_ai_predictions (
        date, symbol, horizon, direction, confidence, bullish_votes, top_drivers_json
    ) VALUES (
        :date, :symbol, :horizon, :direction, :confidence, :bullish_votes, :top_drivers_json
    )
    ON CONFLICT(date, symbol, horizon) DO UPDATE SET
        direction=excluded.direction,
        confidence=excluded.confidence,
        bullish_votes=excluded.bullish_votes,
        top_drivers_json=excluded.top_drivers_json;
    """
    cur.executemany(query, records)
    conn.commit()
    conn.close()


