# -*- coding: utf-8 -*-
"""장중 미시구조(Microstructure) & 파생 급변속도 서킷브레이커 정밀 검증 스크립트 (test_micro_circuit_breaker.py)"""
import sys
import time
import unittest
from collections import deque

sys.stdout.reconfigure(encoding='utf-8')

import market_brain
from market_brain import DerivativeVelocityTracker, DerivativeVelocitySample
import quant_brain
from quant_brain import ContStoikovOFIAnalyzer, ContStoikovMetrics
from nhplug import call


class TestRealAPIAndMicroCircuitBreaker(unittest.TestCase):

    def test_01_real_nh_api_live_quotes(self):
        """실제 NH Open API 호출을 통해 실시간 데이터가 정상 수신되는지 엄격히 검증 (가짜 데이터 없음)"""
        print("\n[검증 1] NH투자증권 실시간 API 실데이터 수신 검증")

        nq_res = call("/gbfuture/quote/v1/current", {"exnm": "FCME", "iem_cd": "NQZ26"})
        o0_nq = nq_res.get("Output_0") or {}
        nq_rate = float(o0_nq.get("rate") or 0.0)
        nq_last = float(o0_nq.get("last") or 0.0)
        print(f"  • CME 나스닥 100 (NQZ26): 현재가 {nq_last:,.2f}pt, 등락률 {nq_rate:+.2f}%")
        self.assertGreater(nq_last, 10000.0, "CME 나스닥 선물 현재가가 정상 범위여야 함")

        now_dt = time.localtime()
        is_night = (now_dt.tm_hour >= 18 or now_dt.tm_hour < 8)
        fut_ep = "/krfuture/quote/v1/night" if is_night else "/krfuture/quote/v1/day"
        kp_res = call(fut_ep, {"iem_cd": "KA016C000"})
        o0_kp = kp_res.get("Output_0") or {}
        kp_prpr = float(o0_kp.get("prpr") or 0.0)
        print(f"  • 코스피 200 선물 ({fut_ep}): 현재가 {kp_prpr:,.2f}pt")
        self.assertGreater(kp_prpr, 100.0, "코스피 200 선물 현재가가 정상 범위여야 함")

        kd_res = call(fut_ep, {"iem_cd": "KA066C000"})
        o0_kd = kd_res.get("Output_0") or {}
        kd_prpr = float(o0_kd.get("prpr") or 0.0)
        print(f"  • 코스닥 150 선물 ({fut_ep}): 현재가 {kd_prpr:,.2f}pt")
        self.assertGreater(kd_prpr, 100.0, "코스닥 150 선물 현재가가 정상 범위여야 함")

        stk_res = call("/krstock/quote/v1/currentPrice", {"iem_cd": "005930", "market_cd": "UNT"})
        o0_stk = stk_res.get("Output_0") or {}
        stk_prpr = int(float(o0_stk.get("stck_prpr") or 0))
        bid1_q = int(float(o0_stk.get("bidp_rsqn1") or 0))
        ask1_q = int(float(o0_stk.get("askp_rsqn1") or 0))
        print(f"  • 삼성전자 (005930): 현재가 {stk_prpr:,}원 | 매수1잔량 {bid1_q:,}주, 매도1잔량 {ask1_q:,}주")
        self.assertGreater(stk_prpr, 10000, "삼성전자 현재가가 정상 수신되어야 함")
        self.assertGreater(bid1_q + ask1_q, 0, "1호가 잔량이 존재해야 함")

    def test_02_derivative_velocity_decoupling_logic(self):
        """코스피 vs 코스닥 선물 베이시스 디커플링 플래시 프리즈 검증"""
        print("\n[검증 2] 코스피 vs 코스닥 디커플링 플래시 프리즈 검증")

        tracker = DerivativeVelocityTracker(history_len=100, freeze_duration=300.0)
        base_t = 1000.0

        for i in range(10):
            t = base_t + i * 4.0
            tracker.record_sample(t, nq_rate=0.10, k200_basis=0.50, kd150_basis=0.20)

        # ── [케이스 A: 코스피 200 베이시스만 급락 (0.50pt -> -2.02pt)] ──
        for i in range(1, 15):
            t = base_t + 40.0 + i * 4.0
            tracker.record_sample(t, nq_rate=0.10, k200_basis=0.50 - (i * 0.18), kd150_basis=0.20)

        kp_st, kd_st = tracker.check_flash_freeze(base_t + 100.0)
        print(f"  • 코스피200 베이시스 급락 시: 코스피동결={kp_st['is_frozen']} ('{kp_st['reason']}'), 코스닥동결={kd_st['is_frozen']}")
        self.assertTrue(kp_st["is_frozen"], "코스피200 베이시스 급락 시 코스피는 반드시 동결되어야 함")
        self.assertFalse(kd_st["is_frozen"], "코스피200 베이시스 급락 시 코스닥은 절대 동결되지 않아야 함 (디커플링 보장)")

        # ── [케이스 B: 코스닥 150 베이시스만 급락] ──
        tracker2 = DerivativeVelocityTracker(history_len=100, freeze_duration=300.0)
        for i in range(10):
            tracker2.record_sample(base_t + i * 4.0, nq_rate=0.10, k200_basis=0.50, kd150_basis=0.20)

        for i in range(1, 15):
            t = base_t + 40.0 + i * 4.0
            tracker2.record_sample(t, nq_rate=0.10, k200_basis=0.50, kd150_basis=0.20 - (i * 0.30))

        kp_st2, kd_st2 = tracker2.check_flash_freeze(base_t + 100.0)
        print(f"  • 코스닥150 베이시스 급락 시: 코스피동결={kp_st2['is_frozen']}, 코스닥동결={kd_st2['is_frozen']} ('{kd_st2['reason']}')")
        self.assertFalse(kp_st2["is_frozen"], "코스닥150 베이시스 급락 시 코스피는 동결되지 않아야 함")
        self.assertTrue(kd_st2["is_frozen"], "코스닥150 베이시스 급락 시 코스닥은 반드시 동결되어야 함")

        # ── [케이스 C: 글로벌 나스닥 선물 급락 시 (양 시장 동시 동결)] ──
        tracker3 = DerivativeVelocityTracker(history_len=100, freeze_duration=300.0)
        for i in range(10):
            tracker3.record_sample(base_t + i * 4.0, nq_rate=0.10, k200_basis=0.50, kd150_basis=0.20)

        for i in range(1, 15):
            t = base_t + 40.0 + i * 4.0
            tracker3.record_sample(t, nq_rate=0.10 - (i * 0.04), k200_basis=0.50, kd150_basis=0.20)

        kp_st3, kd_st3 = tracker3.check_flash_freeze(base_t + 100.0)
        print(f"  • 나스닥 100 선물 급락 시: 코스피동결={kp_st3['is_frozen']}, 코스닥동결={kd_st3['is_frozen']}, 사유='{tracker3.global_freeze_reason}'")
        self.assertTrue(kp_st3["is_frozen"], "나스닥 급락 시 코스피 동결")
        self.assertTrue(kd_st3["is_frozen"], "나스닥 급락 시 코스닥 동결")
        self.assertEqual(tracker3.last_transition_global, "FROZEN", "글로벌 동결 플래그 설정 확인")

    def test_03_cont_stoikov_ofi_and_bid_pulling(self):
        """Cont, Kukanov, Stoikov (2014) 주문흐름 불균형(OFI) 및 허매수벽 급취소 감지 검증"""
        print("\n[검증 3] Cont-Stoikov OFI 및 독성 매도/허매수 취소 포착 검증")

        test_code = "TEST_STOCK"
        ContStoikovOFIAnalyzer._state.pop(test_code, None)

        q1 = {
            "bidp1": 50000, "bidp_rsqn1": 1000,
            "bidp2": 49950, "bidp_rsqn2": 2000,
            "bidp3": 49900, "bidp_rsqn3": 3000,
            "bidp4": 49850, "bidp_rsqn4": 2000,
            "bidp5": 49800, "bidp_rsqn5": 1000,
            "askp1": 50050, "askp_rsqn1": 1000,
            "askp2": 50100, "askp_rsqn2": 2000,
            "askp3": 50150, "askp_rsqn3": 3000,
            "askp4": 50200, "askp_rsqn4": 2000,
            "askp5": 50250, "askp_rsqn5": 1000,
        }
        m1 = ContStoikovOFIAnalyzer.update(test_code, q1, 50000)
        self.assertFalse(m1.is_toxic_sell_flow)

        q2 = dict(q1)
        q2["bidp_rsqn1"] = 1500
        m2 = ContStoikovOFIAnalyzer.update(test_code, q2, 50000)
        self.assertGreater(m2.ofi_instant, 0)

        for i in range(4):
            q_dump = dict(q1)
            q_dump["bidp_rsqn1"] = max(50, 500 - (i * 150))
            q_dump["bidp_rsqn2"] = max(100, 1000 - (i * 300))
            q_dump["askp_rsqn1"] = 2000 + (i * 500)
            m_dump = ContStoikovOFIAnalyzer.update(test_code, q_dump, 50000)

        self.assertTrue(m_dump.is_toxic_sell_flow)
        self.assertIn("OFI", m_dump.risk_reason)

    def test_04_decoupled_stock_gating(self):
        """코스피 플래시 프리즈 시 코스피 종목은 WAIT, 코스닥 종목은 독립 판정되는지 검증"""
        print("\n[검증 4] 코스피 vs 코스닥 종목별 독립 게이팅 검증")

        market_brain.velocity_tracker.is_kp_frozen = True
        market_brain.velocity_tracker.kp_freeze_until = time.time() + 300.0
        market_brain.velocity_tracker.kp_freeze_reason = "코스피200 베이시스 급락"

        market_brain.velocity_tracker.is_kd_frozen = False
        market_brain.velocity_tracker.kd_freeze_until = 0.0
        market_brain.velocity_tracker.kd_freeze_reason = ""

        is_kp_frz, kp_rsn, _ = market_brain.is_flash_frozen("KOSPI")
        is_kd_frz, kd_rsn, _ = market_brain.is_flash_frozen("KOSDAQ")

        print(f"  • 코스피 프리즈 조회: {is_kp_frz} ('{kp_rsn}')")
        print(f"  • 코스닥 프리즈 조회: {is_kd_frz} ('{kd_rsn}')")

        self.assertTrue(is_kp_frz, "코스피는 동결이어야 함")
        self.assertFalse(is_kd_frz, "코스닥은 동결이 아니어야 함")

        decision_kp = quant_brain.evaluate_dynamic_buy_signal("005930", remaining_qty=10)
        print(f"  • 코스피 종목(005930) 판정: Action={decision_kp.action}, Reason='{decision_kp.reason}'")
        self.assertEqual(decision_kp.action, "WAIT")
        self.assertTrue(decision_kp.is_flash_frozen)

        market_brain.velocity_tracker.is_kp_frozen = False
        market_brain.velocity_tracker.kp_freeze_until = 0.0
        market_brain.velocity_tracker.kp_freeze_reason = ""


if __name__ == "__main__":
    unittest.main()
