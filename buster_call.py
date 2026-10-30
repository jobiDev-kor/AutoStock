"""AutoStock 버스터콜(Buster Call) 비상 헤지 및 인버스 자동 매수 관리자 (buster_call.py)

[새로운 작동 원리 및 발동 조건 (사용자 개편 명세)]
1. ON/OFF 스위치:
   - 사용자가 텔레그램 명령(/버스터콜 켜기 / 끄기)으로 제어. 기본값 설정 파일에서 로드.
2. 발동 평가 (장전 /시장 종합 진단):
   - 평가 시점: 오전 08:00 ~ 08:59:59 (장전 시장 진단)
   - 코스피 헤지 발동 조건:
     [코스피 매수차단(not is_kospi_tradeable) + Kelly RFF 비대칭 급락 위험(<=32.0%)]
   - 코스닥 헤지 발동 조건:
     [코스닥 매수차단(not is_kosdaq_tradeable) + Kelly RFF 비대칭 급락 위험(<=32.0%)]
   - 독립적 발동:
     * 코스피만 해당 -> 코스피 인버스만 매수 (코스닥 미매수)
     * 코스닥만 해당 -> 코스닥 인버스만 매수 (코스피 미매수)
     * 둘 다 해당 -> 둘 다 매수
3. 집행 시점:
   - 09:00:00 정규장 개장 즉시 시장가(05)로 인버스 ETF 매수 집행!
4. 현물 100% 보유 및 동액 인버스 헤지 (핵심 원칙):
   - 기존 보유 현물(주식)은 절대 매도하지 않고 100% 안전하게 유지!
   - 계좌 내 코스피 보유 주식 총 매입금액과 동일한 금액만큼 114800 (KODEX 인버스) 매수
   - 계좌 내 코스닥 보유 주식 총 매입금액과 동일한 금액만큼 251340 (KODEX 코스닥150선물인버스) 매수
5. 예수금 부족 시 비례 배분:
   - 주문가능 예수금이 부족한 경우, 코스피 / 코스닥 필요 금액 비율에 맞춰 예수금 한도 내에서 최대한 배분 매수.
6. 인버스 ETF 자동 안전장치:
   - 매수 체결 즉시 실질 순수익 -3.0% 자동 손절 및 +3.0% 트레일링 익절을 [MONITORING] 상태로 자동 등록!
7. 당일 1회 한정 래치(Latch):
   - 당일 1회 실행 후 락(Lock). 필요 시 /버스터콜 리셋 명령으로 수동 해제 가능.
"""
import os
import json
import time
import logging
from datetime import datetime, time as dtime
from dataclasses import dataclass, asdict

import config
import broker
import storage
import quant_brain
import market_schedule
import fee_calculator

logger = logging.getLogger("AutoStock.BusterCall")

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "buster_call_config.json")

# 타겟 인버스 ETF 코드 정의
KOSDAQ_INVERSE_CODE = "251340"  # KODEX 코스닥150선물인버스
KOSPI_INVERSE_CODE = "114800"   # KODEX 인버스


@dataclass
class BusterCallConfig:
    enabled: bool = False
    last_triggered_date_kospi: str = ""
    last_triggered_date_kosdaq: str = ""
    kosdaq_inverse_code: str = KOSDAQ_INVERSE_CODE
    kospi_inverse_code: str = KOSPI_INVERSE_CODE
    # 인버스 ETF 자동 안전장치 설정
    etf_loss_pct: float = -3.0               # 자동 로스컷 기준 (-3.0%)
    etf_profit_pct: float = 3.0              # 트레일링 익절 시작 기준 (+3.0%)
    etf_trailing_fallback_pct: float = 0.8   # 트레일링 최고가 대비 반락 허용폭 (0.8%)


class BusterCallManager:
    def __init__(self):
        self.config = self._load_config()
        self._last_check_ts: float = 0.0
        self._pre_market_notified_date: str = ""

    def _load_config(self) -> BusterCallConfig:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return BusterCallConfig(
                        enabled=bool(data.get("enabled", False)),
                        last_triggered_date_kospi=str(data.get("last_triggered_date_kospi", "")),
                        last_triggered_date_kosdaq=str(data.get("last_triggered_date_kosdaq", "")),
                        kosdaq_inverse_code=str(data.get("kosdaq_inverse_code", KOSDAQ_INVERSE_CODE)),
                        kospi_inverse_code=str(data.get("kospi_inverse_code", KOSPI_INVERSE_CODE)),
                        etf_loss_pct=float(data.get("etf_loss_pct", -3.0)),
                        etf_profit_pct=float(data.get("etf_profit_pct", 3.0)),
                        etf_trailing_fallback_pct=float(data.get("etf_trailing_fallback_pct", 0.8)),
                    )
            except Exception as e:
                logger.error(f"버스터콜 설정 로드 실패: {e}")
        return BusterCallConfig()

    def save_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(asdict(self.config), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"버스터콜 설정 저장 실패: {e}")

    @property
    def is_enabled(self) -> bool:
        return self.config.enabled

    def set_enabled(self, enabled: bool) -> str:
        self.config.enabled = enabled
        self.save_config()
        if enabled:
            status_str = "🟢 활성화 (ON)"
            ai_note = "\n\n⚠️ <b>버스터콜이 켜졌으므로 신규 주식 AI 스나이퍼 매수는 일시 중단(휴면)</b>됩니다. (헤지 집중)"
        else:
            status_str = "⚪ 비활성화 (OFF)"
            ai_note = "\n\n▶️ <b>버스터콜이 꺼졌으므로 AI 스나이퍼 매수가 다시 정상 가동</b>됩니다."
        msg = f"🚨 <b>[버스터콜 상태 변경]</b>\n\n버스터콜 비상 헤지 기능이 <b>{status_str}</b> 되었습니다.{ai_note}"
        logger.info(f"버스터콜 상태 변경: {status_str}")
        return msg

    def reset_latch(self, target: str = "ALL") -> str:
        """당일 1회 실행 락을 수동 해제합니다."""
        target_upper = target.upper()
        if target_upper in ("KOSPI", "코스피"):
            self.config.last_triggered_date_kospi = ""
            desc = "코스피 락 해제"
        elif target_upper in ("KOSDAQ", "코스닥"):
            self.config.last_triggered_date_kosdaq = ""
            desc = "코스닥 락 해제"
        else:
            self.config.last_triggered_date_kospi = ""
            self.config.last_triggered_date_kosdaq = ""
            desc = "코스피 & 코스닥 전체 락 해제"

        self._pre_market_notified_date = ""
        self.save_config()
        return f"🔄 <b>[버스터콜 래치 리셋]</b>\n\n{desc} 완료! 오늘 시장 조건 만족 시 다시 버스터콜 헤지 매수가 작동할 수 있습니다."

    def get_status_summary(self) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        kp_triggered_today = (self.config.last_triggered_date_kospi == today)
        kd_triggered_today = (self.config.last_triggered_date_kosdaq == today)
        return {
            "enabled": self.config.enabled,
            "threshold_pct": -1.5,
            "kp_triggered_today": kp_triggered_today,
            "kd_triggered_today": kd_triggered_today,
            "etf_loss_pct": self.config.etf_loss_pct,
            "etf_profit_pct": self.config.etf_profit_pct,
            "etf_trailing_fallback_pct": self.config.etf_trailing_fallback_pct,
            "kospi_inverse_code": self.config.kospi_inverse_code,
            "kosdaq_inverse_code": self.config.kosdaq_inverse_code
        }

    def format_status_text(self, mkt=None) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        kp_status = "⚡ [오늘 헤지 완료]" if self.config.last_triggered_date_kospi == today else "🛡️ [감시 대기]"
        kd_status = "⚡ [오늘 헤지 완료]" if self.config.last_triggered_date_kosdaq == today else "🛡️ [감시 대기]"
        sw_status = "🟢 [ON - 자동 헤지 활성화]" if self.config.enabled else "⚪ [OFF - 기능 꺼짐]"

        if mkt is None:
            try:
                import market_brain
                mkt = market_brain.get_market_macro_snapshot()
            except Exception:
                mkt = None

        if mkt:
            kelly_str = f"{mkt.kelly_prob_up:.1f}% ({mkt.rff_regime})"
            kp_gate = "🛑 매수차단" if not mkt.is_kospi_tradeable else "🟢 매수허용"
            kd_gate = "🛑 매수차단" if not mkt.is_kosdaq_tradeable else "🟢 매수허용"
            is_crash = ("비대칭 급락 위험" in mkt.rff_regime) or (mkt.kelly_prob_up <= 32.0)
            crash_badge = "🚨 [비대칭 급락 위험 감지]" if is_crash else "🟢 [안정/중립]"
        else:
            kelly_str = "-"
            kp_gate = "-"
            kd_gate = "-"
            crash_badge = "-"

        return (
            f"🚨 <b>[버스터콜(BUSTER CALL) 비상 헤지 상태]</b>\n\n"
            f"• <b>스위치 상태</b>: <b>{sw_status}</b>\n"
            f"• <b>헤지 원칙</b>: <b>기존 보유 주식 100% 안전 유지</b> + 보유 매입금액 동액 인버스 매수\n"
            f"• <b>발동 조건</b>: <b>오전 장전(08:00~) /시장 진단 시</b>\n"
            f"  - <b>코스피</b>: 매수차단 + Kelly RFF 비대칭 급락 위험 (<=32.0%)\n"
            f"  - <b>코스닥</b>: 매수차단 + Kelly RFF 비대칭 급락 위험 (<=32.0%)\n"
            f"• <b>집행 시점</b>: <b>09:00:00 정규장 개장 즉시 시장가 매수</b>\n"
            f"  - 코스피 보유주식 매입금액 ➡️ <b>{self.config.kospi_inverse_code} (KODEX 인버스)</b> 매수\n"
            f"  - 코스닥 보유주식 매입금액 ➡️ <b>{self.config.kosdaq_inverse_code} (KODEX 코스닥150선물인버스)</b> 매수\n"
            f"• <b>예수금 부족 시</b>: 코스피/코스닥 필요금액 비율대로 한도 내 비례 배분 매수\n"
            f"• <b>🔒 ETF 안전장치</b>: 매수 체결 즉시 <b>자동 손절 {self.config.etf_loss_pct:.1f}% / 트레일링 익절 +{self.config.etf_profit_pct:.1f}%</b> 자동 등록\n\n"
            f"📊 <b>[실시간 시장 진단 및 발동 현황]</b>\n"
            f"• Kelly RFF: <b>{kelly_str}</b> {crash_badge}\n"
            f"• 코스피: <b>{kp_gate}</b> | 실행 상태: {kp_status}\n"
            f"• 코스닥: <b>{kd_gate}</b> | 실행 상태: {kd_status}\n\n"
            f"👉 <b>제어 명령어</b>:\n"
            f"• <code>/버스터콜 켜기</code> : 기능 활성화\n"
            f"• <code>/버스터콜 끄기</code> : 기능 비활성화\n"
            f"• <code>/버스터콜 리셋</code> : 오늘 발동 이력 초기화 (재감시)\n"
            f"• <code>/버스터콜 실행</code> : 현재 조건으로 즉시 헤지 집행 (수동)"
        )

    def check_and_execute(self, engine) -> list[str]:
        """주기적으로 시장 상태와 시각을 모니터링하여 장 개장 시 버스터콜 헤지 매수를 집행합니다."""
        now = time.time()
        # 1초에 한 번만 실행 (부하 방지)
        if now - self._last_check_ts < 1.0:
            return []
        self._last_check_ts = now

        if not self.config.enabled:
            return []

        now_dt = datetime.now()
        # 주말 및 증시 휴장일에는 작동하지 않음
        if market_schedule.is_market_holiday(now_dt.date()):
            return []

        t = now_dt.time()
        today_str = now_dt.strftime("%Y-%m-%d")

        # 1. 시장 거시 지표 스냅샷 조회
        import market_brain
        mkt = market_brain.get_market_macro_snapshot()

        # 2. 발동 조건 평가 (Kelly RFF 비대칭 급락 위험 + 시장별 매수 차단)
        is_crash_risk = ("비대칭 급락 위험" in mkt.rff_regime) or (mkt.kelly_prob_up <= 32.0)
        kp_condition = (not mkt.is_kospi_tradeable) and is_crash_risk
        kd_condition = (not mkt.is_kosdaq_tradeable) and is_crash_risk

        kp_need_exec = kp_condition and (self.config.last_triggered_date_kospi != today_str)
        kd_need_exec = kd_condition and (self.config.last_triggered_date_kosdaq != today_str)

        if not (kp_need_exec or kd_need_exec):
            return []

        # 3. 시간대별 분기
        # A) 08:00 ~ 08:59:59 : 장전 예약 감지 알림 (오전 중 1회 발송)
        if dtime(8, 0) <= t < dtime(9, 0):
            if self._pre_market_notified_date != today_str:
                self._pre_market_notified_date = today_str
                arm_msg = self._format_pre_market_armed_alert(mkt, kp_need_exec, kd_need_exec)
                engine.telegram.send_message(arm_msg)
                engine.add_log("🚨 [버스터콜 장전 예약] 09:00 개장 즉시 인버스 헤지 매수 대기")
            return []

        # B) 09:00:00 ~ 09:15:00 (정규장 개장 즉시 집행)
        if dtime(9, 0) <= t < dtime(9, 15):
            logger.warning(f"🚨 [버스터콜 정규장 개장 집행] KOSPI={kp_need_exec}, KOSDAQ={kd_need_exec}")
            if kp_need_exec:
                self.config.last_triggered_date_kospi = today_str
            if kd_need_exec:
                self.config.last_triggered_date_kosdaq = today_str
            self.save_config()

            evt = self._execute_hedge_inverse(
                engine=engine,
                kp_trigger=kp_need_exec,
                kd_trigger=kd_need_exec,
                mkt=mkt
            )
            return [evt]

        return []

    def manual_execute(self, engine) -> str:
        """텔레그램 /버스터콜 실행 명령으로 수동 즉시 집행."""
        if not self.config.enabled:
            return "⚠️ 버스터콜 기능이 비활성화(OFF) 상태입니다. 먼저 <code>/버스터콜 켜기</code>를 실행해 주세요."

        import market_brain
        mkt = market_brain.get_market_macro_snapshot()
        is_crash_risk = ("비대칭 급락 위험" in mkt.rff_regime) or (mkt.kelly_prob_up <= 32.0)
        kp_condition = (not mkt.is_kospi_tradeable) and is_crash_risk
        kd_condition = (not mkt.is_kosdaq_tradeable) and is_crash_risk

        if not kp_condition and not kd_condition:
            return (
                "ℹ️ <b>[버스터콜 조건 미충족]</b>\n\n"
                f"현재 시장은 버스터콜 발동 조건(매수차단 + Kelly 비대칭 급락위험)에 해당하지 않습니다.\n"
                f"• Kelly RFF: <b>{mkt.kelly_prob_up:.1f}%</b> ({mkt.rff_regime})\n"
                f"• 코스피: {'🛑 매수차단' if not mkt.is_kospi_tradeable else '🟢 매수허용'}\n"
                f"• 코스닥: {'🛑 매수차단' if not mkt.is_kosdaq_tradeable else '🟢 매수허용'}\n\n"
                f"<i>(조건이 충족되면 장 시작 시 또는 실시간으로 자동 실행됩니다.)</i>"
            )

        today_str = datetime.now().strftime("%Y-%m-%d")
        if kp_condition:
            self.config.last_triggered_date_kospi = today_str
        if kd_condition:
            self.config.last_triggered_date_kosdaq = today_str
        self.save_config()

        return self._execute_hedge_inverse(
            engine=engine,
            kp_trigger=kp_condition,
            kd_trigger=kd_condition,
            mkt=mkt
        )

    def _execute_hedge_inverse(
        self,
        engine,
        kp_trigger: bool,
        kd_trigger: bool,
        mkt=None
    ) -> str:
        """기존 보유 주식은 100% 보유하고, 각 시장 매입금액만큼 인버스 ETF를 매수합니다."""
        act_no = engine.act_no
        logger.warning(f"🚨🚨 [버스터콜 헤지 매수 시작] 코스피={kp_trigger}, 코스닥={kd_trigger}")

        # 1. 계좌 잔고 조회
        summary, holdings = broker.get_account_balance(act_no)

        total_kp_cost = 0
        total_kd_cost = 0
        kp_holdings_detail = []
        kd_holdings_detail = []

        # 인버스 ETF 자체는 헤지 대상 집계에서 제외
        protected_codes = {self.config.kospi_inverse_code, self.config.kosdaq_inverse_code, KOSPI_INVERSE_CODE, KOSDAQ_INVERSE_CODE}

        for h in holdings:
            code = h.get("iem_cd")
            if not code or code in protected_codes:
                continue

            qty = int(float(h.get("actual_qty") or h.get("rsdl_qty") or 0))
            if qty <= 0:
                continue
            price = float(h.get("phs_pr") or h.get("now_pr") or 0)
            cost = int(qty * price)
            if cost <= 0:
                continue

            m_type = quant_brain.get_stock_market_type(code, h)
            name = h.get("iem_nm", code)

            if m_type == "KOSPI":
                total_kp_cost += cost
                kp_holdings_detail.append(f"• {name}({code}) {qty:,}주 (매입단가 {price:,.0f}원, 총 {cost:,}원)")
            else:
                total_kd_cost += cost
                kd_holdings_detail.append(f"• {name}({code}) {qty:,}주 (매입단가 {price:,.0f}원, 총 {cost:,}원)")

        # 2. 시장별 목표 헤지 금액 결정
        target_kp_amt = total_kp_cost if kp_trigger else 0
        target_kd_amt = total_kd_cost if kd_trigger else 0
        total_target_amt = target_kp_amt + target_kd_amt

        if total_target_amt <= 0:
            msg = (
                f"ℹ️ <b>[버스터콜 헤지 생략]</b>\n\n"
                f"조건은 충족되었으나 계좌 내 해당 시장(코스피:{total_kp_cost:,}원, 코스닥:{total_kd_cost:,}원)에 "
                f"보유 중인 주식이 없어 인버스 매수를 생략합니다."
            )
            logger.info(msg)
            engine.add_log("ℹ️ [버스터콜 생략] 보유 현물 주식 없음")
            engine.telegram.send_message(msg)
            return msg

        # 3. 주문가능 예수금 조회 및 비례 배분 연산
        available_cash = int(float(summary.get("stk_orr_pbl_amt") or summary.get("drn_pbl_amt") or summary.get("dca") or 0))

        if available_cash <= 0:
            msg = (
                f"🚨 <b>[버스터콜 헤지 집행 실패: 예수금 0원]</b>\n\n"
                f"• 필요 헤지 금액: 코스피 <b>{target_kp_amt:,}원</b> / 코스닥 <b>{target_kd_amt:,}원</b>\n"
                f"• 현재 주문가능 예수금: <b>0원</b>\n\n"
                f"⚠️ 계좌 내 주문가능 예수금이 전혀 없어 인버스 매수를 집행하지 못했습니다."
            )
            logger.warning(msg)
            engine.add_log("🚨 [버스터콜 실패] 주문가능 예수금 0원")
            engine.telegram.send_message(msg)
            return msg

        if available_cash >= total_target_amt:
            alloc_kp = target_kp_amt
            alloc_kd = target_kd_amt
            budget_desc = f"🟢 예수금 충분 (주문가능 {available_cash:,}원 >= 필요 {total_target_amt:,}원)"
        else:
            ratio_kp = target_kp_amt / total_target_amt if total_target_amt > 0 else 0.0
            alloc_kp = int(available_cash * ratio_kp)
            alloc_kd = available_cash - alloc_kp
            budget_desc = (
                f"⚠️ 예수금 부족 비례 배분\n"
                f"  - 주문가능 예수금: <b>{available_cash:,}원</b> (필요 {total_target_amt:,}원 대비 부족)\n"
                f"  - 코스피 배분({ratio_kp*100:.1f}%): <b>{alloc_kp:,}원</b>\n"
                f"  - 코스닥 배분({(1-ratio_kp)*100:.1f}%): <b>{alloc_kd:,}원</b>"
            )

        # 4. 인버스 ETF 현재가 조회 및 매수 수량 산출
        qty_kp = 0
        price_kp = 0
        actual_kp_amt = 0
        res_kp = None

        if alloc_kp > 0:
            time.sleep(0.2)
            q_kp = broker.get_current_price(self.config.kospi_inverse_code, market_cd="UNT")
            price_kp = int((q_kp or {}).get("stck_prpr") or (q_kp or {}).get("main_cls_prpr") or 0)
            if price_kp <= 0:
                price_kp = 1150  # 폴백
            qty_kp = int(alloc_kp // price_kp)

        qty_kd = 0
        price_kd = 0
        actual_kd_amt = 0
        res_kd = None

        if alloc_kd > 0:
            time.sleep(0.2)
            q_kd = broker.get_current_price(self.config.kosdaq_inverse_code, market_cd="UNT")
            price_kd = int((q_kd or {}).get("stck_prpr") or (q_kd or {}).get("main_cls_prpr") or 0)
            if price_kd <= 0:
                price_kd = 2660  # 폴백
            qty_kd = int(alloc_kd // price_kd)

        # 5. 시장가(05) 매수 주문 집행 및 ETF 안전장치 자동 등록
        stop_kp, profit_kp = 0, 0
        if qty_kp > 0:
            res_kp = broker.execute_buy_order(
                act_no=act_no,
                iem_cd=self.config.kospi_inverse_code,
                qty=qty_kp,
                order_type="05",
                rmt_mkt_cd="KRX",
                dry_run=config.DRY_RUN
            )
            actual_kp_amt = qty_kp * price_kp
            stop_kp, profit_kp = self._register_etf_protection(
                engine=engine,
                code=self.config.kospi_inverse_code,
                name="KODEX 인버스",
                qty=qty_kp,
                price=price_kp
            )

        stop_kd, profit_kd = 0, 0
        if qty_kd > 0:
            res_kd = broker.execute_buy_order(
                act_no=act_no,
                iem_cd=self.config.kosdaq_inverse_code,
                qty=qty_kd,
                order_type="05",
                rmt_mkt_cd="KRX",
                dry_run=config.DRY_RUN
            )
            actual_kd_amt = qty_kd * price_kd
            stop_kd, profit_kd = self._register_etf_protection(
                engine=engine,
                code=self.config.kosdaq_inverse_code,
                name="KODEX 코스닥150선물인버스",
                qty=qty_kd,
                price=price_kd
            )

        # 6. 결과 메시지 포맷팅 및 알림 전송
        report = self._format_hedge_execution_alert(
            kp_trigger=kp_trigger,
            kd_trigger=kd_trigger,
            total_kp_cost=total_kp_cost,
            total_kd_cost=total_kd_cost,
            target_kp_amt=target_kp_amt,
            target_kd_amt=target_kd_amt,
            actual_kp_amt=actual_kp_amt,
            actual_kd_amt=actual_kd_amt,
            qty_kp=qty_kp,
            qty_kd=qty_kd,
            price_kp=price_kp,
            price_kd=price_kd,
            stop_kp=stop_kp,
            profit_kp=profit_kp,
            stop_kd=stop_kd,
            profit_kd=profit_kd,
            kp_holdings_detail=kp_holdings_detail,
            kd_holdings_detail=kd_holdings_detail,
            budget_desc=budget_desc,
            mkt=mkt
        )

        engine.add_log(f"🚨 [버스터콜 헤지 완료] 코스피 {qty_kp:,}주({actual_kp_amt:,}원), 코스닥 {qty_kd:,}주({actual_kd_amt:,}원) 매수 집행")
        engine.telegram.send_message(report)
        return report

    def _register_etf_protection(
        self,
        engine,
        code: str,
        name: str,
        qty: int,
        price: int
    ) -> tuple[int, int]:
        """매수된 인버스 ETF에 자동 -3.0% 로스컷 및 +3.0% 이상 트레일링 익절을 등록합니다."""
        etf_loss = self.config.etf_loss_pct
        etf_profit = self.config.etf_profit_pct
        etf_fallback = self.config.etf_trailing_fallback_pct

        stop_pr = fee_calculator.calculate_target_price_for_net_pnl(price, etf_loss)
        profit_pr = fee_calculator.calculate_target_price_for_net_pnl(price, etf_profit)

        # 1. 영구 저장소에 등록
        storage.update_stock_target(
            iem_cd=code,
            iem_nm=name,
            buy_price=float(price),
            stop_loss_price=stop_pr,
            profit_cut_price=profit_pr,
            target_type="PERCENT",
            loss_pct=etf_loss,
            profit_pct=etf_profit
        )

        # 2. engine.tracked_items 에 MONITORING 상태로 등록
        from engine import WatchItem
        existing = engine.tracked_items.get(code)
        if existing:
            total_q = existing.hold_qty + qty
            comb_pr = ((existing.hold_qty * existing.buy_price) + (qty * price)) / max(total_q, 1)
            existing.hold_qty = total_q
            existing.sellable_qty = total_q
            existing.buy_price = comb_pr
            existing.effective_buy_price = fee_calculator.get_effective_buy_price(comb_pr)
            existing.stop_loss_price = fee_calculator.calculate_target_price_for_net_pnl(comb_pr, etf_loss)
            existing.profit_cut_price = fee_calculator.calculate_target_price_for_net_pnl(comb_pr, etf_profit)
            existing.target_type = "PERCENT"
            existing.loss_pct = etf_loss
            existing.profit_pct = etf_profit
            existing.status = "MONITORING"
            existing.use_trailing = True
            existing.trailing_active = False
            existing.trailing_fallback_pct = etf_fallback
            stop_pr = existing.stop_loss_price
            profit_pr = existing.profit_cut_price
        else:
            engine.tracked_items[code] = WatchItem(
                iem_cd=code,
                iem_nm=name,
                hold_qty=qty,
                sellable_qty=qty,
                buy_price=float(price),
                effective_buy_price=fee_calculator.get_effective_buy_price(price),
                stop_loss_price=stop_pr,
                profit_cut_price=profit_pr,
                target_type="PERCENT",
                loss_pct=etf_loss,
                profit_pct=etf_profit,
                current_price=price,
                status="MONITORING",
                use_trailing=True,
                trailing_active=False,
                trailing_fallback_pct=etf_fallback
            )

        logger.info(
            f"🔒 [인버스 ETF 보호 등록] {name}({code}) {qty}주 평단가 {price:,}원 -> "
            f"로스컷 {stop_pr:,}원({etf_loss:.1f}%), 트레일링 시작 {profit_pr:,}원(+{etf_profit:.1f}%)"
        )
        return stop_pr, profit_pr

    def _format_pre_market_armed_alert(self, mkt, kp_need: bool, kd_need: bool) -> str:
        targets = []
        if kp_need:
            targets.append("코스피 (114800 KODEX 인버스)")
        if kd_need:
            targets.append("코스닥 (251340 KODEX 코스닥150선물인버스)")

        target_str = ", ".join(targets)
        return (
            f"🚨 <b>[버스터콜 비상 헤지 장전 예약 감지]</b>\n\n"
            f"오전 장전 시장 진단 결과, <b>급락 위험 조건</b>이 포착되었습니다!\n"
            f"• <b>Kelly RFF 상방확률</b>: <b>{mkt.kelly_prob_up:.1f}%</b> ({mkt.rff_regime})\n"
            f"• <b>코스피 게이트</b>: {'🛑 매수차단' if not mkt.is_kospi_tradeable else '🟢 매수허용'}\n"
            f"• <b>코스닥 게이트</b>: {'🛑 매수차단' if not mkt.is_kosdaq_tradeable else '🟢 매수허용'}\n\n"
            f"🛡️ <b>[09:00:00 개장 즉시 집행 예정]</b>\n"
            f"• <b>헤지 대상</b>: <b>{target_str}</b>\n"
            f"• <b>원칙</b>: 기존 보유 주식은 100% 유지하며, 매입금액과 동일하게 인버스 ETF 시장가 매수 집행\n"
            f"<i>(취소를 원하시면 개장 전 <code>/버스터콜 끄기</code>를 입력하세요.)</i>"
        )

    def _format_hedge_execution_alert(
        self,
        kp_trigger: bool,
        kd_trigger: bool,
        total_kp_cost: int,
        total_kd_cost: int,
        target_kp_amt: int,
        target_kd_amt: int,
        actual_kp_amt: int,
        actual_kd_amt: int,
        qty_kp: int,
        qty_kd: int,
        price_kp: int,
        price_kd: int,
        stop_kp: int,
        profit_kp: int,
        stop_kd: int,
        profit_kd: int,
        kp_holdings_detail: list[str],
        kd_holdings_detail: list[str],
        budget_desc: str,
        mkt=None
    ) -> str:
        lines = [
            f"🚨 <b>[버스터콜 비상 헤지: 인버스 매수 완료]</b>\n",
            f"🛡️ <b>핵심 원칙: 기존 보유 현물 100% 안전 유지 + 동액 인버스 헤지 포지션 구축</b>\n"
        ]

        if mkt:
            lines.append(f"• <b>시장 상태</b>: Kelly RFF <b>{mkt.kelly_prob_up:.1f}%</b> ({mkt.rff_regime})")

        lines.append(f"• <b>예수금 상태</b>: {budget_desc}\n")

        if kp_trigger:
            lines.append(f"🏛️ <b>[코스피 인버스 헤지]</b>")
            lines.append(f"• <b>보호 현물 총 매입액</b>: <b>{total_kp_cost:,}원</b> ({len(kp_holdings_detail)}개 종목 보유 유지)")
            lines.append(f"• <b>매수 인버스</b>: <b>{self.config.kospi_inverse_code} (KODEX 인버스)</b>")
            lines.append(f"• <b>체결 수량</b>: <b>{qty_kp:,}주</b> (@ {price_kp:,}원, 실투입 {actual_kp_amt:,}원 / 목표 {target_kp_amt:,}원)")
            lines.append(f"• <b>🔒 ETF 안전장치</b>: 손절 {stop_kp:,}원(-3%) / 익절 트레일링 {profit_kp:,}원(+3%)\n")

        if kd_trigger:
            lines.append(f"🚀 <b>[코스닥 인버스 헤지]</b>")
            lines.append(f"• <b>보호 현물 총 매입액</b>: <b>{total_kd_cost:,}원</b> ({len(kd_holdings_detail)}개 종목 보유 유지)")
            lines.append(f"• <b>매수 인버스</b>: <b>{self.config.kosdaq_inverse_code} (KODEX 코스닥150선물인버스)</b>")
            lines.append(f"• <b>체결 수량</b>: <b>{qty_kd:,}주</b> (@ {price_kd:,}원, 실투입 {actual_kd_amt:,}원 / 목표 {target_kd_amt:,}원)")
            lines.append(f"• <b>🔒 ETF 안전장치</b>: 손절 {stop_kd:,}원(-3%) / 익절 트레일링 {profit_kd:,}원(+3%)\n")

        lines.append("💡 <i>기존에 보유 중인 주식은 한 주도 매도되지 않고 정상 유지 중입니다.</i>")
        return "\n".join(lines)


# 전역 싱글톤 인스턴스
buster_call_manager = BusterCallManager()
