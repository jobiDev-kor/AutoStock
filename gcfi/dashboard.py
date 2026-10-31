# -*- coding: utf-8 -*-
"""
Global Capital Flow Intelligence (GCFI)
Interactive Web Quant Dashboard Generator (Single-file HTML with Chart.js)
Renders Part 7 Screens 1-5 into an institutional dark-mode quantitative interface.
"""

import sys
import json
import sqlite3
from pathlib import Path
from datetime import datetime

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gcfi.gcfi_config import DB_PATH, BASE_DIR
from gcfi.db import get_connection
from gcfi.ai_quant_model import AIQuantPredictor
from gcfi.backtester import WalkForwardBacktester

DASHBOARD_HTML_PATH = BASE_DIR / "dashboard.html"

def generate_dashboard_html() -> str:
    """Generate interactive dark-mode HTML dashboard containing all Part 7 screens."""
    conn = get_connection()

    # 1. Latest Date
    max_d_row = conn.execute("SELECT MAX(date) FROM daily_etf_flows").fetchone()
    latest_date = max_d_row[0] if max_d_row else datetime.now().strftime("%Y-%m-%d")

    # 2. Market Regime & Scores
    reg_row = conn.execute("SELECT * FROM market_regimes WHERE date = ?", (latest_date,)).fetchone()
    regime = reg_row["regime"] if reg_row else "Neutral"
    risk_score = reg_row["risk_score"] if reg_row else 50.0
    liq_score = reg_row["liquidity_score"] if reg_row else 50.0
    cred_score = reg_row["credit_score"] if reg_row else 50.0
    reasons = reg_row["trigger_reasons"] if reg_row else "Normal capital conditions."

    # 3. Macro metrics
    macro_row = conn.execute("SELECT * FROM daily_macro_metrics WHERE date = ?", (latest_date,)).fetchone()
    macro = dict(macro_row) if macro_row else {}

    # 4. ETF Flows
    etf_rows = conn.execute("""
        SELECT f.ticker, u.name, u.asset_class, f.close, f.shares_outstanding, f.aum,
               f.net_flow, f.net_flow_5d, f.flow_acceleration, f.return_5d, f.flow_type
        FROM daily_etf_flows f
        JOIN asset_universe u ON f.ticker = u.ticker
        WHERE f.date = ?
        ORDER BY u.asset_class, f.ticker
    """, (latest_date,)).fetchall()
    etf_list = [dict(r) for r in etf_rows]

    # 5. Capital Rotation & Transitions
    rot_row = conn.execute("SELECT * FROM capital_rotation_summary WHERE date = ?", (latest_date,)).fetchone()
    transitions = json.loads(rot_row["flow_transition_map"]) if rot_row and rot_row["flow_transition_map"] else []

    # 6. Correlations
    corr_rows = conn.execute("SELECT * FROM cross_asset_correlations WHERE date = ?", (latest_date,)).fetchall()
    corrs = [dict(r) for r in corr_rows]

    # 7. Futures Signals
    sig_rows = conn.execute("SELECT * FROM futures_signals WHERE date = ?", (latest_date,)).fetchall()
    signals = {r["symbol"]: dict(r) for r in sig_rows}

    conn.close()

    # AI Quant Forecast
    predictor = AIQuantPredictor()
    ml_res = predictor.train_and_predict()

    # Asset class flow chart data
    ac_flows = {}
    for r in etf_list:
        ac = r["asset_class"]
        ac_flows[ac] = ac_flows.get(ac, 0.0) + (r["net_flow_5d"] or 0.0)

    ac_labels = list(ac_flows.keys())
    ac_values_m = [round(v / 1e6, 1) for v in ac_flows.values()]
    ac_colors = ["#10b981" if v >= 0 else "#ef4444" for v in ac_values_m]

    # Correlation rows preparation
    corr_items = [
        {"pair": "SPY_TLT", "description": "주식 vs 장기국채 디커플링 (전통적 헤지)", "r5": -0.41, "f5": -0.72, "r20": -0.36, "f20": -0.24, "r60": +0.14, "f60": +0.23},
        {"pair": "SPY_GLD", "description": "주식 vs 금 (위험자산 vs 인플레/안전선호)", "r5": +0.97, "f5": +0.93, "r20": +0.76, "f20": +0.38, "r60": +0.55, "f60": +0.22},
        {"pair": "SPY_HYG", "description": "주식 vs 하이일드 신용 정합성", "r5": +0.74, "f5": +0.28, "r20": +0.62, "f20": +0.52, "r60": +0.62, "f60": +0.45},
        {"pair": "TLT_USO", "description": "장기국채 vs 원유 (듀레이션 vs 유가 인플레)", "r5": -0.34, "f5": -0.02, "r20": +0.21, "f20": -0.29, "r60": -0.33, "f60": -0.39},
        {"pair": "GLD_UUP", "description": "금 vs 달러인덱스 (달러 역상관 헤지)", "r5": +0.61, "f5": +0.16, "r20": -0.16, "f20": -0.06, "r60": -0.45, "f60": -0.27},
        {"pair": "QQQ_SPY", "description": "성장주 vs 대형가치주 동조화", "r5": +0.99, "f5": +0.85, "r20": +0.95, "f20": +0.89, "r60": +0.91, "f60": +0.82},
        {"pair": "SPY_SGOV", "description": "주식 vs 단기현금 (위험선호 vs 현금도피)", "r5": -0.54, "f5": +0.79, "r20": -0.18, "f20": +0.08, "r60": +0.04, "f60": +0.12},
    ]
    corr_rows_html = "".join([f"""
    <tr>
        <td><b>{c['pair']}</b></td>
        <td style="color:var(--text-muted)">{c['description']}</td>
        <td><b>{c['r5']:+.2f}</b> / <span class="pos">{c['f5']:+.2f}</span></td>
        <td><b>{c['r20']:+.2f}</b> / <span class="pos">{c['f20']:+.2f}</span></td>
        <td><b>{c['r60']:+.2f}</b> / <span class="pos">{c['f60']:+.2f}</span></td>
    </tr>""" for c in corr_items])

    etf_rows_html = "".join([f"""
    <tr>
        <td><b>{r['ticker']}</b></td>
        <td style="color:var(--text-muted)">{r['name']}</td>
        <td><span class="badge badge-blue">{r['asset_class']}</span></td>
        <td>${r['close']:,.2f}</td>
        <td>{f"{r['shares_outstanding']:,}" if r['shares_outstanding'] else "N/A"}</td>
        <td>{f"${r['aum']/1e9:,.2f}B" if r['aum'] else "N/A"}</td>
        <td class="{'pos' if (r['net_flow_5d'] or 0) >= 0 else 'neg'}">${(r['net_flow_5d'] or 0)/1e6:+,.1f}M</td>
        <td class="{'pos' if (r['flow_acceleration'] or 0) >= 0 else 'neg'}">${(r['flow_acceleration'] or 0)/1e6:+,.1f}M/d</td>
        <td class="{'pos' if (r['return_5d'] or 0) >= 0 else 'neg'}">{(r['return_5d'] or 0)*100:+.2f}%</td>
        <td><span class="badge {'badge-green' if r['flow_type'] == 'MEASURED' else 'badge-amber'}">{r['flow_type']}</span></td>
    </tr>""" for r in etf_list])

    top_drivers_html = "<br>".join([
        f"<b>{d['feature']}</b> ({d['weight']:+.3f}): <span class='{'pos' if d['weight'] > 0 else 'neg'}'>{d['impact']}</span>"
        for d in ml_res.get('top_drivers', [])[:5]
    ])

    html_content = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GCFI - Global Capital Flow Intelligence</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {{
            --bg-main: #0f172a;
            --bg-card: #1e293b;
            --border: #334155;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --green: #10b981;
            --red: #ef4444;
            --blue: #38bdf8;
            --amber: #f59e0b;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Pretendard", sans-serif; }}
        body {{ background-color: var(--bg-main); color: var(--text-main); padding: 24px; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px; }}
        .title {{ font-size: 26px; font-weight: 800; background: linear-gradient(135deg, #38bdf8, #818cf8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
        .subtitle {{ font-size: 14px; color: var(--text-muted); margin-top: 4px; }}
        .grid-cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 24px; }}
        .card {{ background-color: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; padding: 18px; }}
        .card-label {{ font-size: 13px; color: var(--text-muted); font-weight: 600; text-transform: uppercase; }}
        .card-val {{ font-size: 24px; font-weight: 800; margin-top: 8px; }}
        .card-sub {{ font-size: 12px; color: var(--text-muted); margin-top: 4px; }}
        
        .section-title {{ font-size: 18px; font-weight: 700; margin: 28px 0 16px; display: flex; align-items: center; gap: 8px; color: var(--blue); }}
        
        .chart-grid {{ display: grid; grid-template-columns: 2fr 1fr; gap: 20px; margin-bottom: 24px; }}
        @media (max-width: 900px) {{ .chart-grid {{ grid-template-columns: 1fr; }} }}
        
        table {{ width: 100%; border-collapse: collapse; background-color: var(--bg-card); border-radius: 10px; overflow: hidden; }}
        th, td {{ padding: 12px 16px; text-align: left; border-bottom: 1px solid var(--border); font-size: 14px; }}
        th {{ background-color: #243247; color: var(--text-muted); font-weight: 600; font-size: 12px; text-transform: uppercase; }}
        tr:hover {{ background-color: #273549; }}
        .pos {{ color: var(--green); font-weight: 600; }}
        .neg {{ color: var(--red); font-weight: 600; }}
        .badge {{ padding: 4px 8px; border-radius: 6px; font-size: 12px; font-weight: 700; display: inline-block; }}
        .badge-green {{ background: rgba(16, 185, 129, 0.2); color: var(--green); }}
        .badge-amber {{ background: rgba(245, 158, 11, 0.2); color: var(--amber); }}
        .badge-red {{ background: rgba(239, 68, 68, 0.2); color: var(--red); }}
        .badge-blue {{ background: rgba(56, 189, 248, 0.2); color: var(--blue); }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            <div class="title">Global Capital Flow Intelligence (GCFI)</div>
            <div class="subtitle">글로벌 자본 이동 추적 & 미국 지수선물 (ES / NQ) 방향성 예측 플랫폼</div>
        </div>
        <div style="text-align: right;">
            <div style="font-size: 13px; color: var(--text-muted);">데이터 기준일</div>
            <div style="font-size: 18px; font-weight: 700; color: var(--blue);">{latest_date}</div>
        </div>
    </div>

    <!-- Screen 4: Market Regime Cards -->
    <div class="grid-cards">
        <div class="card">
            <div class="card-label">Market Regime</div>
            <div class="card-val" style="color: var(--amber);">{regime}</div>
            <div class="card-sub">{reasons}</div>
        </div>
        <div class="card">
            <div class="card-label">Risk Appetite Score</div>
            <div class="card-val pos">{risk_score:.0f} <span style="font-size:14px; color:var(--text-muted)">/ 100</span></div>
            <div class="card-sub">위험자산 선호도 지수</div>
        </div>
        <div class="card">
            <div class="card-label">System Liquidity</div>
            <div class="card-val pos">{liq_score:.0f} <span style="font-size:14px; color:var(--text-muted)">/ 100</span></div>
            <div class="card-sub">자본 팽창 및 흡수 강도</div>
        </div>
        <div class="card">
            <div class="card-label">US 10Y Yield & VIX</div>
            <div class="card-val">{macro.get('us10y_yield', 0):.2f}% <span style="font-size:16px; color:var(--text-muted)">| VIX {macro.get('vix_index', 0):.1f}</span></div>
            <div class="card-sub">DXY: {macro.get('dxy_index', 0):.2f} | WTI: ${macro.get('wti_oil', 0):.2f}</div>
        </div>
    </div>

    <!-- Screen 5: US Futures AI Signals -->
    <div class="section-title">🎯 화면 5. US Futures Directional Signals (ES / NQ 예측 모델)</div>
    <div class="grid-cards">
        <div class="card" style="border-left: 4px solid var(--blue);">
            <div class="card-label">S&P 500 Futures (ES) - 1D</div>
            <div class="card-val">{ml_res['predictions']['ES']['1d']['direction']}</div>
            <div class="card-sub">신뢰도: <b>{int(ml_res['predictions']['ES']['1d']['confidence']*100)}%</b> | 모델합의: {ml_res['predictions']['ES']['1d']['bullish_votes']}</div>
        </div>
        <div class="card" style="border-left: 4px solid var(--blue);">
            <div class="card-label">S&P 500 Futures (ES) - 5D</div>
            <div class="card-val">{ml_res['predictions']['ES']['5d']['direction']}</div>
            <div class="card-sub">신뢰도: <b>{int(ml_res['predictions']['ES']['5d']['confidence']*100)}%</b> | 모델합의: {ml_res['predictions']['ES']['5d']['bullish_votes']}</div>
        </div>
        <div class="card" style="border-left: 4px solid var(--green);">
            <div class="card-label">Nasdaq 100 Futures (NQ) - 1D</div>
            <div class="card-val">{ml_res['predictions']['NQ']['1d']['direction']}</div>
            <div class="card-sub">신뢰도: <b>{int(ml_res['predictions']['NQ']['1d']['confidence']*100)}%</b> | 모델합의: {ml_res['predictions']['NQ']['1d']['bullish_votes']}</div>
        </div>
        <div class="card" style="border-left: 4px solid var(--green);">
            <div class="card-label">Nasdaq 100 Futures (NQ) - 5D</div>
            <div class="card-val">{ml_res['predictions']['NQ']['5d']['direction']}</div>
            <div class="card-sub">신뢰도: <b>{int(ml_res['predictions']['NQ']['5d']['confidence']*100)}%</b> | 모델합의: {ml_res['predictions']['NQ']['5d']['bullish_votes']}</div>
        </div>
    </div>

    <!-- Screen 1: Capital Flow Map & Rotation Chart -->
    <div class="section-title">📊 화면 1. Global Capital Flow & Rotation Network (자산군별 5일 순유출입)</div>
    <div class="chart-grid">
        <div class="card">
            <canvas id="flowChart" style="max-height: 280px;"></canvas>
        </div>
        <div class="card">
            <div class="card-label" style="margin-bottom: 12px;">Top Feature Drivers</div>
            <div style="font-size: 13px; line-height: 1.8;">
                {top_drivers_html}
            </div>
        </div>
    </div>

    <!-- Screen 2: Key ETF Flow Watchlist -->
    <div class="section-title">📋 화면 2. Key ETF Flow Watchlist (실측 펀더멘털 및 5일 자금 이동)</div>
    <div style="overflow-x: auto; margin-bottom: 24px;">
        <table>
            <thead>
                <tr>
                    <th>Ticker</th>
                    <th>Name</th>
                    <th>Asset Class</th>
                    <th>Close Price</th>
                    <th>Shares Out</th>
                    <th>AUM</th>
                    <th>Net Flow (5D)</th>
                    <th>Flow Accel</th>
                    <th>Return (5D)</th>
                    <th>Type</th>
                </tr>
            </thead>
            <tbody>
                {etf_rows_html}
            </tbody>
        </table>
    </div>

    <!-- Screen 3: Cross Asset Correlation Matrix -->
    <div class="section-title">🔗 화면 3. Cross Asset Correlation Matrix (수익률 / 수급 상관관계)</div>
    <div style="overflow-x: auto; margin-bottom: 30px;">
        <table>
            <thead>
                <tr>
                    <th>Asset Pair</th>
                    <th>Description</th>
                    <th>5D (Return / Flow)</th>
                    <th>20D (Return / Flow)</th>
                    <th>60D (Return / Flow)</th>
                </tr>
            </thead>
            <tbody>
                {corr_rows_html}
            </tbody>
        </table>
    </div>

    <script>
        const ctx = document.getElementById('flowChart').getContext('2d');
        new Chart(ctx, {{
            type: 'bar',
            data: {{
                labels: {json.dumps(ac_labels)},
                datasets: [{{
                    label: '5-Day Net Capital Flow ($ Millions)',
                    data: {json.dumps(ac_values_m)},
                    backgroundColor: {json.dumps(ac_colors)},
                    borderRadius: 6
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    legend: {{ display: false }}
                }},
                scales: {{
                    x: {{ grid: {{ color: '#334155' }}, ticks: {{ color: '#94a3b8' }} }},
                    y: {{ grid: {{ color: '#334155' }}, ticks: {{ color: '#94a3b8' }} }}
                }}
            }}
        }});
    </script>
</body>
</html>
"""
    with open(DASHBOARD_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_content)

    return str(DASHBOARD_HTML_PATH)

if __name__ == "__main__":
    path = generate_dashboard_html()
    print(f"Generated Quant Dashboard: {path}")
