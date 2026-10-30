"""AutoStock 시세포착 및 실시간 감시 엔진 (engine.py)

1. MTS 매수 체결 감지: 계좌 잔고를 초 단위로 모니터링하여 새 종목 유입 시 텔레그램으로 입고 알림 발송
2. 대화형 설정: 사용자가 텔레그램으로 로스컷/상승컷 숫자를 입력하기 전까지 감시 대기(WAITING_CONFIG)
3. 제비용 및 실제 평단가 반영: 수수료+거래세를 감안한 실질 순수익 기준으로 로스컷/상승컷 관리
4. 시세포착 기능: 호가창에 미리 주문을 내지 않고 조건 도달 시 즉각 매도 집행
5. 단일가 매매 제외: 프리마켓/메인마켓/애프터마켓 접속매매 시간에만 집행
"""
import time
from datetime import datetime
from dataclasses import dataclass
import logging
import config
import broker
import market_schedule
import fee_calculator
import storage
import quant_brain
from buster_call import buster_call_manager
from telegram_bot import TelegramNotifier

logger = logging.getLogger("AutoStock.Engine")

@dataclass
class WatchItem:
    iem_cd: str
    iem_nm: str
    hold_qty: int
    sellable_qty: int
    buy_price: float
    effective_buy_price: float = 0.0
    stop_loss_price: int = 0
    profit_cut_price: int = 0
    target_type: str = "PRICE"         # "PERCENT" 또는 "PRICE"
    loss_pct: float = 0.0              # 설정된 손절률 (%)
    profit_pct: float = 0.0            # 설정된 익절률 (%)
    current_price: int = 0
    pnl_pct: float = 0.0
    status: str = "WAITING_CONFIG"     # WAITING_CONFIG, MONITORING, ORDER_SENT, DONE
    detected_time: str = ""
    last_checked_time: str = ""
    # ── 트레일링 익절(Trailing Stop) 설정 및 상태 ──
    use_trailing: bool = False         # 트레일링 익절 사용 여부 ('트' 입력 시 True)
    trailing_active: bool = False      # 트레일링 익절 활성화 여부
    highest_price: int = 0             # 활성화 후 기록한 최고가
    trailing_fallback_pct: float = 0.8 # 최고가 대비 반락 허용폭 (% 기본 0.8%)
    trailing_stop_price: int = 0       # 동적으로 상향 조정되는 트레일링 매도 트리거선

    def __post_init__(self):
        if self.effective_buy_price <= 0 and self.buy_price > 0:
            self.effective_buy_price = fee_calculator.get_effective_buy_price(self.buy_price)
        if not self.detected_time:
            self.detected_time = datetime.now().strftime("%H:%M:%S")


@dataclass
class AIBuyTarget:
    iem_cd: str
    iem_nm: str
    target_price: int                  # AI 매수 타점
    target_qty: int                    # 주문 예정 수량
    budget: int = 0                    # 예산 한도
    max_buy_price: int = 0             # 매수 제한 상한선 (이 가격 초과 시 매수 금지)
    total_target_qty: int = 0          # 목표 총 매수 수량 (예: 20주)
    remaining_qty: int = 0             # 남은 미체결 수량
    accumulated_qty: int = 0           # 누적 매수 체결 수량
    accumulated_amt: float = 0.0       # 누적 매수 체결 금액
    avg_buy_price: float = 0.0         # 누적 평균 매입단가
    sell_target_price: int = 0         # AI 매도 타점 (트레일링 익절 시작선)
    stop_loss_price: int = 0           # 대표님 지정 로스컷가
    loss_pct: float = -5.0             # 대표님 지정 손절률 (기본 -5.0%)
    min_profit_pct: float = 5.0        # 대표님 지정 최소 익절률 및 트레일링 시작선 (기본 +5.0%)
    use_dynamic_ai: bool = True        # 1초마다 실시간 수급 동적 계산 여부 (옵션 B)
    auto_buy: bool = True              # 자동 매수 여부 (옵션 B 기본 True)
    confidence: int = 0                # 신뢰도 점수
    created_time: str = ""
    last_notified_time: float = 0.0
    status: str = "WAITING_PRICE"      # WAITING_PRICE, BOUGHT, CANCELLED

    def __post_init__(self):
        if not self.created_time:
            self.created_time = datetime.now().strftime("%H:%M:%S")
        if self.total_target_qty <= 0:
            self.total_target_qty = self.target_qty
        if self.remaining_qty <= 0:
            self.remaining_qty = self.total_target_qty


class AutoStockEngine:
    def __init__(self, act_no: str):
        self.act_no = act_no
        self.tracked_items: dict[str, WatchItem] = {}
        self.ai_buy_targets: dict[str, AIBuyTarget] = {}
        self.history_log: list[str] = []
        self.account_summary: dict = {}
        self.telegram = TelegramNotifier()
        self.is_running: bool = False
        self._first_scan_done: bool = False
        self.buster_call = buster_call_manager
        self._sent_briefing_slots: set[str] = set()

        # 저장된 AI 매수 타점 로드
        saved_ai = storage.load_ai_targets()
        for c, d in saved_ai.items():
            if d.get("is_active"):
                t_qty = int(d.get("total_target_qty") or d.get("target_qty") or 20)
                r_qty = int(d.get("remaining_qty") or t_qty)
                self.ai_buy_targets[c] = AIBuyTarget(
                    iem_cd=c,
                    iem_nm=d.get("iem_nm", c),
                    target_price=int(d.get("target_price", 0)),
                    target_qty=t_qty,
                    budget=int(d.get("budget", 0)),
                    max_buy_price=int(d.get("max_buy_price", 0)),
                    total_target_qty=t_qty,
                    remaining_qty=r_qty,
                    accumulated_qty=int(d.get("accumulated_qty", 0)),
                    accumulated_amt=float(d.get("accumulated_amt", 0.0)),
                    avg_buy_price=float(d.get("avg_buy_price", 0.0)),
                    sell_target_price=int(d.get("sell_target_price", 0)),
                    stop_loss_price=int(d.get("stop_loss_price", 0)),
                    loss_pct=float(d.get("loss_pct", -5.0)),
                    min_profit_pct=float(d.get("min_profit_pct", 5.0)),
                    use_dynamic_ai=bool(d.get("use_dynamic_ai", True)),
                    auto_buy=bool(d.get("auto_buy", True)),
                    confidence=int(d.get("confidence", 0)),
                    created_time=datetime.now().strftime("%H:%M:%S")
                )

    def add_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.history_log.append(entry)
        if len(self.history_log) > 50:
            self.history_log.pop(0)
        logger.info(msg)

    def scan_account_for_new_positions(self):
        """계좌 잔고를 조회하여 새로 매수되어 들어온 종목을 감지합니다."""
        summary, holdings = broker.get_account_balance(self.act_no)
        self.account_summary = summary

        saved_targets = storage.load_targets()
        current_codes = set()

        for item in holdings:
            code = item.get("iem_cd")
            if not code:
                continue
            name = item.get("iem_nm", code)
            # 실제 체결기준 현재 보유수량 (rsdl_qty: 잔여수량)
            qty = int(float(item.get("actual_qty") or item.get("rsdl_qty") or 0))
            if qty <= 0:
                continue

            current_codes.add(code)
            buy_price = float(item.get("phs_pr") or item.get("now_pr") or 0)
            now_pr = int(float(item.get("now_pr") or 0))

            # 새로 감지된 종목
            if code not in self.tracked_items:
                sellable = broker.get_sellable_quantity(self.act_no, code)
                effective_qty = sellable if sellable > 0 else qty

                # 이전에 이미 저장된 목표값이 있는 경우 바로 복원
                saved = saved_targets.get(code)
                if saved and saved.get("is_active"):
                    watch = WatchItem(
                        iem_cd=code,
                        iem_nm=name,
                        hold_qty=qty,
                        sellable_qty=effective_qty,
                        buy_price=buy_price,
                        stop_loss_price=saved["stop_loss_price"],
                        profit_cut_price=saved["profit_cut_price"],
                        target_type=saved.get("target_type", "PRICE"),
                        loss_pct=saved.get("loss_pct", 0.0),
                        profit_pct=saved.get("profit_pct", 0.0),
                        current_price=now_pr,
                        status="MONITORING",
                        use_trailing=bool(saved.get("use_trailing", False))
                    )
                    self.tracked_items[code] = watch
                    self.add_log(f"🔄 [기존 설정 복원] {name}({code}) 손절가: {watch.stop_loss_price:,}원 | 익절가: {watch.profit_cut_price:,}원")
                else:
                    # 신규 종목 입고: 텔레그램 알림 후 사용자 입력 대기 (WAITING_CONFIG)
                    watch = WatchItem(
                        iem_cd=code,
                        iem_nm=name,
                        hold_qty=qty,
                        sellable_qty=effective_qty,
                        buy_price=buy_price,
                        current_price=now_pr,
                        status="WAITING_CONFIG"
                    )
                    self.tracked_items[code] = watch
                    self.add_log(f"🔔 [보유 종목 인지] {name}({code}) {qty}주 | 매입단가 {buy_price:,.0f}원 (텔레그램 설정 대기)")

                    # 첫 실행 이후 새로 매수된 종목에 대해서만 개별 입고 알림 발송
                    if self._first_scan_done:
                        self.telegram.notify_new_position(code, name, qty, buy_price)

            else:
                # 🔴 이미 감시 중인 종목: 추가 매수로 인한 수량 증가 및 평단가 변동 감지
                watch = self.tracked_items[code]
                old_qty = watch.hold_qty
                old_price = watch.buy_price
                new_price = buy_price

                # 수량이 증가했거나 가중평균 매입단가가 변경된 경우
                if qty > old_qty or (new_price > 0 and abs(new_price - old_price) >= 1.0):
                    watch.hold_qty = qty
                    watch.buy_price = new_price
                    watch.effective_buy_price = fee_calculator.get_effective_buy_price(new_price)
                    sellable = broker.get_sellable_quantity(self.act_no, code)
                    watch.sellable_qty = sellable if sellable > 0 else qty

                    # A) 비율(%) 기준으로 설정되어 있던 경우: 신규 2주 가중평단가 기준으로 목표가 자동 재계산!
                    if watch.target_type == "PERCENT" and watch.loss_pct != 0:
                        watch.stop_loss_price = fee_calculator.calculate_target_price_for_net_pnl(new_price, watch.loss_pct)
                        watch.profit_cut_price = fee_calculator.calculate_target_price_for_net_pnl(new_price, watch.profit_pct)

                        storage.update_stock_target(
                            iem_cd=code,
                            iem_nm=name,
                            buy_price=new_price,
                            stop_loss_price=watch.stop_loss_price,
                            profit_cut_price=watch.profit_cut_price,
                            target_type="PERCENT",
                            loss_pct=watch.loss_pct,
                            profit_pct=watch.profit_pct,
                            use_trailing=watch.use_trailing
                        )

                        self.add_log(
                            f"🔄 [추가 매수: 평단가 갱신] {name}({code}) {old_qty}주 -> {qty}주 | "
                            f"신규 평단가: {new_price:,.0f}원 | "
                            f"갱신 손절가: {watch.stop_loss_price:,}원({watch.loss_pct:.2f}%) | 갱신 익절가: {watch.profit_cut_price:,}원({watch.profit_pct:+.2f}%)"
                        )

                        self.telegram.send_message(
                            f"🔄 <b>[추가 매수 감지: 평단가 및 목표가 자동 갱신]</b>\n\n"
                            f"• <b>종목명</b>: {name} (<code>{code}</code>)\n"
                            f"• <b>보유수량</b>: {old_qty:,}주 ➡️ <b>{qty:,}주</b> (+{qty - old_qty:,}주 추가 매수)\n"
                            f"• <b>신규 평균단가</b>: <b>{new_price:,.0f}원</b> (기존 {old_price:,.0f}원)\n"
                            f"• 🔻 <b>재계산된 로스컷</b>: <b>{watch.stop_loss_price:,}원</b> (순수익 {watch.loss_pct:.2f}%)\n"
                            f"• 🔺 <b>재계산된 수익컷</b>: <b>{watch.profit_cut_price:,}원</b> (순수익 {watch.profit_pct:+.2f}%)\n\n"
                            f"💡 합산된 <b>총 보유수량의 가중평균 매입단가 기준</b>으로 로스컷/수익컷이 자동 갱신되었습니다."
                        )
                    else:
                        # B) 고정 가격(원) 기준이었던 경우: 새 평단가 대비 실질 손익률 안내
                        new_loss_p = fee_calculator.calculate_net_pnl_pct(new_price, watch.stop_loss_price) if watch.stop_loss_price > 0 else 0
                        new_prof_p = fee_calculator.calculate_net_pnl_pct(new_price, watch.profit_cut_price) if watch.profit_cut_price > 0 else 0

                        self.add_log(
                            f"🔄 [추가 매수: 평단가 변동] {name}({code}) {old_qty}주 -> {qty}주 | 신규 평단가: {new_price:,.0f}원"
                        )

                        self.telegram.send_message(
                            f"🔄 <b>[추가 매수 감지: 평단가 변동 알림]</b>\n\n"
                            f"• <b>종목명</b>: {name} (<code>{code}</code>)\n"
                            f"• <b>보유수량</b>: {old_qty:,}주 ➡️ <b>{qty:,}주</b> (+{qty - old_qty:,}주 추가 매수)\n"
                            f"• <b>신규 평균단가</b>: <b>{new_price:,.0f}원</b> (기존 {old_price:,.0f}원)\n"
                            f"• 🔻 기존 설정 로스컷: {watch.stop_loss_price:,}원 (신규 평단가 기준 순수익 {new_loss_p:.2f}%)\n"
                            f"• 🔺 기존 설정 수익컷: {watch.profit_cut_price:,}원 (신규 평단가 기준 순수익 {new_prof_p:+.2f}%)\n\n"
                            f"👉 목표가를 새로 변경하시려면:\n<code>{name} 손절가 익절가</code>를 입력해 주세요."
                        )
                elif qty < old_qty:
                    # 🔴 MTS 수기 일부 매도 감지 (수량 감소)
                    sold_cnt = old_qty - qty
                    watch.hold_qty = qty
                    sellable = broker.get_sellable_quantity(self.act_no, code)
                    watch.sellable_qty = sellable if sellable > 0 else qty

                    self.add_log(f"ℹ️ [MTS 일부 매도 감지] {watch.iem_nm}({code}) {old_qty}주 -> {qty}주 (-{sold_cnt}주 매도)")
                    self.telegram.send_message(
                        f"ℹ️ <b>[MTS 일부 매도 감지]</b>\n\n"
                        f"• <b>종목명</b>: {watch.iem_nm} (<code>{code}</code>)\n"
                        f"• <b>보유수량</b>: {old_qty:,}주 ➡️ <b>{qty:,}주</b> (-{sold_cnt:,}주 매도)\n"
                        f"• <b>조치</b>: 남은 <b>{qty:,}주</b>에 대해 기존 로스컷/상승컷 감시를 유지합니다."
                    )

        # 🔴 잔고 조회가 실패하거나 빈 목록인 경우 감시 목록 보호 (일시적 API 오류 방어)
        if not holdings:
            logger.warning("잔고 조회 결과가 비어 있어 감시 목록 검사를 건너뜁니다.")
            return

        # 🔴 MTS 수기 전량 매도 감지 (계좌에서 실제 잔고 소진 시)
        removed_codes = [c for c in list(self.tracked_items.keys()) if c not in current_codes]
        for c in removed_codes:
            item = self.tracked_items.get(c)
            if not item:
                continue

            # 이중 검증: 매도가능수량 API 직접 확인하여 일시적 목록 누락 방어
            sellable = broker.get_sellable_quantity(self.act_no, c)
            if sellable > 0:
                logger.info(f"종목 {item.iem_nm}({c}) 잔고 목록 일시 누락되었으나 매도가능수량 {sellable}주 확인됨 -> 유지")
                continue

            # 실제로 매도 완료되어 잔고가 0인 경우에만 감시 목록에서 삭제
            self.tracked_items.pop(c, None)
            storage.remove_stock_target(c)
            self.add_log(f"🗑️ [MTS 수기 매도 감지: 감시 폐기] {item.iem_nm}({c}) 잔고 소진 -> 감시 해제")

            # 오직 사용자가 실제로 목표가를 설정하여 감시 중(MONITORING)이었던 종목만 텔레그램 알림 발송
            # (목표가가 설정되지 않은 단순 설정대기 WAITING_CONFIG 종목은 알림 생략)
            if item.status == "MONITORING" and (item.stop_loss_price > 0 or item.profit_cut_price > 0):
                self.telegram.send_message(
                    f"🗑️ <b>[MTS 수기 매도 감지: 감시 폐기 알림]</b>\n\n"
                    f"• <b>종목명</b>: {item.iem_nm} (<code>{c}</code>)\n"
                    f"• <b>상태</b>: MTS에서 전량 매도되어 계좌 잔고가 소진되었습니다.\n"
                    f"• <b>조치</b>: 설정되어 있던 <b>로스컷({item.stop_loss_price:,}원) 및 상승컷({item.profit_cut_price:,}원) 감시를 즉시 폐기(해제)</b>했습니다."
                )

        # 첫 스캔 완료 시 시작 안내 발송 (1회)
        if not self._first_scan_done:
            self._first_scan_done = True
            holdings_cnt = len(self.tracked_items)
            self.telegram.send_message(
                f"🚀 <b>[AutoStock 감시 시스템 ON]</b>\n\n"
                f"• <b>계좌번호</b>: <code>{self.act_no}</code>\n"
                f"• <b>현재 상태</b>: 보유 중인 <b>{holdings_cnt}개 종목</b>이 안전하게 <b>[설정 대기]</b> 상태로 준비되었습니다.\n"
                f"• <b>작동 원칙</b>: 텔레그램으로 명령어를 입력하신 종목에 한해서만 시세포착 감시가 활성화됩니다.\n\n"
                f"👉 <code>/잔고</code> : 현재 계좌 종목 전체 조회\n"
                f"👉 <code>대한항공 25000 27500</code> : 원하는 종목만 감시 시작"
            )

    def poll_telegram_commands(self):
        """텔레그램에서 사용자가 보낸 설정/수정/취소 명령 확인."""
        actions = self.telegram.poll_user_commands(self)
        for act in actions:
            self.add_log(act)

    def check_buster_call(self):
        """버스터콜(지수 -1.5% 하향 돌파 시 전액 매도 + 인버스 스위칭) 감시 및 집행."""
        events = self.buster_call.check_and_execute(self)
        for evt in events:
            self.add_log("🚨 [버스터콜 비상 집행 완료]")

    def check_market_regime_transitions(self):
        """실시간 시장 국면 전환(급락/안전/수급순풍) 감지 및 텔레그램 알림."""
        import market_brain
        alert = market_brain.check_and_notify_market_regime_change(self.telegram)
        if alert:
            self.add_log(f"🌐 [시장 국면 전환 알림] {market_brain.get_current_regime_name()}")

    def check_scheduled_briefings(self):
        """새벽 06:30, 아침 07:30에 자동으로 시장 매크로 브리핑(/시장)을 텔레그램으로 전송합니다. (주말/공휴일 제외)"""
        now = datetime.now()
        
        # 1. 주말 및 법정 공휴일(증시 휴장일)에는 발송하지 않음
        if market_schedule.is_market_holiday(now.date()):
            return

        t = now.time()
        today_str = now.strftime("%Y-%m-%d")

        # 오래된 슬롯 키 정리 (최근 3일치만 유지)
        if len(self._sent_briefing_slots) > 10:
            self._sent_briefing_slots = {k for k in self._sent_briefing_slots if k.startswith(today_str)}

        # 정기 브리핑 슬롯 정의: (슬롯ID, 목표시, 목표분, 제목헤더)
        slots = [
            ("0630", 6, 30, "🌅 <b>[새벽 06:30 모닝 매크로 정기 브리핑]</b>\n\n"),
            ("0730", 7, 30, "☀️ <b>[아침 07:30 개장전 시장 & AI 예측 정기 브리핑]</b>\n\n")
        ]
        for slot_id, target_hour, target_min, slot_header in slots:
            slot_key = f"{today_str}_{slot_id}"
            if slot_key in self._sent_briefing_slots:
                continue

            # 정확히 해당 시/분에 진입했는지 확인
            if t.hour == target_hour and t.minute == target_min:
                self._sent_briefing_slots.add(slot_key)
                try:
                    import market_brain
                    import buster_call
                    mkt = market_brain.get_market_macro_snapshot(force_refresh=True)
                    mgr = getattr(self, "buster_call", None) or buster_call.buster_call_manager
                    bc_st = mgr.get_status_summary()
                    bc_icon = "🟢 ON" if bc_st["enabled"] else "⚪ OFF"
                    bc_footer = f"\n\n🚨 <b>버스터콜 비상 헤지</b>: <b>{bc_icon}</b> (장전 매수차단+Kelly급락 시 현물유지 & 인버스 동일금액 헤지매수)"
                    
                    full_text = slot_header + mkt.briefing_text + bc_footer
                    sent = self.telegram.send_message(full_text)
                    if sent:
                        self.add_log(f"⏰ [정기 브리핑 발송 완료] {slot_id} ({now.strftime('%H:%M:%S')})")
                    else:
                        self.add_log(f"⚠️ [정기 브리핑 발송 실패] {slot_id}")
                except Exception as e:
                    self.add_log(f"❌ [정기 브리핑 오류] {slot_id}: {e}")

    def check_market_and_trigger_orders(self):
        """설정이 완료되어 MONITORING 상태인 종목들의 실시간 가격을 확인하고 로스컷/수익컷 조건을 검사합니다."""
        if not self.tracked_items:
            return

        session = market_schedule.get_current_market_session()

        for code, item in list(self.tracked_items.items()):
            # 텔레그램으로 설정을 입력받기 전(WAITING_CONFIG)에는 절대 주문 조건을 발동하지 않음
            if item.status != "MONITORING":
                continue

            quote = broker.get_current_price(code, market_cd="UNT")
            if not quote:
                continue

            # 🔴 VI(변동성완화장치) 발동 중 단일가 호가 왜곡 보호
            # VI 발동 시(2분간 단일가 접수) 출렁이는 예상 가격은 실체결가가 아니므로 주문을 보류하고 해제 후 실제 체결가를 기다림
            mkt_status = str(quote.get("market_status") or "").strip().upper()
            if mkt_status in ("E", "F", "D"):
                # E: KRX VI 발동, F: NXT VI 발동, D: 단일가/정지
                self.add_log(
                    f"⚠️ [VI 발동 감지: 주문 보류] {item.iem_nm}({code}) 상태={mkt_status}. "
                    f"2분간 단일가 호가 왜곡 구간이므로 VI 해제 및 정상 체결 재개 시까지 대기합니다."
                )
                continue

            cur_price = int(quote.get("stck_prpr") or quote.get("main_cls_prpr") or 0)
            if cur_price <= 0:
                continue

            item.current_price = cur_price
            if item.buy_price > 0:
                # 제비용(수수료+거래세)이 감안된 실질 순수익률 계산
                item.pnl_pct = fee_calculator.calculate_net_pnl_pct(item.buy_price, cur_price)
            item.last_checked_time = datetime.now().strftime("%H:%M:%S")

            # 1) 로스컷 검사 (현재가 <= 손절가: 갭 하락 시에도 즉각 발동)
            if item.stop_loss_price > 0 and cur_price <= item.stop_loss_price:
                self.add_log(
                    f"🚨 [로스컷 도달] {item.iem_nm}({code}) "
                    f"현재가 {cur_price:,}원 <= 손절가 {item.stop_loss_price:,}원 (순수익률 {item.pnl_pct:.2f}%)"
                )
                self._handle_trigger(item, session, trigger_type="LOSS_CUT")

            # 2) 수익컷 및 트레일링 스탑(Trailing Stop) 검사
            elif item.profit_cut_price > 0:
                if not item.trailing_active:
                    # 상승컷 기준선 최초 도달 검사
                    if cur_price >= item.profit_cut_price:
                        if item.use_trailing:
                            # 🚀 트레일링 익절 모드 진입: 지금 바로 매도하지 않고 최고가를 추적!
                            item.trailing_active = True
                            item.highest_price = cur_price
                            fallback_pr = int(cur_price * (1 - item.trailing_fallback_pct / 100))
                            item.trailing_stop_price = fee_calculator.align_to_tick_size(
                                max(fallback_pr, item.profit_cut_price), mode="floor"
                            )
                            self.add_log(
                                f"🚀 [트레일링 익절 발동] {item.iem_nm}({code}) 상승컷({item.profit_cut_price:,}원) 돌파! "
                                f"현재가 {cur_price:,}원 (최고가 추적 시작, 익절방어선: {item.trailing_stop_price:,}원)"
                            )
                            self.telegram.send_message(
                                f"🚀 <b>[트레일링 익절 모드 발동!]</b>\n\n"
                                f"• <b>종목명</b>: <b>{item.iem_nm}</b> (<code>{code}</code>)\n"
                                f"• <b>상승컷 돌파</b>: <b>{cur_price:,}원</b> (+{item.pnl_pct:.2f}%)\n"
                                f"• <b>전략</b>: <b>수익 극대화 트레일링 스탑 가동</b>\n"
                                f"  💡 <i>지금 바로 매도하지 않고, 주가가 더 치솟을 때마다 익절선을 끌어올립니다!</i>\n"
                                f"• <b>현재 익절 방어선</b>: <b>{item.trailing_stop_price:,}원</b>\n"
                                f"  (최고가 대비 {item.trailing_fallback_pct}% 반락 시 자동 매도)"
                            )
                        else:
                            self.add_log(
                                f"🎉 [수익컷 도달] {item.iem_nm}({code}) "
                                f"현재가 {cur_price:,}원 >= 익절가 {item.profit_cut_price:,}원 (순수익률 {item.pnl_pct:+.2f}%)"
                            )
                            self._handle_trigger(item, session, trigger_type="PROFIT_CUT")
                else:
                    # 🚀 이미 트레일링 스탑이 가동 중인 상태
                    if cur_price > item.highest_price:
                        old_high = item.highest_price
                        item.highest_price = cur_price
                        new_stop = fee_calculator.align_to_tick_size(
                            int(cur_price * (1 - item.trailing_fallback_pct / 100)), mode="floor"
                        )
                        # 방어선은 오직 상향만 가능 (하향 불가)
                        if new_stop > item.trailing_stop_price:
                            item.trailing_stop_price = new_stop

                        self.add_log(
                            f"📈 [트레일링 고가 갱신] {item.iem_nm}: {old_high:,}원 ➡️ {cur_price:,}원 (+{item.pnl_pct:.2f}%). "
                            f"익절 방어선 상향 ➡️ {item.trailing_stop_price:,}원"
                        )
                    elif cur_price <= item.trailing_stop_price:
                        # 최고가 달성 후 반락하여 트레일링 방어선 터치 -> 최고 수익 확보 매도!
                        self.add_log(
                            f"🎉 [트레일링 익절 매도 발동] {item.iem_nm} 최고가 {item.highest_price:,}원 달성 후 "
                            f"반락 {cur_price:,}원 <= 방어선 {item.trailing_stop_price:,}원 터치!"
                        )
                        self._handle_trigger(item, session, trigger_type="TRAILING_PROFIT")

    def _handle_trigger(self, item: WatchItem, session: market_schedule.MarketSession, trigger_type: str):
        """조건 충족 시 단일가 시간대 검증 후 최유리 거래소(SOR/KRX/NXT)로 즉시 매도 주문 집행."""
        if not session.is_tradeable:
            self.add_log(
                f"⚠️ [주문 보류] 현재 세션: '{session.session_name}'. 단일가 구간이므로 실시간 주문을 대기합니다."
            )
            return

        # 🔴 종목별 거래소 지원 여부(KRX 전용 vs KRX+NXT 복합) 및 현재 세션에 따라 최유리 시장 결정
        target_mkt, sor_split, mkt_desc = broker.resolve_best_execution_market(item.iem_cd, session.session_name)

        # 예외: 프리마켓(08:00~08:50)에 KRX 전용 종목(NXT 미지원)인 경우 -> 09:00 KRX 개장 시까지 대기
        if "프리마켓" in session.session_name and target_mkt == "KRX":
            self.add_log(
                f"⚠️ [KRX 전용 종목 대기] {item.iem_nm}은(는) KRX 전용 종목으로 NXT 프리마켓에서 거래할 수 없습니다. "
                f"09:00 KRX 메인마켓 개장 즉시 집행 대기합니다."
            )
            return

        item.status = "ORDER_SENT"
        qty_to_sell = item.sellable_qty if item.sellable_qty > 0 else item.hold_qty

        self.add_log(
            f"⚡ [즉시 매도 실행] {item.iem_nm}({item.iem_cd}) {qty_to_sell}주 "
            f"타입: {trigger_type} | 거래소: {target_mkt} ({mkt_desc})"
        )

        res = broker.execute_sell_order(
            act_no=self.act_no,
            iem_cd=item.iem_cd,
            qty=qty_to_sell,
            price=item.current_price,
            order_type=config.ORDER_PRICE_TYPE,
            rmt_mkt_cd=target_mkt,
            dry_run=config.DRY_RUN
        )

        # 텔레그램 체결 알림 문구
        if trigger_type == "LOSS_CUT":
            type_korean = "🚨 로스컷(손절)"
        elif trigger_type == "TRAILING_PROFIT":
            type_korean = f"🎉 트레일링 익절 (최고가 {item.highest_price:,}원 추적 매도)"
        else:
            type_korean = "🎉 수익컷(익절)"

        if res.get("success"):
            item.status = "DONE"
            storage.remove_stock_target(item.iem_cd)

            self.telegram.send_message(
                f"📢 <b>[{type_korean} 매도 주문 접수 완료]</b>\n\n"
                f"• <b>종목</b>: {item.iem_nm} (<code>{item.iem_cd}</code>)\n"
                f"• <b>체결단가</b>: {item.current_price:,}원\n"
                f"• <b>수량</b>: {qty_to_sell:,}주\n"
                f"• <b>집행 거래소</b>: <b>{target_mkt}</b> ({mkt_desc})\n"
                f"• <b>주문번호</b>: {res.get('order_no') or '-'}\n"
                f"• <b>최종 실질 수익률</b>: {item.pnl_pct:+.2f}%\n"
                f"• <b>구분</b>: {'[가상 모의체결]' if config.DRY_RUN else '[실제 매도 주문]'}"
            )
        else:
            err_msg = res.get('error') or res.get('rsp_msg') or res.get('reason') or "알 수 없는 오류"
            self.add_log(f"❌ [주문 접수 실패] {item.iem_nm}: {err_msg}")
            self.telegram.send_message(
                f"🚨 <b>[{type_korean} 매도 주문 전송 실패!]</b>\n\n"
                f"• <b>종목</b>: {item.iem_nm} (<code>{item.iem_cd}</code>)\n"
                f"• <b>실패 사유</b>: <b>{err_msg}</b>\n"
                f"• <b>현재가</b>: {item.current_price:,}원\n"
                f"• <b>조치</b>: 감시 상태를 유지하며 즉시 재시도 대기합니다."
            )
            item.status = "MONITORING"

    def check_ai_buy_targets(self):
        """AI 매수 타점 감시 대상 종목들의 가격과 수급을 1초마다 동적 체크하여 매수를 집행합니다."""
        if not self.ai_buy_targets:
            return

        # 🔴 [버스터콜 연동] 버스터콜(비상 헤지)이 켜져 있는 동안에는 신규 주식 AI 매수를 일체 중단(휴면)합니다.
        if getattr(self, "buster_call", None) and self.buster_call.is_enabled:
            return



        session = market_schedule.get_current_market_session()

        for code, target in list(self.ai_buy_targets.items()):
            quote = broker.get_current_price(code, market_cd="UNT")
            if not quote:
                continue

            mkt_status = str(quote.get("market_status") or "").strip().upper()
            if mkt_status in ("E", "F", "D"):
                continue

            cur_price = int(quote.get("stck_prpr") or quote.get("main_cls_prpr") or 0)
            if cur_price <= 0:
                continue

            # 🔴 해당 종목 소속 시장(KOSPI vs KOSDAQ)에 맞춰 독립적인 플래시 프리즈 가드
            import market_brain
            m_type = quant_brain.get_stock_market_type(code, quote)
            is_frozen, freeze_reason, rem_sec = market_brain.is_flash_frozen(m_type)
            if is_frozen:
                continue

            # ── [옵션 B] 1초마다 실시간 수급(OFI, 지지벽, VWAP)을 동적 재연산하여 매수 집행 ──
            if target.use_dynamic_ai:
                if not session.is_tradeable:
                    continue

                decision = quant_brain.evaluate_dynamic_buy_signal(code, target.remaining_qty, target.max_buy_price)
                if decision.action in ("BUY_FULL", "BUY_SPLIT") and decision.qty_to_buy > 0 and target.auto_buy:
                    self.add_log(f"🎯 [AI 동적 매수 발동] {target.iem_nm}({code}) 확신도 {decision.confidence}% -> {decision.qty_to_buy}주 매수 주문")
                    res = broker.execute_buy_order(
                        act_no=self.act_no,
                        iem_cd=code,
                        qty=decision.qty_to_buy,
                        price=decision.current_price,
                        order_type="01",  # 최유리 지정가
                        rmt_mkt_cd="AUTO",
                        dry_run=config.DRY_RUN
                    )

                    if res.get("success"):
                        target.accumulated_qty += decision.qty_to_buy
                        target.accumulated_amt += decision.qty_to_buy * decision.current_price
                        target.avg_buy_price = round(target.accumulated_amt / max(target.accumulated_qty, 1), 2)
                        target.remaining_qty -= decision.qty_to_buy

                        storage.update_ai_target(
                            iem_cd=code,
                            iem_nm=target.iem_nm,
                            target_price=target.target_price,
                            target_qty=target.total_target_qty,
                            budget=target.budget,
                            max_buy_price=target.max_buy_price,
                            sell_target_price=target.sell_target_price,
                            stop_loss_price=target.stop_loss_price,
                            loss_pct=target.loss_pct,
                            min_profit_pct=target.min_profit_pct,
                            total_target_qty=target.total_target_qty,
                            remaining_qty=target.remaining_qty,
                            accumulated_qty=target.accumulated_qty,
                            accumulated_amt=target.accumulated_amt,
                            use_dynamic_ai=True,
                            auto_buy=True,
                            confidence=decision.confidence
                        )

                        mode_name = "일괄 100% 매수" if decision.action == "BUY_FULL" else "50% 분할 진입"
                        self.telegram.send_message(
                            f"🎯 <b>[AI 동적 퀀트 매수 집행]</b>\n\n"
                            f"• <b>종목명</b>: <b>{target.iem_nm}</b> (<code>{code}</code>)\n"
                            f"• <b>체결 수량</b>: <b>{decision.qty_to_buy:,}주</b> ({mode_name})\n"
                            f"• <b>체결 단가</b>: <b>{decision.current_price:,}원</b>\n"
                            f"• <b>수학적 확신도</b>: <b>{decision.confidence}%</b> (MLI {decision.mli_index:+.2f}, OFI {decision.ofi_ratio}배, 마이크로가 {decision.micro_price:,.0f}원)\n"
                            f"• <b>매수 근거</b>: {decision.reason}\n"
                            f"• <b>진행 현황</b>: 총 {target.total_target_qty}주 중 <b>{target.accumulated_qty}주 확보</b> "
                            f"(평단가 {target.avg_buy_price:,.0f}원 / 미체결 잔여 {target.remaining_qty}주)\n"
                            f"• <b>모드</b>: {'🟡 가상체결 (DRY_RUN)' if config.DRY_RUN else '🔴 실매매 체결'}"
                        )

                        # 목표 수량 전량 달성 시 포지션 감시로 자동 전환!
                        if target.remaining_qty <= 0:
                            stop_pr = fee_calculator.calculate_target_price_for_net_pnl(target.avg_buy_price, target.loss_pct)
                            profit_pr = fee_calculator.calculate_target_price_for_net_pnl(target.avg_buy_price, target.min_profit_pct)

                            storage.update_stock_target(
                                iem_cd=code,
                                iem_nm=target.iem_nm,
                                buy_price=target.avg_buy_price,
                                stop_loss_price=stop_pr,
                                profit_cut_price=profit_pr,
                                loss_pct=target.loss_pct,
                                profit_pct=target.min_profit_pct
                            )
                            self.ai_buy_targets.pop(code, None)
                            storage.remove_ai_target(code)

                            self.telegram.send_message(
                                f"🎉 <b>[AI 목표 수량 전량 매수 완료!]</b>\n\n"
                                f"• <b>종목명</b>: <b>{target.iem_nm}</b> (<code>{code}</code>)\n"
                                f"• <b>총 체결수량</b>: <b>{target.total_target_qty:,}주</b>\n"
                                f"• <b>최종 평균매입단가</b>: <b>{target.avg_buy_price:,.0f}원</b>\n\n"
                                f"🛡️ <b>[실시간 보호 및 트레일링 가동]</b>\n"
                                f"• 🔻 <b>로스컷 ({target.loss_pct:.1f}%)</b>: <b>{stop_pr:,}원</b>\n"
                                f"• 🚀 <b>최소 수익 (+{target.min_profit_pct:.1f}%) 트레일링 시작</b>: <b>{profit_pr:,}원</b>\n"
                                f"  <i>(+{target.min_profit_pct:.1f}% 돌파 후 즉시 매도하지 않고, 최고가를 추적하여 매도수익을 극대화합니다!)</i>"
                            )
                continue

            # ── 기존 정적 매복형 모드 ──
            if cur_price <= target.target_price:
                if not session.is_tradeable:
                    continue

                # 시장 리스크 거부권 게이트 검사 (코스피 vs 코스닥 디커플링 독립 방어)
                import market_brain
                m_type = quant_brain.get_stock_market_type(code, quote)
                is_mkt_ok, mkt_reason, _ = market_brain.is_market_tradeable_for_stock(m_type)
                if not is_mkt_ok:
                    continue

                if target.auto_buy:
                    self.add_log(f"🎯 [AI 스나이퍼 매수 발동] {target.iem_nm}({code}) 현재가 {cur_price:,}원 <= 목표가 {target.target_price:,}원 -> 매수 주문 전송")
                    res = broker.execute_buy_order(
                        act_no=self.act_no,
                        iem_cd=code,
                        qty=target.target_qty,
                        price=target.target_price,
                        order_type="01",
                        rmt_mkt_cd="AUTO",
                        dry_run=config.DRY_RUN
                    )
                    self.ai_buy_targets.pop(code, None)
                    storage.remove_ai_target(code)

                    sell_tgt = target.sell_target_price if target.sell_target_price > 0 else fee_calculator.calculate_target_price_for_net_pnl(cur_price, target.min_profit_pct)
                    stop_pr = fee_calculator.calculate_target_price_for_net_pnl(cur_price, target.loss_pct)
                    storage.update_stock_target(
                        iem_cd=code,
                        iem_nm=target.iem_nm,
                        buy_price=cur_price,
                        stop_loss_price=stop_pr,
                        profit_cut_price=sell_tgt,
                        loss_pct=target.loss_pct,
                        profit_pct=target.min_profit_pct
                    )
                    self.telegram.send_message(
                        f"🎯 <b>[AI 스나이퍼 매수 체결 완료]</b>\n\n"
                        f"• <b>종목명</b>: <b>{target.iem_nm}</b> (<code>{code}</code>)\n"
                        f"• <b>체결 수량</b>: <b>{target.target_qty:,}주</b>\n"
                        f"• <b>체결 단가</b>: <b>{cur_price:,}원</b>\n"
                        f"• <b>총 체결금액</b>: {cur_price * target.target_qty:,}원\n"
                        f"• <b>모드</b>: {'🟡 가상체결 (DRY_RUN)' if config.DRY_RUN else '🔴 실매매 체결'}\n\n"
                        f"🛡️ <b>[보호 및 트레일링 익절 전략]</b>\n"
                        f"• 🚀 <b>최소 수익 ({target.min_profit_pct:.1f}%) 트레일링 시작</b>: <b>{sell_tgt:,}원</b>\n"
                        f"• 🔻 <b>로스컷 ({target.loss_pct:.1f}%)</b>: <b>{stop_pr:,}원</b>"
                    )
