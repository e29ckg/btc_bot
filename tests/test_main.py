import unittest
import base64
from types import SimpleNamespace
from unittest.mock import patch

import main
from starlette.requests import Request
from starlette.responses import JSONResponse


class TradingHelpersTest(unittest.TestCase):
    def test_structural_stop_is_beyond_closed_candle_and_cost_buffer(self):
        original = main.CONFIG.copy()
        try:
            main.CONFIG["slBufferPoints"] = 25.0
            tick = SimpleNamespace(bid=4360.000, ask=4360.200)
            symbol = SimpleNamespace(point=0.001, digits=3)
            self.assertEqual(main.calculate_signal_stop("BUY", 4358.500, 4361.000, tick, symbol), 4358.275)
            self.assertEqual(main.calculate_signal_stop("SELL", 4358.500, 4361.000, tick, symbol), 4361.225)
        finally:
            main.CONFIG.clear()
            main.CONFIG.update(original)

    @patch.object(main.mt5, "order_send")
    @patch.object(main.mt5, "account_info", return_value=SimpleNamespace(balance=4660.0))
    @patch.object(main, "send_telegram", return_value=True)
    @patch.object(main, "save_runtime_state")
    @patch.object(main, "log_paper_trade")
    def test_execute_trade_opens_paper_order_without_broker_send(
        self, _log, _save, _telegram, _account, order_send
    ):
        original_state = main.paper_state.copy()
        original_mode = main.EXECUTION_MODE
        try:
            main.EXECUTION_MODE = "paper"
            main.paper_state.update(
                position=None,
                realized_pl=0.0,
                floating_pl=0.0,
                consecutive_losses=0,
                week_start_balance=None,
            )
            opened = main.execute_trade(
                "BTCUSDc", main.mt5.ORDER_TYPE_BUY, 0.01,
                80000.0, 79500.0, 0.0, "Automated paper order test",
            )
            self.assertTrue(opened)
            self.assertEqual(main.paper_state["position"]["symbol"], "BTCUSDc")
            self.assertEqual(main.paper_state["position"]["type"], "BUY")
            self.assertEqual(main.paper_state["position"]["volume"], 0.01)
            order_send.assert_not_called()
        finally:
            main.EXECUTION_MODE = original_mode
            main.paper_state.clear()
            main.paper_state.update(original_state)

    @patch.object(main.mt5, "symbol_info")
    def test_paper_profit_uses_tick_value_for_buy_and_sell(self, symbol_info):
        symbol_info.return_value = SimpleNamespace(trade_tick_size=0.01, trade_tick_value=1.0)
        base = {"symbol": "XAUUSDm", "entry": 2500.0, "volume": 0.1}
        self.assertAlmostEqual(main.paper_profit({**base, "type": "BUY"}, 2501.0), 10.0)
        self.assertAlmostEqual(main.paper_profit({**base, "type": "SELL"}, 2499.0), 10.0)

    @patch.object(main, "send_telegram", return_value=True)
    @patch.object(main, "save_runtime_state")
    @patch.object(main, "log_paper_trade")
    @patch.object(main, "paper_profit", return_value=-12.5)
    def test_close_paper_position_tracks_loss_streak(self, _profit, _log, _save, _telegram):
        original = main.paper_state.copy()
        try:
            main.paper_state.update(position={"ticket": "PAPER-1", "type": "BUY", "symbol": "XAUUSDm"},
                                    realized_pl=0.0, floating_pl=3.0, trades=0, wins=0, losses=0,
                                    consecutive_losses=0)
            self.assertTrue(main.close_paper_position(2499.0, "test"))
            self.assertEqual(main.paper_state["realized_pl"], -12.5)
            self.assertEqual(main.paper_state["losses"], 1)
            self.assertEqual(main.paper_state["consecutive_losses"], 1)
            self.assertIsNone(main.paper_state["position"])
        finally:
            main.paper_state.clear()
            main.paper_state.update(original)

    def test_filling_mode_translates_symbol_flags(self):
        self.assertEqual(
            main.get_order_filling_mode(SimpleNamespace(filling_mode=1)),
            main.mt5.ORDER_FILLING_FOK,
        )
        self.assertEqual(
            main.get_order_filling_mode(SimpleNamespace(filling_mode=2)),
            main.mt5.ORDER_FILLING_IOC,
        )
        self.assertEqual(
            main.get_order_filling_mode(SimpleNamespace(filling_mode=3)),
            main.mt5.ORDER_FILLING_IOC,
        )
        self.assertEqual(
            main.get_order_filling_mode(SimpleNamespace(filling_mode=0)),
            main.mt5.ORDER_FILLING_RETURN,
        )

    def test_required_rate_count_covers_longest_indicator(self):
        original = main.CONFIG.copy()
        try:
            main.CONFIG.update(
                emaFastLen=50,
                emaSlowLen=600,
                emaEntryLen=8,
                adxLen=14,
                atrLen=14,
            )
            self.assertEqual(main.required_rate_count(), 610)

            main.CONFIG.update(emaSlowLen=100, adxLen=200)
            self.assertEqual(main.required_rate_count(), 410)
        finally:
            main.CONFIG.clear()
            main.CONFIG.update(original)

    @patch.object(main.mt5, "copy_rates_from_pos", return_value=[{"close": 1.0}])
    @patch.object(main.mt5, "initialize", return_value=True)
    def test_analyze_data_rejects_incomplete_history(self, _initialize, _copy_rates):
        self.assertIsNone(main.analyze_data())


class DashboardSecurityTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _ok_response(_request):
        return JSONResponse({"ok": True})

    async def test_dashboard_requires_authentication(self):
        request = Request({"type": "http", "method": "GET", "path": "/api/config", "headers": []})
        response = await main.dashboard_security(request, self._ok_response)
        self.assertEqual(response.status_code, 401)

    async def test_authenticated_request_and_mutation_guard(self):
        credentials = base64.b64encode(
            f"{main.DASHBOARD_USERNAME}:{main.DASHBOARD_PASSWORD}".encode()
        )
        headers = [(b"authorization", b"Basic " + credentials)]
        request = Request({"type": "http", "method": "GET", "path": "/api/config", "headers": headers})
        response = await main.dashboard_security(request, self._ok_response)
        self.assertEqual(response.status_code, 200)

        mutation = Request({"type": "http", "method": "POST", "path": "/api/start", "headers": headers})
        response = await main.dashboard_security(mutation, self._ok_response)
        self.assertEqual(response.status_code, 405)

        confirmed_headers = headers + [(b"x-bot-action", b"confirm")]
        confirmed = Request({"type": "http", "method": "POST", "path": "/api/start", "headers": confirmed_headers})
        response = await main.dashboard_security(confirmed, self._ok_response)
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
