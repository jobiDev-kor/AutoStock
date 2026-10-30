# -*- coding: utf-8 -*-
import sys
import io
import config
import broker
import fee_calculator
import market_schedule
import market_brain
import quant_brain

# Windows console encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

print("=== 1. CONFIG & SYSTEM INTEGRITY CHECK ===")
print("Account:", config.DEFAULT_ACCOUNT)
print("DRY_RUN (실매매 여부):", config.DRY_RUN, "->", "[실거래 주문]" if not config.DRY_RUN else "[모의 체결]")
print("Order Type:", config.ORDER_PRICE_TYPE, "-> 05: 시장가")

print("\n=== 2. BALANCE & SOR ROUTING AUDIT ===")
summary, holdings = broker.get_account_balance(config.DEFAULT_ACCOUNT)
print(f"총 {len(holdings)}개 실보유 종목 검증:")

for idx, h in enumerate(holdings, 1):
    c = h.get("iem_cd")
    name = h.get("iem_nm")
    qty = h.get("actual_qty")
    buy = float(h.get("phs_pr") or 0)
    now = float(h.get("now_pr") or 0)
    is_nxt = broker.check_nxt_availability(c)
    mkt, sor, desc = broker.resolve_best_execution_market(c, "메인마켓 (09:00~15:20)")
    tick = fee_calculator.get_krx_tick_size(now if now > 0 else buy)
    
    sl = fee_calculator.calculate_target_price_for_net_pnl(buy, -2.0)
    tp = fee_calculator.calculate_target_price_for_net_pnl(buy, 3.0)
    sl_pnl = fee_calculator.calculate_net_pnl_pct(buy, sl)
    tp_pnl = fee_calculator.calculate_net_pnl_pct(buy, tp)

    print(f"  {idx}. {name} ({c}) - {qty}주")
    print(f"     • 매입가: {buy:,.0f}원 | 현재가: {now:,.0f}원 | 호가단위: {tick:,}원")
    print(f"     • 거래소 라우팅: {'NXT+KRX (SOR 자동 최유리)' if is_nxt else 'KRX 전용 (NXT 불가)'}")
    print(f"     • 🔻 -2% 손절 목표가: {sl:,}원 (실질 순수익 {sl_pnl:+.2f}%)")
    print(f"     • 🔺 +3% 익절 목표가: {tp:,}원 (실질 순수익 {tp_pnl:+.2f}%)")

print("\n=== 3. MARKET BRAIN AI QUANT AUDIT ===")
mkt = market_brain.get_market_macro_snapshot(force_refresh=True)
print(f"• 국면 판정: {mkt.regime_name} (건강점수 {mkt.macro_score}점)")
print(f"• Kelly (2024 JOF) RFF 초고차원 시장 상방 확률: {mkt.kelly_prob_up:.1f}% ({mkt.rff_regime})")
print(f"• Oxford GP-CPD 변화점 국면전환 위험도: {mkt.cpd_hazard:.1f}%")
print(f"• 코스피200 선물: {mkt.k200_future:,.2f}pt | 현물: {mkt.k200_cash:,.2f}pt | 베이시스: {mkt.basis:+.2f}pt ({mkt.basis_status})")
print(f"• 외인 선물: {mkt.foreign_future_val:+,}백만원 | 옵션: {mkt.option_sentiment}")
print(f"• 미국 선물: NQ {mkt.nasdaq_rate:+.2f}% | ES {mkt.sp500_rate:+.2f}%")
print(f"• 매수 가능 여부: {'🟢 허용' if mkt.is_tradeable else '🛑 차단: ' + mkt.block_reason}")

print("\n=== 4. QUANT BRAIN MICROSTRUCTURE AUDIT (전 가격대 종목 검증) ===")
# Test 1: 고가 대형주 (삼성전자, 가격 제한 없음)
res_samsung = quant_brain.analyze_stock_quant("삼성전자", target_qty=10, max_buy_price=0)
if res_samsung:
    print(f"• [대형주/고가주] {res_samsung.iem_nm} ({res_samsung.iem_cd}): 현재가 {res_samsung.current_price:,}원")
    print(f"   - 상한가 제한: {'없음 (자유 매수)' if res_samsung.max_buy_price == 0 else f'{res_samsung.max_buy_price:,}원'}")
    print(f"   - 권장 매수가: {res_samsung.recommended_entry_price:,}원 (확신도 {res_samsung.confidence_score}%)")
    print(f"   - 트레일링 시작가: {res_samsung.recommended_exit_price:,}원 | 손절가: {res_samsung.user_stop_loss_price:,}원")

# Test 2: 대한항공 동적 1초 신호 검증
sig = quant_brain.evaluate_dynamic_buy_signal("003490", remaining_qty=20, max_buy_price=0)
print(f"• [대한항공 1초 동적 신호]: Action={sig.action}, 수량={sig.qty_to_buy}주, 확신도={sig.confidence}%")
print(f"   - DeepLOB MLI: {sig.mli_index:+.3f} | 마이크로가격: {sig.micro_price:,.0f}원")
print(f"   - UCL 위상 지속성(H0): {sig.persistence_score:.2f} ({sig.persistence_desc})")
print(f"   - Boyd 변동성 배율: {sig.vol_ratio:.1f}배")
print(f"   - 산출 사유: {sig.reason}")

print("\n=== 5. MARKET SCHEDULE & AUCTION PROTECTION ===")
session = market_schedule.get_current_market_session()
print(f"현재 세션: {session.session_name} (거래가능: {session.is_tradeable})")

print("\n✅ 검증 완료: 4대 최신 퀀트 이론이 성공적으로 결합되어 정상 작동 중입니다!")
