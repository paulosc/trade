import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone

from helpers import cfg  # noqa: F401  (ajusta sys.path)
from tradebot import painel

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "config.example.json")


def fake_send(status=200, doc=None, calls=None):
    def send(method, url, headers, body):
        if calls is not None:
            calls.append((method, url))
        if url.endswith("/v2/clock"):
            return 200, {"is_open": False, "next_open": "2026-10-29T09:30:00-04:00"}
        if url.endswith("/v2/positions"):
            return 200, []
        return status, doc if doc is not None else {"equity": "100000", "last_equity": "99000",
                                                    "buying_power": "200000"}
    return send


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shutil.copyfile(EXAMPLE, os.path.join(self.dir, "config.example.json"))
        self.cfg_path = os.path.join(self.dir, "config.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, **kw):
        kw.setdefault("send", fake_send())
        kw.setdefault("base_env", {})
        return painel.Painel(self.cfg_path, **kw)


class TestSetup(Base):
    def test_req13_creates_config_from_example(self):
        self.assertFalse(os.path.exists(self.cfg_path))
        self.make()
        self.assertTrue(os.path.exists(self.cfg_path))

    def test_req13_watchlist_parsing(self):
        self.assertEqual(painel.parse_watchlist("aapl, nvda;TSLA  aapl"), ["AAPL", "NVDA", "TSLA"])
        for bad in ("", "AAPL, rm -rf", "1234"):
            with self.assertRaises(painel.PainelError):
                painel.parse_watchlist(bad)

    def test_sec07_panel_never_writes_live(self):
        self.make()
        with self.assertRaises(painel.PainelError):
            painel.save_setup(self.cfg_path, "live", "AAPL")
        cfg = painel.save_setup(self.cfg_path, "sim", "AAPL, MSFT")
        self.assertEqual((cfg["mode"], cfg["watchlist"]), ("sim", ["AAPL", "MSFT"]))

    def test_sec07_paper_needs_saved_keys(self):
        p = self.make()
        with self.assertRaises(painel.PainelError):
            p.save_setup("paper", "AAPL")


class TestKeys(Base):
    def test_req14_good_keys_are_tested_then_saved(self):
        calls = []
        p = self.make(send=fake_send(calls=calls))
        res = p.save_keys(" PKABCDEFGH1234 ", "segredo")
        self.assertTrue(res["ok"])
        self.assertIn("100.000,00", res["equity"])
        self.assertEqual(calls[0], ("GET", painel.PAPER_URL + "/v2/account"))
        env = painel.read_env_file(os.path.join(self.dir, ".env"))
        self.assertEqual(env["ALPACA_PAPER_KEY_ID"], "PKABCDEFGH1234")

    def test_req14_bad_keys_are_not_saved(self):
        p = self.make(send=fake_send(status=401, doc={"message": "unauthorized"}))
        with self.assertRaises(painel.PainelError) as ctx:
            p.save_keys("PKX", "Y")
        self.assertIn("recusou", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".env")))

    def test_sec07_live_keys_rejected(self):
        with self.assertRaises(painel.PainelError):
            painel.check_paper_keys("AKLIVE123", "s", send=fake_send())

    def test_sec09_env_file_preserves_other_lines(self):
        path = os.path.join(self.dir, ".env")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# meu comentário\nOUTRA=1\nALPACA_PAPER_KEY_ID=velha\n")
        painel.write_env_file(path, {"ALPACA_PAPER_KEY_ID": "nova", "ALPACA_PAPER_SECRET_KEY": "s"})
        with open(path, encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("# meu comentário", txt)
        self.assertIn("OUTRA=1", txt)
        self.assertEqual(painel.read_env_file(path)["ALPACA_PAPER_KEY_ID"], "nova")

    def test_sec09_status_never_returns_secret(self):
        p = self.make()
        p.save_keys("PKABCDEFGH1234", "SEGREDO-MUITO-SECRETO")
        painel.save_setup(self.cfg_path, "paper", "AAPL")
        dump = json.dumps(p.status(), ensure_ascii=False)
        self.assertNotIn("SEGREDO-MUITO-SECRETO", dump)
        self.assertNotIn("PKABCDEFGH1234", dump)
        self.assertIn("…1234", dump)


class TestRobot(Base):
    def test_sec07_robot_env_strips_live(self):
        base = {"PATH": "x", "ALPACA_LIVE_KEY_ID": "L", "ALPACA_LIVE_SECRET_KEY": "L",
                "TRADEBOT_LIVE_CONFIRM": "I-ACCEPT-REAL-MONEY-RISK"}
        env = painel.robot_env(base, {"ALPACA_PAPER_KEY_ID": "k", "ALPACA_PAPER_SECRET_KEY": "s"}, "paper")
        for k in painel.LIVE_VARS:
            self.assertNotIn(k, env)
        self.assertEqual(env["ALPACA_PAPER_KEY_ID"], "k")
        self.assertNotIn("ALPACA_PAPER_KEY_ID", painel.robot_env(base, {"ALPACA_PAPER_KEY_ID": "k"}, "sim"))

    def test_sec07_refuses_to_start_live_config(self):
        p = self.make()
        with open(self.cfg_path, encoding="utf-8") as f:
            c = json.load(f)
        c["mode"] = "live"
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(c, f)
        with self.assertRaises(painel.PainelError):
            p.start()
        self.assertFalse(p.runner.running)

    def test_req15_start_and_stop_process(self):
        script = "import time\nprint('[09:50:00] outside-window', flush=True)\ntime.sleep(60)"
        p = self.make(robot_cmd=[sys.executable, "-c", script])
        painel.save_setup(self.cfg_path, "sim", "AAPL")
        p.start()
        self.assertTrue(p.runner.running)
        for _ in range(100):
            if p.runner.lines:
                break
            threading.Event().wait(0.05)
        st = p.status()["robot"]
        self.assertEqual(st["last_status"], "outside-window")
        self.assertTrue(st["last_status_pt"])          # fora do pregão vira "bolsa fechada"
        p.stop()
        self.assertFalse(p.runner.running)

    def test_req15_kill_blocks_start(self):
        p = self.make()
        painel.save_setup(self.cfg_path, "sim", "AAPL")
        p.kill(True)
        self.assertTrue(p.status()["killed"])
        with self.assertRaises(painel.PainelError):
            p.start()
        p.kill(False)
        self.assertFalse(p.status()["killed"])

    def test_req15_flatten_paper_calls_broker(self):
        calls = []
        p = self.make(send=fake_send(calls=calls))
        p.save_keys("PKABCDEFGH1234", "s")
        painel.save_setup(self.cfg_path, "paper", "AAPL")
        p.flatten()
        self.assertIn(("DELETE", painel.PAPER_URL + "/v2/orders"), calls)


class TestStatus(Base):
    def test_req16_events_in_plain_language(self):
        kinds = {
            "signal": {"event": "signal", "symbol": "ACME", "entry": 10, "stop": 9, "target": 11},
            "aprovado": {"event": "timemachine", "symbol": "ACME", "approved": True, "p_target": .72, "p_stop": .26},
            "vetado": {"event": "timemachine", "symbol": "BRVO", "approved": False, "reason": "vantagem insuficiente"},
            "bloqueado": {"event": "blocked", "symbol": "X", "reasons": ["limite de operações do dia"]},
            "ordem": {"event": "order", "symbol": "ACME", "qty": 3, "entry": 10, "stop": 9, "target": 11},
            "erro": {"event": "error", "where": "data", "symbol": "X", "error": "timeout"},
        }
        for kind, rec in kinds.items():
            k, text = painel.describe_event(rec)
            self.assertEqual(k, "sinal" if kind == "signal" else kind)
            self.assertTrue(text)
        self.assertIn("72%", painel.describe_event(kinds["aprovado"])[1])

    def test_req16_status_market_and_journal(self):
        p = self.make()
        painel.save_setup(self.cfg_path, "sim", "AAPL")
        now = datetime(2026, 10, 28, 14, 0, tzinfo=timezone.utc)      # 10:00 em NY, quarta
        jr = p.journal(p.cfg())
        jr.log("2026-10-28", "signal", symbol="AAPL", entry=1, stop=0.5, target=1.5)
        st = p.status(now=now)
        self.assertEqual(st["market"]["ny_time"], "10:00")
        self.assertTrue(st["market"]["is_open"])
        self.assertTrue(st["market"]["entries_from"].startswith("2026-10-28T09:45"))
        self.assertEqual(st["events"][0]["kind"], "sinal")
        self.assertIsNone(st["account"])

    def test_req16_outside_window_while_closed_says_closed(self):
        p = self.make(robot_cmd=[sys.executable, "-c", "print('[06:00:00] outside-window', flush=True)"])
        painel.save_setup(self.cfg_path, "sim", "AAPL")
        p.start()
        for _ in range(100):
            if p.runner.lines:
                break
            threading.Event().wait(0.05)
        p.stop()
        early = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)    # 06:00 em NY
        late = datetime(2026, 10, 28, 19, 45, tzinfo=timezone.utc)    # 15:45 em NY
        self.assertEqual(p.status(now=early)["robot"]["last_status_pt"], painel.STATUS_PT["closed"])
        self.assertEqual(p.status(now=late)["robot"]["last_status_pt"], painel.STATUS_PT["outside-window"])

    def test_req16_paper_status_reads_account(self):
        p = self.make()
        p.save_keys("PKABCDEFGH1234", "s")
        painel.save_setup(self.cfg_path, "paper", "AAPL")
        st = p.status()
        self.assertEqual(st["market"]["source"], "alpaca")
        self.assertIn("1.000,00", st["account"]["day_pnl"])


class TestHttp(Base):
    def setUp(self):
        super().setUp()
        self.p = self.make()
        self.srv = painel.make_server(self.p, port=0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, path, token=None, host=None, body=None):
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                   data=None if body is None else json.dumps(body).encode(),
                                   method="GET" if body is None else "POST")
        if token:
            r.add_header("X-Painel-Token", token)
        if host:
            r.add_header("Host", host)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_req13_page_has_token_and_steps(self):
        code, body = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn(self.p.token, body)
        self.assertIn('rel="icon"', body)
        for step in ("Seu computador", "Como você quer rodar?", "Ligar o robô", "Acompanhar"):
            self.assertIn(step, body)

    def test_sec08_api_requires_token(self):
        self.assertEqual(self.req("/api/status")[0], 403)
        self.assertEqual(self.req("/api/start", token="errado", body={})[0], 403)
        code, body = self.req("/api/status", token=self.p.token)
        self.assertEqual(code, 200)
        self.assertIn("watchlist", json.loads(body))

    def test_sec08_rejects_foreign_host(self):
        self.assertEqual(self.req("/", host="evil.example:%d" % self.port)[0], 403)
        self.assertEqual(self.req("/api/status", token=self.p.token, host="evil.example")[0], 403)

    def test_sec08_listens_on_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")

    def test_sec07_api_refuses_live_mode(self):
        code, body = self.req("/api/setup", token=self.p.token, body={"mode": "live", "watchlist": "AAPL"})
        self.assertEqual(code, 400)
        self.assertIn("live", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
