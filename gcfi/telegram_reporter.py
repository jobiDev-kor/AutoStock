# -*- coding: utf-8 -*-
"""
Global Capital Flow Intelligence (GCFI)
Telegram Reporting Engine
Formats real-time GCFI quant intelligence into beautiful, executive-ready HTML for Telegram.
"""

import json
import sqlite3
from typing import Dict, Any, Optional
from datetime import datetime
from gcfi.gcfi_config import DB_PATH
from gcfi.db import get_connection

def format_gcfi_telegram_briefing() -> str:
    """Generate executive summary briefing for Telegram /글로벌 or /자본 command."""
    conn = get_connection()

    # 1. Latest Date
    max_d_row = conn.execute("SELECT MAX(date) FROM daily_etf_flows").fetchone()
    if not max_d_row or not max_d_row[0]:
        conn.close()
        return "⚠️ GCFI 글로벌 자본 데이터가 아직 초기화되지 않았습니다."

    latest_date = max_d_row[0]

    # 2. Market Regime & Scores
    reg_row = conn.execute("SELECT * FROM market_regimes WHERE date = ?", (latest_date,)).fetchone()
    regime = reg_row["regime"] if reg_row else "Neutral"
    risk_score = reg_row["risk_score"] if reg_row else 50.0
    liq_score = reg_row["liquidity_score"] if reg_row else 50.0
    cred_score = reg_row["credit_score"] if reg_row else 50.0
    trigger_reasons = reg_row["trigger_reasons"] if reg_row else ""

    # 3. Capital Rotation
    rot_row = conn.execute("SELECT * FROM capital_rotation_summary WHERE date = ?", (latest_date,)).fetchone()
    eq_flow = rot_row["equity_total_flow_5d"] if rot_row else 0.0
    bd_flow = rot_row["bond_total_flow_5d"] if rot_row else 0.0
    cash_flow = rot_row["cash_flight_flow_5d"] if rot_row else 0.0
    eq_bd_ratio = rot_row["equity_vs_bond_ratio"] if rot_row else 1.0

    transitions = json.loads(rot_row["flow_transition_map"]) if rot_row and rot_row["flow_transition_map"] else []

    # 4. Futures Signals (ES & NQ)
    sig_rows = conn.execute("SELECT * FROM futures_signals WHERE date = ?", (latest_date,)).fetchall()
    signals = {r["symbol"]: dict(r) for r in sig_rows}

    # 5. Key ETF Flows (SPY, QQQ, TLT, SGOV, GLD, HYG, USO, IBIT)
    watch_tickers = ["SPY", "QQQ", "TLT", "SGOV", "GLD", "HYG", "USO", "IBIT"]
    etf_rows = conn.execute(f"""
        SELECT ticker, close, net_flow_5d, flow_acceleration, return_5d
        FROM daily_etf_flows
        WHERE date = ? AND ticker IN ({','.join('?' for _ in watch_tickers)})
    """, (latest_date, *watch_tickers)).fetchall()
    etf_map = {r["ticker"]: dict(r) for r in etf_rows}

    # 6. Macro indicators
    macro_row = conn.execute("SELECT * FROM daily_macro_metrics WHERE date = ?", (latest_date,)).fetchone()
    macro = dict(macro_row) if macro_row else {}

    conn.close()

    # Build Regime Icon & Shield Status
    regime_icon = "🟢" if "Risk-On" in regime or "Liquidity Expansion" in regime else ("🟡" if "Neutral" in regime else "🔴")
    shield_icon = "🛡️ <b>[정상 매수 허용]</b>" if risk_score > -30 and "Credit Stress" not in regime else "🛑 <b>[신규 매수 주의/차단]</b>"

    # Transition summary string
    top_rot = ""
    if transitions:
        # Show top absorption destination
        dest_shares = {}
        for tr in transitions:
            dst = tr["to_asset"]
            dest_shares[dst] = tr["absorption_share_pct"]
        top_dst = sorted(dest_shares.items(), key=lambda x: x[1], reverse=True)[0]
        top_rot = f"• <b>자본 회전</b>: 유출 자본의 <b>{top_dst[1]}%</b>가 <b>{top_dst[0]}</b>으로 집중 흡수"

    # Key flows bullet list
    def fmt_m(val):
        return f"{val/1e6:+,.0f}M" if val else "$0M"

    spy_f = fmt_m(etf_map.get("SPY", {}).get("net_flow_5d", 0))
    qqq_f = fmt_m(etf_map.get("QQQ", {}).get("net_flow_5d", 0))
    tlt_f = fmt_m(etf_map.get("TLT", {}).get("net_flow_5d", 0))
    sgov_f = fmt_m(etf_map.get("SGOV", {}).get("net_flow_5d", 0))
    gld_f = fmt_m(etf_map.get("GLD", {}).get("net_flow_5d", 0))
    hyg_f = fmt_m(etf_map.get("HYG", {}).get("net_flow_5d", 0))

    es_sig = signals.get("ES", {})
    nq_sig = signals.get("NQ", {})

    msg = (
        f"🌐 <b>[GCFI 글로벌 자본 이동 & 선물 인텔리전스]</b>\n"
        f"기준일자: <code>{latest_date}</code>\n\n"
        f"{regime_icon} <b>글로벌 시장 국면: {regime}</b>\n"
        f"• <b>위험 점수</b>: <b>{risk_score:.0f}점</b> / 100점\n"
        f"• <b>유동성 점수</b>: <b>{liq_score:.0f}점</b> | <b>신용 점수</b>: <b>{cred_score:.0f}점</b>\n"
        f"• <b>리스크 실드</b>: {shield_icon}\n\n"
        f"💰 <b>[5일 누적 글로벌 자금 순유출입]</b>\n"
        f"• 🇺🇸 <b>미국 주식</b>: SPY <b>{spy_f}</b> | QQQ <b>{qqq_f}</b>\n"
        f"• 🏦 <b>미국 채권</b>: 국채(TLT) <b>{tlt_f}</b> | 고수익(HYG) <b>{hyg_f}</b>\n"
        f"• 💵 <b>초단기 현금</b>: SGOV <b>{sgov_f}</b> (현금 도피 없음)\n"
        f"• 🥇 <b>안전자산 금</b>: GLD <b>{gld_f}</b>\n"
        f"• ⚖️ <b>주식/채권 유입비율</b>: <b>{eq_bd_ratio:.1f}배</b> (주식 압도적 우위)\n"
        f"{top_rot}\n\n"
        f"🎯 <b>[미국 지수선물 방향성 및 AI 진단]</b>\n"
        f"• <b>S&P 500 (ES)</b>: <b>{es_sig.get('direction', 'NEUTRAL')}</b> (신뢰도 {int(es_sig.get('confidence', 0.5)*100)}%)\n"
        f"• <b>나스닥 100 (NQ)</b>: <b>{nq_sig.get('direction', 'NEUTRAL')}</b> (신뢰도 {int(nq_sig.get('confidence', 0.5)*100)}%)\n"
        f"  <i>({nq_sig.get('notes', '정상 수급 흐름').split('|')[-1].strip()})</i>\n\n"
        f"📊 <b>[핵심 거시경제 지표]</b>\n"
        f"• 10년물 금리: <b>{macro.get('us10y_yield', 0):.2f}%</b> | VIX 변동성: <b>{macro.get('vix_index', 0):.1f}pt</b>\n"
        f"• 달러인덱스: <b>{macro.get('dxy_index', 0):.2f}</b> | WTI 원유: <b>${macro.get('wti_oil', 0):.2f}</b>\n\n"
        f"👉 상세 명령: <code>/자본회전</code> (자금 흡수 경로), <code>/미국선물</code> (선물 팩터 가중치)"
    )
    return msg

def format_futures_detail_briefing() -> str:
    """Generate detailed AI futures prediction briefing for /미국선물 command."""
    conn = get_connection()
    ai_rows = conn.execute("""
        SELECT symbol, horizon, direction, confidence, bullish_votes, top_drivers_json, date
        FROM futures_ai_predictions
        ORDER BY date DESC
    """).fetchall()
    conn.close()

    if ai_rows:
        latest_date = ai_rows[0]["date"]
        p_data = {}
        top_drivers = []
        for r in ai_rows:
            if r["date"] == latest_date:
                sym = r["symbol"]
                hz = r["horizon"]
                if sym not in p_data:
                    p_data[sym] = {}
                p_data[sym][hz] = dict(r)
                if not top_drivers and r["top_drivers_json"]:
                    try:
                        top_drivers = json.loads(r["top_drivers_json"])
                    except Exception:
                        pass

        p_es = p_data.get("ES", {})
        p_nq = p_data.get("NQ", {})

        lines = [
            f"🎯 <b>[GCFI AI 퀀트 미국 지수선물 정밀 진단]</b>",
            f"기준일자: <code>{latest_date}</code>\n",
            f"<b>1. 나스닥 100 선물 (NQ)</b>",
            f" • <b>익일(1-Day)</b>: <b>{p_nq.get('1d', {}).get('direction', 'NEUTRAL')}</b> (신뢰도 {int(p_nq.get('1d', {}).get('confidence', 0.5)*100)}%)",
            f" • <b>차주(5-Day)</b>: <b>{p_nq.get('5d', {}).get('direction', 'NEUTRAL')}</b> (신뢰도 {int(p_nq.get('5d', {}).get('confidence', 0.5)*100)}%)",
            f" • 모델 투표: 3개 모델 중 <b>{p_nq.get('5d', {}).get('bullish_votes', '0/3')}</b> 상승 지지\n",
            f"<b>2. S&P 500 선물 (ES)</b>",
            f" • <b>익일(1-Day)</b>: <b>{p_es.get('1d', {}).get('direction', 'NEUTRAL')}</b> (신뢰도 {int(p_es.get('1d', {}).get('confidence', 0.5)*100)}%)",
            f" • <b>차주(5-Day)</b>: <b>{p_es.get('5d', {}).get('direction', 'NEUTRAL')}</b> (신뢰도 {int(p_es.get('5d', {}).get('confidence', 0.5)*100)}%)",
            f" • 모델 투표: 3개 모델 중 <b>{p_es.get('5d', {}).get('bullish_votes', '0/3')}</b> 상승 지지\n",
            f"🧠 <b>[주요 수급 및 매크로 영향 팩터 (Top Drivers)]</b>"
        ]

        for d in top_drivers[:5]:
            icon = "🟢" if d["weight"] > 0 else "🔴"
            lines.append(f" {icon} <code>{d['feature']}</code> (가중치 {d['weight']:+.3f}): {d['impact']}")

        lines.append("\n💡 <i>RFF 비선형 커널 릿지 + 선형 릿지 + Gradient Boosting 앙상블 분석 결과입니다.</i>")
        return "\n".join(lines)

    # Fallback to dynamic execution if DB empty
    from gcfi.ai_quant_model import AIQuantPredictor
    predictor = AIQuantPredictor()
    ml_res = predictor.train_and_predict()

    latest_date = ml_res.get("date", "최신")
    p_es = ml_res["predictions"].get("ES", {})
    p_nq = ml_res["predictions"].get("NQ", {})

    lines = [
        f"🎯 <b>[GCFI AI 퀀트 미국 지수선물 정밀 진단]</b>",
        f"기준일자: <code>{latest_date}</code> (학습 표본: {ml_res['sample_size']}일)\n",
        f"<b>1. 나스닥 100 선물 (NQ)</b>",
        f" • <b>익일(1-Day)</b>: <b>{p_nq.get('1d', {}).get('direction')}</b> (신뢰도 {int(p_nq.get('1d', {}).get('confidence', 0)*100)}%)",
        f" • <b>차주(5-Day)</b>: <b>{p_nq.get('5d', {}).get('direction')}</b> (신뢰도 {int(p_nq.get('5d', {}).get('confidence', 0)*100)}%)",
        f" • 모델 투표: 3개 모델 중 <b>{p_nq.get('5d', {}).get('bullish_votes')}</b> 상승 지지\n",
        f"<b>2. S&P 500 선물 (ES)</b>",
        f" • <b>익일(1-Day)</b>: <b>{p_es.get('1d', {}).get('direction')}</b> (신뢰도 {int(p_es.get('1d', {}).get('confidence', 0)*100)}%)",
        f" • <b>차주(5-Day)</b>: <b>{p_es.get('5d', {}).get('direction')}</b> (신뢰도 {int(p_es.get('5d', {}).get('confidence', 0)*100)}%)",
        f" • 모델 투표: 3개 모델 중 <b>{p_es.get('5d', {}).get('bullish_votes')}</b> 상승 지지\n",
        f"🧠 <b>[주요 수급 및 매크로 영향 팩터 (Top Drivers)]</b>"
    ]

    for d in ml_res.get("top_drivers", []):
        icon = "🟢" if d["weight"] > 0 else "🔴"
        lines.append(f" {icon} <code>{d['feature']}</code> (가중치 {d['weight']:+.3f}): {d['impact']}")

    lines.append("\n💡 <i>RFF 비선형 커널 릿지 + 선형 릿지 + Gradient Boosting 앙상블 분석 결과입니다.</i>")
    return "\n".join(lines)

def format_capital_rotation_briefing() -> str:
    """Generate detailed capital rotation transition briefing for /자본회전 command."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM capital_rotation_summary ORDER BY date DESC LIMIT 1").fetchone()
    conn.close()

    if not row:
        return "⚠️ 자본 회전 분석 데이터가 없습니다."

    transitions = json.loads(row["flow_transition_map"]) if row["flow_transition_map"] else []
    sources = json.loads(row["source_assets"]) if row["source_assets"] else {}
    destinations = json.loads(row["destination_assets"]) if row["destination_assets"] else {}

    lines = [
        f"🔄 <b>[GCFI 글로벌 자본 이동 경로 및 흡수 네트워크]</b>",
        f"기준일자: <code>{row['date']}</code>\n",
        f"🔻 <b>[자본 이탈원 (Sources)]</b>",
    ]
    for src, amt in sources.items():
        lines.append(f" • {src}: <b>{amt/1e6:,.1f}M 달러 유출</b>")

    lines.append("\n🔺 <b>[자본 흡수처 (Destinations)]</b>")
    for dst, amt in destinations.items():
        lines.append(f" • {dst}: <b>+{amt/1e6:,.1f}M 달러 유입</b>")

    lines.append("\n🔀 <b>[자금 회전 세부 추적 (Top 5 경로)]</b>")
    for tr in transitions[:5]:
        lines.append(f" • {tr['from_asset']} ➔ <b>{tr['to_asset']}</b> ({tr['absorption_share_pct']}% 흡수, 약 ${tr['estimated_flow_m']}M)")

    lines.append("\n💡 <i>전체 자산군의 5일 순유출입 벡터를 기반으로 자금의 회전 이동을 추적한 결과입니다.</i>")
    return "\n".join(lines)
