# -*- coding: utf-8 -*-
"""
Global Capital Flow Intelligence (GCFI)
Master Pipeline Runner & Institutional Quant Audit Reporter (Phases 1 - 5)
"""

import sys
import logging
from tabulate import tabulate
from datetime import datetime

# Enforce UTF-8 encoding for Windows standard output
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

from gcfi.db import init_db, get_connection
from gcfi.collector import CapitalFlowCollector
from gcfi.flow_engine import CapitalFlowEngine
from gcfi.regime import MarketRegimeDetector
from gcfi.futures_engine import FuturesSignalEngine
from gcfi.ai_quant_model import AIQuantPredictor
from gcfi.backtester import WalkForwardBacktester
from gcfi.autostock_bridge import AutoStockGlobalShield
from gcfi.gcfi_config import ETF_UNIVERSE

def run_gcfi_master():
    print("=" * 88)
    print("  [GCFI] GLOBAL CAPITAL FLOW INTELLIGENCE - INSTITUTIONAL QUANT PLATFORM")
    print(f"  Execution Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 88)

    # -------------------------------------------------------------
    # Step 1: Initialize Database
    # -------------------------------------------------------------
    print("\n[Step 1/7] Initializing Storage Architecture (SQLite WAL Mode)...")
    init_db()
    print("  -> Database tables and schema verified.")

    # -------------------------------------------------------------
    # Step 2: Phase 1 Data Ingestion & Flow Calculation
    # -------------------------------------------------------------
    print("\n[Step 2/7] Ingesting Multi-Asset Market Data & Flow Calculation (Phase 1)...")
    collector = CapitalFlowCollector(period="6mo")
    result = collector.run_pipeline()
    latest_date = result['latest_date']
    print(f"  -> ETF Snapshots: {result['snapshots_count']} | Flow Records: {result['flow_records_count']} | Macro Records: {result['macro_records_count']}")
    print(f"  -> Latest Market Date: {latest_date}")

    # -------------------------------------------------------------
    # Step 3: Phase 2 Advanced Capital Flow, Velocity & Rotation Network
    # -------------------------------------------------------------
    print("\n[Step 3/7] Analyzing Capital Rotation & Cross-Asset Correlations (Phase 2)...")
    flow_engine = CapitalFlowEngine()
    returns_p, flows_p = flow_engine.load_time_series()
    corrs = flow_engine.compute_cross_asset_correlations(returns_p, flows_p)
    velocity_data = flow_engine.analyze_flow_velocity_and_spikes()
    rotation_data = flow_engine.track_capital_rotation_network()
    print(f"  -> Computed 21 rolling correlation pairs across 5D, 20D, 60D windows.")
    print(f"  -> Mapped Source-to-Destination Capital Transition Network.")

    # -------------------------------------------------------------
    # Step 4: Phase 3 Market Regime Detection & State Transition
    # -------------------------------------------------------------
    print("\n[Step 4/7] Evaluating Market Regime & State Transitions (Phase 3)...")
    detector = MarketRegimeDetector()
    backfill_res = detector.backfill_historical_regimes()
    regime_res = detector.analyze_latest_regime()
    transition_res = detector.detect_regime_transition()
    print(f"  -> Backfilled {backfill_res['total_dates']} historical dates: {backfill_res['distribution']}")
    print(f"  -> Current Regime: {transition_res['current_regime']} (Persisted for {transition_res['duration_days']} consecutive trading days)")
    if transition_res['transition_detected']:
        print(f"  -> [TRANSITION ALERT] Changed from {transition_res['prev_regime']} to {transition_res['current_regime']}!")

    # -------------------------------------------------------------
    # Step 5: Phase 4 AI Quant Predictive Models (RFF, Ridge, Boosting)
    # -------------------------------------------------------------
    print("\n[Step 5/7] Running AI Quant Predictive Engine (Phase 4)...")
    ai_predictor = AIQuantPredictor()
    ml_results = ai_predictor.train_and_predict()
    print(f"  -> Trained models over {ml_results['sample_size']} days with {ml_results['features_count']} flow/macro features.")

    # -------------------------------------------------------------
    # Step 6: Phase 5 Walk-Forward Backtesting & AutoStock Safety Shield
    # -------------------------------------------------------------
    print("\n[Step 6/7] Running Walk-Forward Backtest & AutoStock Shield Check (Phase 5)...")
    backtester = WalkForwardBacktester()
    bt_nq = backtester.run_walk_forward(symbol="NQ")
    bt_es = backtester.run_walk_forward(symbol="ES")

    shield = AutoStockGlobalShield()
    shield_status = shield.evaluate_buy_permission()

    # Rule-based futures signals
    futures_engine = FuturesSignalEngine()
    signals = futures_engine.generate_signals(regime_res)

    # =============================================================
    # COMPREHENSIVE INSTITUTIONAL QUANT AUDIT REPORT
    # =============================================================
    conn = get_connection()

    print("\n" + "=" * 88)
    print(f"  [GCFI QUANT REPORT] MULTI-ASSET CAPITAL FLOW INTELLIGENCE (Date: {latest_date})")
    print("=" * 88)

    # 1. 15 Core ETFs Table
    rows = conn.execute("""
        SELECT f.ticker, u.asset_class, f.close, f.shares_outstanding, f.aum,
               f.net_flow, f.net_flow_5d, f.flow_acceleration, f.return_5d, f.flow_type
        FROM daily_etf_flows f
        JOIN asset_universe u ON f.ticker = u.ticker
        WHERE f.date = ?
        ORDER BY u.asset_class, f.ticker
    """, (latest_date,)).fetchall()

    t1_data = []
    for r in rows:
        shares_str = f"{r['shares_outstanding']:,}" if r['shares_outstanding'] else "N/A"
        aum_str = f"${r['aum']/1e9:,.2f}B" if r['aum'] else "N/A"
        flow_1d_str = f"${r['net_flow']/1e6:+,.1f}M" if r['net_flow'] else "$0.0M"
        flow_5d_str = f"${r['net_flow_5d']/1e6:+,.1f}M" if r['net_flow_5d'] else "$0.0M"
        fa_str = f"${r['flow_acceleration']/1e6:+,.1f}M/d" if r['flow_acceleration'] else "$0.0M/d"
        ret_5d_str = f"{r['return_5d']*100:+.2f}%" if r['return_5d'] else "0.00%"

        t1_data.append([
            r['ticker'],
            r['asset_class'],
            f"${r['close']:,.2f}",
            shares_str,
            aum_str,
            flow_1d_str,
            flow_5d_str,
            fa_str,
            ret_5d_str,
            r['flow_type'],
        ])
    headers1 = ["Ticker", "Asset Class", "Close", "Shares", "AUM", "Flow 1D", "Flow 5D", "Flow Accel", "Ret 5D", "Type"]
    print("\n[1. Core 15 ETFs Flow & Fundamental Snapshot]")
    print(tabulate(t1_data, headers=headers1, tablefmt="github"))

    # 2. Flow Velocity Table
    print("\n[2. Flow Velocity & Institutional Block Spikes (% of AUM per 5D & Z-Score)]")
    t2_data = []
    for v in sorted(velocity_data, key=lambda x: x["velocity_pct"], reverse=True):
        t2_data.append([
            v["ticker"],
            v["asset_class"],
            f"${v['aum']/1e9:,.2f}B",
            f"${v['nf5']/1e6:+,.1f}M",
            f"{v['velocity_pct']:+.2f}%",
            f"{v['z_score_20d']:+.2f}",
            f"[SPIKE: {v['spike_type']}]" if v["is_spike"] else "NORMAL",
        ])
    headers2 = ["Ticker", "Asset Class", "AUM", "5D Flow", "Flow Velocity (5D/AUM)", "20D Flow Z-Score", "Alert"]
    print(tabulate(t2_data, headers=headers2, tablefmt="github"))

    # 3. Capital Rotation Mapping
    print("\n[3. Capital Rotation: Where Did Exiting Capital Move?]")
    print(f"  - Total Equity Flow (5D): ${rotation_data['equity_flow_5d_m']:+,.1f}M")
    print(f"  - Total Bond Flow (5D):   ${rotation_data['bond_flow_5d_m']:+,.1f}M")
    print(f"  - Equity / Bond Ratio:    {rotation_data['equity_vs_bond_ratio']}x")
    print(f"  - Short Cash (SGOV) Flow: ${rotation_data['cash_flow_5d_m']:+,.1f}M (Cash Flight Proxy)")
    print(f"  - Risk / Cash Ratio:      {rotation_data['risk_vs_cash_ratio']}x")
    print(f"  - High Yield / TLT Ratio: {rotation_data['hy_vs_tlt_ratio']}x")

    t4_data = []
    for tr in rotation_data.get("transitions", []):
        t4_data.append([
            tr["from_asset"],
            f"${tr['outflow_amount_m']:,.1f}M",
            "->",
            tr["to_asset"],
            f"{tr['absorption_share_pct']:.1f}%",
            f"${tr['estimated_flow_m']:,.1f}M",
        ])
    if t4_data:
        headers4 = ["Exiting Source", "Total Outflow", "", "Absorbing Destination", "Absorption Share", "Est. Rotated Flow"]
        print(tabulate(t4_data, headers=headers4, tablefmt="github"))

    # 4. Cross-Asset Rolling Correlations
    print("\n[4. Cross-Asset Rolling Correlations (5D / 20D / 60D)]")
    t3_data = []
    corr_by_pair = {}
    for c in corrs:
        p = c["pair"]
        if p not in corr_by_pair:
            corr_by_pair[p] = {"desc": c["description"], "r5": 0, "f5": 0, "r20": 0, "f20": 0, "r60": 0, "f60": 0}
        w = c["window"]
        corr_by_pair[p][f"r{w}"] = c["return_corr"]
        corr_by_pair[p][f"f{w}"] = c["flow_corr"]

    for pair, val in corr_by_pair.items():
        t3_data.append([
            pair,
            val["desc"],
            f"{val['r5']:+.2f} / {val['f5']:+.2f}",
            f"{val['r20']:+.2f} / {val['f20']:+.2f}",
            f"{val['r60']:+.2f} / {val['f60']:+.2f}",
        ])
    headers3 = ["Asset Pair", "Economic Significance", "5D (Ret/Flow)", "20D (Ret/Flow)", "60D (Ret/Flow)"]
    print(tabulate(t3_data, headers=headers3, tablefmt="github"))

    # 5. Market Regime & State Transitions
    print("\n[5. Market Regime & State Transition Analysis]")
    print(f"  - Current State:       {transition_res['current_regime']} (Duration: {transition_res['duration_days']} trading days)")
    print(f"  - Risk Score:          {transition_res['risk_score']} / 100")
    print(f"  - Liquidity Score:     {transition_res['liquidity_score']} / 100")
    print(f"  - Credit Score:        {transition_res['credit_score']} / 100")
    print(f"  - Regime Drivers:      {regime_res.get('trigger_reasons')}")
    if transition_res['alert_msg']:
        print(f"\n  [TELEGRAM ALERT PAYLOAD]:\n{transition_res['alert_msg']}")

    # 6. AI Quant Futures Forecast & Top Macro Drivers
    print("\n[6. AI Quant Directional Momentum Forecast (RFF / Ridge / Gradient Boosting)]")
    p_es = ml_results["predictions"].get("ES", {})
    p_nq = ml_results["predictions"].get("NQ", {})

    ml_table = [
        ["ES (S&P 500)", "1-Day (Tomorrow)", p_es.get("1d", {}).get("direction"), f"{int(p_es.get('1d', {}).get('confidence', 0)*100)}%", p_es.get("1d", {}).get("bullish_votes")],
        ["ES (S&P 500)", "5-Day (Next Week)", p_es.get("5d", {}).get("direction"), f"{int(p_es.get('5d', {}).get('confidence', 0)*100)}%", p_es.get("5d", {}).get("bullish_votes")],
        ["NQ (Nasdaq 100)", "1-Day (Tomorrow)", p_nq.get("1d", {}).get("direction"), f"{int(p_nq.get('1d', {}).get('confidence', 0)*100)}%", p_nq.get("1d", {}).get("bullish_votes")],
        ["NQ (Nasdaq 100)", "5-Day (Next Week)", p_nq.get("5d", {}).get("direction"), f"{int(p_nq.get('5d', {}).get('confidence', 0)*100)}%", p_nq.get("5d", {}).get("bullish_votes")],
    ]
    print(tabulate(ml_table, headers=["Symbol", "Horizon", "AI Consensus", "Confidence", "Model Votes (Bullish)"], tablefmt="github"))

    print("\n  [Top 5 Capital Flow & Macro Predictive Drivers (Feature Importance)]")
    for d in ml_results.get("top_drivers", []):
        print(f"    * {d['feature']:16}: Weight {d['weight']:+.3f} -> {d['impact']}")

    # 7. Walk-Forward Backtesting Performance
    print("\n[7. Walk-Forward Backtesting Performance (Out-of-Sample)]")
    bt_table = [
        ["NQ (Nasdaq 100)", f"{bt_nq.get('test_days')} days", f"{bt_nq.get('accuracy')}%", f"{bt_nq.get('long_win_rate')}%", f"{bt_nq.get('cum_strategy_return'):+}%", f"{bt_nq.get('cum_benchmark_return'):+}%", str(bt_nq.get("sharpe_ratio")), f"{bt_nq.get('max_drawdown')}%"],
        ["ES (S&P 500)", f"{bt_es.get('test_days')} days", f"{bt_es.get('accuracy')}%", f"{bt_es.get('long_win_rate')}%", f"{bt_es.get('cum_strategy_return'):+}%", f"{bt_es.get('cum_benchmark_return'):+}%", str(bt_es.get("sharpe_ratio")), f"{bt_es.get('max_drawdown')}%"],
    ]
    headers_bt = ["Symbol", "Sample Period", "Overall Accuracy", "Long Win Rate", "Strategy Cum Ret", "Benchmark Ret", "Sharpe", "Max Drawdown"]
    print(tabulate(bt_table, headers=headers_bt, tablefmt="github"))

    # 8. AutoStock Safety Bridge Output
    print("\n[8. AutoStock Global Risk Shield Status (Read-Only Bridge)]")
    print(f"  - Domestic Stock Buy Permission: {'[PERMITTED]' if shield_status['allow_buy'] else '[BLOCKED]'}")
    print(f"  - Shield Reason: {shield_status.get('block_reason') or 'No systemic capital risk detected; normal trading allowed.'}")
    print(f"  - Bridge Status: {shield_status['notes']}")

    conn.close()

    # 9. Auto-Generate Web Dashboard
    from gcfi.dashboard import generate_dashboard_html
    dash_path = generate_dashboard_html()
    print(f"\n[9. Interactive Web Dashboard Generated]")
    print(f"  - Dashboard Path: {dash_path}")

    print("\n" + "=" * 88)
    print("  [GCFI] MASTER QUANT PIPELINE EXECUTED SUCCESSFULLY")
    print("=" * 88)

if __name__ == "__main__":
    run_gcfi_master()
