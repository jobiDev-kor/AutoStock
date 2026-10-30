"""AutoStock — NH나무증권 실시간 MTS 연동 시세포착 자동매매 메인 프로그램

MTS로 수동 매수한 종목을 초단위로 감지하여,
사전에 호가창에 스탑 주문을 내지 않고(시세포착)
로스컷/수익컷 가격 도달 시 단일가 시간을 피해 실시간 즉시 주문을 집행합니다.
"""
import sys
import os
import time
import logging
from datetime import datetime
from tabulate import tabulate

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)

import config
import broker
import market_schedule
from engine import AutoStockEngine

def clear_screen():
    # 윈도우/리눅스 콘솔 클리어
    os.system("cls" if os.name == "nt" else "clear")

def render_dashboard(engine: AutoStockEngine, iteration: int):
    now = datetime.now()
    session = market_schedule.get_current_market_session(now)

    print("=" * 80)
    print(f"🤖  AutoStock — NH투자증권 MTS 연동 시세포착 자동매매 시스템")
    print(f"⏰  현재시각: {now.strftime('%Y-%m-%d %H:%M:%S')}  |  루프 횟수: #{iteration}")
    print(f"📍  현재 세션: {session}")
    tg_status = "🟢 [연동 완료]" if engine.telegram.is_configured else "⚪ [미설정 (.env 에 봇토큰 필요)]"
    print(f"💼  감시 계좌: {engine.act_no}  |  매매모드: {'🟡 [DRY-RUN 모의체결]' if config.DRY_RUN else '🔴 [실거래 주문집행]'}  |  텔레그램: {tg_status}")
    print(f"⚙️  설정값: 종목별 텔레그램 대화형 지정  |  주문: {'시장가(05)' if config.ORDER_PRICE_TYPE=='05' else '지정가(01)'}")
    if hasattr(engine, "buster_call"):
        bc_st = engine.buster_call.get_status_summary()
        bc_badge = "🟢 [가동중 ON]" if bc_st["enabled"] else "⚪ [비활성 OFF]"
        kp_latch = "⚡발동완료" if bc_st["kp_triggered_today"] else "대기"
        kd_latch = "⚡발동완료" if bc_st["kd_triggered_today"] else "대기"
        print(f"🚨  버스터콜: {bc_badge} (장전 매수차단+Kelly급락 시 현물유지 & 인버스 동일금액 헤지) | 코스피[{kp_latch}], 코스닥[{kd_latch}]")
    import market_brain
    is_frz, frz_rsn, frz_rem = market_brain.is_flash_frozen()
    vel = market_brain.get_derivative_velocity()
    frz_badge = f"🚨 [플래시 프리즈 발동 중 (잔여 {frz_rem}초): {frz_rsn}]" if is_frz else "🟢 [정상 안정]"
    print(f"⚡  선물급변 속도: NQ(3분) {vel['nq_delta_3m']:+.2f}%p | K200(3분) {vel['k200_basis_delta_3m']:+.2f}pt | 프리즈: {frz_badge}")
    print("=" * 80)

    # 1. 감시 중인 종목 테이블
    table_data = []
    for code, item in engine.tracked_items.items():
        pnl_str = f"{item.pnl_pct:+.2f}%" if item.pnl_pct != 0 else "0.00%"
        if item.pnl_pct > 0:
            pnl_str = f"▲ {pnl_str}"
        elif item.pnl_pct < 0:
            pnl_str = f"▼ {pnl_str}"

        stop_str = f"{item.stop_loss_price:,}원" if item.stop_loss_price > 0 else "[대기]"
        profit_str = f"{item.profit_cut_price:,}원" if item.profit_cut_price > 0 else "[대기]"
        if getattr(item, 'use_trailing', False) and item.profit_cut_price > 0:
            profit_str = f"🚀{profit_str}(트)"

        status_disp = {
            "WAITING_CONFIG": "🟡 [텔레그램입력대기]",
            "MONITORING": "🟢 [시세포착감시중]",
            "ORDER_SENT": "⚡ [매도주문발송]",
            "DONE": "✅ [체결완료]"
        }.get(item.status, item.status)

        table_data.append([
            item.iem_cd,
            item.iem_nm,
            f"{item.hold_qty:,}주",
            f"{item.buy_price:,.0f}원",
            f"{item.current_price:,}원" if item.current_price > 0 else "-",
            pnl_str,
            stop_str,
            profit_str,
            status_disp,
            item.detected_time
        ])

    headers = [
        "종목코드", "종목명", "보유수량", "매입단가", "현재가", 
        "수익률", "로스컷가", "수익컷가", "상태", "감시시작"
    ]

    print("\n📋 [현재 감시 중인 종목 현황]")
    if table_data:
        print(tabulate(table_data, headers=headers, tablefmt="rounded_grid"))
    else:
        print("   (현재 계좌에 감시 대상 종목이 없습니다. MTS로 종목을 매수하면 자동으로 등록됩니다.)")

    # 1-2. AI 매수 스나이퍼 감시 테이블
    if getattr(engine, "ai_buy_targets", None):
        ai_table = []
        is_bc_on = getattr(engine, "buster_call", None) and engine.buster_call.is_enabled
        for c, t in engine.ai_buy_targets.items():
            if is_bc_on:
                mode = "⏸️ [휴면:버스터콜ON]"
            else:
                mode = "🟢 [자동스나이퍼]" if t.auto_buy else "🟡 [승인대기]"
            ai_table.append([
                t.iem_cd,
                t.iem_nm,
                f"{t.target_price:,}원",
                f"{t.target_qty:,}주",
                f"{t.budget:,}원",
                f"{t.confidence}%",
                mode,
                t.created_time
            ])
        print("\n🧠 [AI 퀀트 매수 스나이퍼 감시 현황]")
        ai_headers = ["종목코드", "종목명", "목표타점", "주문수량", "예산한도", "신뢰도", "모드", "등록시각"]
        print(tabulate(ai_table, headers=ai_headers, tablefmt="rounded_grid"))

    # 2. 최근 감시 및 주문 이벤트 로그
    print("\n📝 [최근 실시간 이벤트 로그 (최신 5건)]")
    if engine.history_log:
        for entry in engine.history_log[-5:]:
            print(f"   {entry}")
    else:
        print("   (아직 발생한 이벤트가 없습니다)")
    print("-" * 80)
    print("💡 중단하려면 언제든지 Ctrl + C 를 누르세요.")


def main():
    act_no = config.DEFAULT_ACCOUNT
    print(f"🚀 AutoStock 엔진을 시작합니다. (대상 계좌: {act_no})")

    engine = AutoStockEngine(act_no=act_no)
    engine.add_log(f"AutoStock 시스템 초기화 완료 (계좌: {act_no}, DRY_RUN={config.DRY_RUN})")

    iteration = 0
    last_balance_scan = 0.0

    try:
        while True:
            iteration += 1
            now_ts = time.time()

            # 1단계: 텔레그램에서 사용자가 보낸 설정/수정/취소 명령 즉시 수신 및 처리
            engine.poll_telegram_commands()

            # 2단계: 계좌 잔고를 검사하여 MTS 신규 매수 종목 감지 (최초 또는 3초 간격)
            if now_ts - last_balance_scan >= 3.0:
                engine.scan_account_for_new_positions()
                last_balance_scan = now_ts

            # 3단계: 텔레그램 명령 재확인 (지연 최소화)
            engine.poll_telegram_commands()

            # 4단계: 감시 종목의 현재가를 확인하고 로스컷/수익컷 도달 시 즉시 매도 주문
            engine.check_market_and_trigger_orders()

            # 4-1단계: AI 매수 감시 종목 가격 확인 및 스나이퍼 매수 집행
            engine.check_ai_buy_targets()

            # 4-2단계: 시장 매크로 국면 전환 (급락/안전/수급순풍) 감지 및 텔레그램 알림
            engine.check_market_regime_transitions()

            # 4-3단계: 버스터콜 (-1.5% 하향 돌파 시 전액 매도 + 인버스 스위칭) 감시 및 집행
            engine.check_buster_call()

            # 4-4단계: 새벽 06:30, 아침 07:30 정기 시장 브리핑 자동 발송 (주말/공휴일 제외)
            engine.check_scheduled_briefings()

            # 5단계: 화면 갱신
            if config.CLEAR_CONSOLE_DASHBOARD:
                clear_screen()
            render_dashboard(engine, iteration)

            # 6단계: 설정된 폴링 주기 대기 (기본 1초)
            time.sleep(1.0)

    except KeyboardInterrupt:
        print("\n\n🛑 사용자에 의해 AutoStock 프로그램이 중단되었습니다.")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 치명적 오류 발생: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
