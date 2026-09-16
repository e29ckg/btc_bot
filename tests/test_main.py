import unittest
import base64
from types import SimpleNamespace
from unittest.mock import patch

import main
from starlette.requests import Request
from starlette.responses import JSONResponse


class TradingHelpersTest(unittest.TestCase):
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
