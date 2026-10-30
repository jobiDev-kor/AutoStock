"""버스터콜(Buster Call) 비상 헤지 및 인버스 매수 종합 검증 테스트 스크립트 (test_buster_call.py)
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, time as dtime

import config
from buster_call import BusterCallManager, KOSDAQ_INVERSE_CODE, KOSPI_INVERSE_CODE
from telegram_bot import TelegramNotifier

class TestBusterCall(unittest.TestCase):
    def setUp(self):
        self.mgr = BusterCallManager()
        self.mgr.config.enabled = True
        self.mgr.config.last_triggered_date_kospi = ""
        self.mgr.config.last_triggered_date_kosdaq = ""
        self.mgr._pre_market_notified_date = ""
        self.mgr.save_config()

    def test_01_toggle_and_reset(self):
        """1. ON/OFF 토글 및 당일 락 리셋 검증."""
        self.mgr.set_enabled(False)
        self.assertFalse(self.mgr.is_enabled)
        self.mgr.set_enabled(True)
        self.assertTrue(self.mgr.is_enabled)

        today = datetime.now().strftime("%Y-%m-%d")
        self.mgr.config.last_triggered_date_kosdaq = today
        self.mgr.config.last_triggered_date_kospi = today
        self.mgr.save_config()

        self.mgr.reset_latch("ALL")
        self.assertEqual(self.mgr.config.last_triggered_date_kosdaq, "")
        self.assertEqual(self.mgr.config.last_triggered_date_kospi, "")

    def test_02_independent_market_hedging_and_holdings_retention(self):
        """2. 코스닥만 조건 만족 시: 코스닥 인버스만 매수 + 기존 현물 100% 유지(매도 안 함)."""
        engine = MagicMock()
        engine.act_no = "12345678"
        engine.tracked_items = {}
        engine.history_log = []

        mock_holdings = [
            # 코스닥 종목: 매입가 50,000원 (10주 * 5,000원)
            {"iem_cd": "068760", "iem_nm": "셀트리온제약", "actual_qty": 10, "phs_pr": 5000, "now_pr": 5000},
            # 코스피 종목: 매입가 70,000원 (1주 * 70,000원)
            {"iem_cd": "005930", "iem_nm": "삼성전자", "actual_qty": 1, "phs_pr": 70000, "now_pr": 70000},
        ]
        mock_summary = {"stk_orr_pbl_amt": 200000}  # 예수금 충분

        with patch("market_schedule.is_market_holiday", return_value=False), \
             patch("broker.get_account_balance", return_value=(mock_summary, mock_holdings)), \
             patch("broker.execute_sell_order") as mock_sell_order, \
             patch("broker.get_current_price") as mock_curr_price, \
             patch("broker.execute_buy_order") as mock_buy_order, \
             patch("quant_brain.get_stock_market_type") as mock_market_type, \
             patch("storage.update_stock_target"):

            def m_type_side_effect(c, h=None):
                return "KOSDAQ" if c == "068760" else "KOSPI"
            mock_market_type.side_effect = m_type_side_effect

            # 코스닥 인버스 현재가 2,500원
            mock_curr_price.return_value = {"stck_prpr": "2500"}
            mock_buy_order.return_value = {"success": True, "dry_run": True}

            # 코스닥만 발동 (kp_trigger=False, kd_trigger=True)
            report = self.mgr._execute_hedge_inverse(
                engine=engine,
                kp_trigger=False,
                kd_trigger=True
            )

            # 1. 기존 주식 매도 주문은 일절 나가지 않아야 함!
            mock_sell_order.assert_not_called()

            # 2. 인버스 매수는 코스닥(251340)만 나가야 함! (50,000원 // 2,500원 = 20주)
            mock_buy_order.assert_called_once()
            buy_args = mock_buy_order.call_args[1]
            self.assertEqual(buy_args["iem_cd"], KOSDAQ_INVERSE_CODE)
            self.assertEqual(buy_args["qty"], 20)

            # 3. 매수된 인버스에 자동 -3.0% 손절 / +3.0% 트레일링 익절 등록 확인
            self.assertIn(KOSDAQ_INVERSE_CODE, engine.tracked_items)
            inv_watch = engine.tracked_items[KOSDAQ_INVERSE_CODE]
            self.assertEqual(inv_watch.status, "MONITORING")
            self.assertEqual(inv_watch.loss_pct, -3.0)
            self.assertEqual(inv_watch.profit_pct, 3.0)
            self.assertTrue(inv_watch.use_trailing)

    def test_03_proportional_allocation_on_low_cash(self):
        """3. 예수금 부족 시 코스피/코스닥 목표 비율대로 비례 배분 검증."""
        engine = MagicMock()
        engine.act_no = "12345678"
        engine.tracked_items = {}

        mock_holdings = [
            # 코스피 60,000원 (60%)
            {"iem_cd": "005930", "iem_nm": "삼성전자", "actual_qty": 1, "phs_pr": 60000, "now_pr": 60000},
            # 코스닥 40,000원 (40%)
            {"iem_cd": "068760", "iem_nm": "셀트리온제약", "actual_qty": 1, "phs_pr": 40000, "now_pr": 40000},
        ]
        # 필요 총액 100,000원 대비 주문가능 예수금 50,000원만 존재 (50% 부족)
        mock_summary = {"stk_orr_pbl_amt": 50000}

        with patch("market_schedule.is_market_holiday", return_value=False), \
             patch("broker.get_account_balance", return_value=(mock_summary, mock_holdings)), \
             patch("broker.execute_sell_order") as mock_sell_order, \
             patch("broker.get_current_price") as mock_curr_price, \
             patch("broker.execute_buy_order") as mock_buy_order, \
             patch("quant_brain.get_stock_market_type") as mock_market_type, \
             patch("storage.update_stock_target"):

            mock_market_type.side_effect = lambda c, h=None: "KOSPI" if c == "005930" else "KOSDAQ"
            # 114800: 1,000원, 251340: 2,000원
            def price_side_effect(c, market_cd="UNT"):
                if c == KOSPI_INVERSE_CODE:
                    return {"stck_prpr": "1000"}
                return {"stck_prpr": "2000"}
            mock_curr_price.side_effect = price_side_effect
            mock_buy_order.return_value = {"success": True, "dry_run": True}

            # 코스피 & 코스닥 둘 다 발동
            report = self.mgr._execute_hedge_inverse(
                engine=engine,
                kp_trigger=True,
                kd_trigger=True
            )

            # 비례 배분:
            # 총 예수금 50,000원 중 코스피(60%) = 30,000원 ➡️ 30주 매수
            # 코스닥(40%) = 20,000원 ➡️ 10주 매수
            self.assertEqual(mock_buy_order.call_count, 2)
            calls = mock_buy_order.call_args_list
            kp_call = next(c for c in calls if c[1]["iem_cd"] == KOSPI_INVERSE_CODE)
            kd_call = next(c for c in calls if c[1]["iem_cd"] == KOSDAQ_INVERSE_CODE)

            self.assertEqual(kp_call[1]["qty"], 30)
            self.assertEqual(kd_call[1]["qty"], 10)

    def test_04_telegram_commands(self):
        """4. 텔레그램 명령 제어 검증."""
        notifier = TelegramNotifier()
        engine = MagicMock()
        engine.buster_call = self.mgr

        # 1. 켜기
        rep = notifier._process_command("/버스터콜 켜기", engine)
        self.assertIn("활성화 (ON)", rep)
        self.assertTrue(self.mgr.is_enabled)

        # 2. 끄기
        rep = notifier._process_command("/버스터콜 끄기", engine)
        self.assertIn("비활성화 (OFF)", rep)
        self.assertFalse(self.mgr.is_enabled)

        # 3. 리셋
        rep = notifier._process_command("/버스터콜 리셋", engine)
        self.assertIn("완료", rep)

        # 4. 상태 조회
        rep = notifier._process_command("/버스터콜", engine)
        self.assertIn("버스터콜(BUSTER CALL) 비상 헤지 상태", rep)
        self.assertIn("114800", rep)
        self.assertIn("251340", rep)
        self.assertIn("보유 주식 100% 안전 유지", rep)

if __name__ == "__main__":
    unittest.main()
