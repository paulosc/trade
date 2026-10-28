import json
import unittest

from helpers import cfg  # noqa: F401  (ajusta sys.path)
from tradebot import broker, data

LIVE_ENV = {"TRADEBOT_LIVE_CONFIRM": broker.LIVE_CONFIRM_PHRASE,
            "ALPACA_LIVE_KEY_ID": "LK", "ALPACA_LIVE_SECRET_KEY": "LS"}


class TestCredentials(unittest.TestCase):
    def test_sec01_default_is_paper(self):
        mode, url, *_ = broker.resolve_credentials({}, {"ALPACA_PAPER_KEY_ID": "k", "ALPACA_PAPER_SECRET_KEY": "s"})
        self.assertEqual((mode, url), ("paper", broker.PAPER_URL))

    def test_sec02_live_requires_all_three(self):
        self.assertEqual(broker.resolve_credentials({"mode": "live"}, LIVE_ENV)[:2], ("live", broker.LIVE_URL))
        for drop in LIVE_ENV:
            env = {k: v for k, v in LIVE_ENV.items() if k != drop}
            with self.assertRaises(broker.ConfigError):
                broker.resolve_credentials({"mode": "live"}, env)
        with self.assertRaises(broker.ConfigError):
            broker.resolve_credentials({"mode": "live"}, dict(LIVE_ENV, TRADEBOT_LIVE_CONFIRM="yes"))

    def test_sec02_paper_keys_never_used_for_live(self):
        env = {"TRADEBOT_LIVE_CONFIRM": broker.LIVE_CONFIRM_PHRASE,
               "ALPACA_PAPER_KEY_ID": "k", "ALPACA_PAPER_SECRET_KEY": "s"}
        with self.assertRaises(broker.ConfigError):
            broker.resolve_credentials({"mode": "live"}, env)

    def test_invalid_mode(self):
        with self.assertRaises(broker.ConfigError):
            broker.resolve_credentials({"mode": "yolo"}, {})


class TestAlpaca(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def send(method, url, headers, body):
            self.calls.append((method, url, headers, body))
            if url.endswith("/v2/account"):
                return 200, {"equity": "1000"}
            if method == "POST":
                return 200, {"id": "abc"}
            return 403, {"message": "forbidden"}
        self.a = broker.Alpaca(broker.PAPER_URL, "KEY", "SECRET", send=send)

    def test_req08_bracket_payload(self):
        self.a.submit_bracket("TSLA", 8, 364.55, 358.32, 370.04, "tb-1")
        method, url, headers, body = self.calls[-1]
        self.assertEqual((method, url), ("POST", broker.PAPER_URL + "/v2/orders"))
        self.assertEqual(body["order_class"], "bracket")
        self.assertEqual(body["qty"], "8")
        self.assertEqual(body["limit_price"], "364.55")
        self.assertEqual(body["stop_loss"], {"stop_price": "358.32"})
        self.assertEqual(body["take_profit"], {"limit_price": "370.04"})
        self.assertEqual(body["client_order_id"], "tb-1")

    def test_req08_client_order_id_deterministic(self):
        a = broker.client_order_id("2026-10-19", "TSLA", 9)
        self.assertEqual(a, broker.client_order_id("2026-10-19", "TSLA", 9))
        self.assertNotEqual(a, broker.client_order_id("2026-10-19", "TSLA", 10))

    def test_http_error_raises(self):
        with self.assertRaises(broker.BrokerError):
            self.a.positions()

    def test_sec03_repr_hides_keys(self):
        self.assertNotIn("SECRET", repr(self.a))
        self.assertNotIn("KEY", repr(self.a))


class TestData(unittest.TestCase):
    def test_parse_yahoo_skips_nulls(self):
        raw = json.dumps({"chart": {"result": [{"timestamp": [1, 2], "indicators": {"quote": [{
            "open": [1, None], "high": [2, 3], "low": [0.5, 1], "close": [1.5, 2], "volume": [10, 20]}]}}]}})
        bars = data.parse_yahoo(raw)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["c"], 1.5)

    def test_parse_yahoo_error(self):
        with self.assertRaises(data.DataError):
            data.parse_yahoo('{"chart": {"result": null}}')

    def test_alpaca_pagination_and_grouping(self):
        pages = [
            {"bars": [{"t": "2026-10-19T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1}],
             "next_page_token": "p2"},
            {"bars": [{"t": "2026-10-19T13:35:00Z", "o": 1.5, "h": 2, "l": 1, "c": 1.8, "v": 1},
                      {"t": "2026-10-19T21:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}],
             "next_page_token": None},
        ]
        seen = []

        def get(url, headers=None):
            seen.append(url)
            return json.dumps(pages[len(seen) - 1])
        bars = data.fetch_alpaca("TSLA", "k", "s", get=get)
        self.assertEqual(len(bars), 3)
        self.assertIn("page_token=p2", seen[1])
        days = data.group_sessions(bars, 5)
        (d, s), = days.items()
        self.assertEqual(d.isoformat(), "2026-10-19")
        self.assertEqual(s[0]["c"], 1.5)           # 09:30 ET
        self.assertEqual(s[1]["c"], 1.8)           # 09:35 ET; barra pós-mercado descartada
        self.assertEqual(sum(b is not None for b in s), 2)

    def test_req09_flatten_waits_for_cancels_and_confirms(self):
        # o caso de 28/10: stop e alvo ainda "em cancelamento" seguram as ações
        calls, state = [], {"open_polls": 2, "pos_polls": 1, "closed": False}

        def send(method, url, headers, body):
            calls.append((method, url.split("paper-api.alpaca.markets")[-1]))
            if method == "GET" and "/v2/orders" in url:
                if state["open_polls"] > 0:
                    state["open_polls"] -= 1
                    return 200, [{"symbol": "AMD", "status": "pending_cancel"}]
                return 200, []
            if method == "DELETE" and "/v2/positions" in url:
                assert state["open_polls"] == 0, "fechou antes de o cancelamento terminar"
                state["closed"] = True
                return 207, [{"symbol": "AMD", "status": 200}]
            if method == "GET" and url.endswith("/v2/positions"):
                if not state["closed"] or state["pos_polls"] > 0:
                    state["pos_polls"] -= 1
                    return 200, [{"symbol": "AMD", "qty": "6"}]
                return 200, []
            return 200, []
        a = broker.Alpaca(broker.PAPER_URL, "k", "s", send=send, sleep=lambda s: None)
        self.assertEqual(a.flatten(), [])
        deletes = [c for c in calls if c[0] == "DELETE"]
        self.assertEqual(deletes[0][1], "/v2/orders")
        self.assertIn("/v2/positions", deletes[1][1])

    def test_req09_flatten_raises_when_position_stays(self):
        def send(method, url, headers, body):
            if method == "GET" and url.endswith("/v2/positions"):
                return 200, [{"symbol": "AMD", "qty": "6"}]
            if method == "DELETE" and "/v2/positions" in url:
                return 207, [{"symbol": "AMD", "status": 403, "body": {"message": "insufficient qty available"}}]
            return 200, []
        a = broker.Alpaca(broker.PAPER_URL, "k", "s", send=send, sleep=lambda s: None)
        with self.assertRaises(broker.BrokerError) as e:
            a.flatten(tries=2, polls=2)
        self.assertIn("AMD 6", str(e.exception))

    def test_datasource_falls_back_to_yahoo_without_keys(self):
        self.assertEqual(data.DataSource({"data_source": "alpaca"}, {}).kind, "yahoo")

    def test_req18_history_once_per_day_then_short_window(self):
        from datetime import datetime, timezone
        from urllib.parse import parse_qs, urlparse
        seen = []
        past_bar = {"t": "2026-10-26T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1}
        today_bars = [{"t": "2026-10-28T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.6, "v": 1}]

        def get(url, headers=None):
            start = parse_qs(urlparse(url).query)["start"][0]
            seen.append(start)
            bars = list(today_bars) + ([past_bar] if start < "2026-10-20" else [])
            return json.dumps({"bars": bars, "next_page_token": None})

        ds = data.DataSource({"data_source": "alpaca", "bar_minutes": 5,
                              "timemachine": {"lookback_sessions": 59}},
                             {"ALPACA_PAPER_KEY_ID": "k", "ALPACA_PAPER_SECRET_KEY": "s"}, get=get)
        now = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
        first = ds.sessions("AMD", now=now)
        self.assertEqual(len(first), 2)
        self.assertLess(seen[0], "2026-08-01")                       # histórico completo
        today_bars.append({"t": "2026-10-28T13:35:00Z", "o": 1.6, "h": 2, "l": 1, "c": 1.9, "v": 1})
        second = ds.sessions("AMD", now=now.replace(minute=1))
        self.assertGreaterEqual(seen[1], "2026-10-25")               # só a janela curta
        self.assertEqual(list(second), list(first))                  # o passado veio do cache
        self.assertEqual(second[list(second)[-1]][1]["c"], 1.9)      # e hoje foi atualizado
        ds.sessions("AMD", now=datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc))
        self.assertLess(seen[2], "2026-08-01")                       # virou o dia: baixa tudo de novo


if __name__ == "__main__":
    unittest.main()
