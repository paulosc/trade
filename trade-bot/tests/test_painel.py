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
        self.assertIn("Trocar chaves", body)              # chaves salvas: só confirmação + botão
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

    def test_req17_radar_endpoint_requires_token(self):
        self.p.fetch_bars = lambda sym, cfg: []
        self.p.fetch_prices = lambda syms, cfg: {}
        self.assertEqual(self.req("/api/radar")[0], 403)
        code, body = self.req("/api/radar", token=self.p.token)
        self.assertEqual(code, 200)
        self.assertIn("items", json.loads(body))
        code, page = self.req("/")
        self.assertIn("Radar: o que o robô está vigiando agora", page)


# ---------------------------------------------------------------- radar (REQ-17)

RCFG = {"bar_minutes": 5, "opening_range_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0,
        "no_entries_before": "09:45", "no_entries_after": "15:30", "flat_by": "15:55"}


def ny(hh, mm, ss=0):
    from tradebot import clock
    return datetime(2026, 10, 28, hh, mm, ss, tzinfo=clock.NY)        # quarta-feira


def bar(slot, o, h, l, c):
    t = int(ny(9, 30).timestamp()) + slot * 300
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 1000}


# faixa de abertura (3 barras): máxima 101, mínima 99, stop (meio) 100
OPENING = [bar(0, 100, 101, 99.5, 100.5), bar(1, 100.5, 100.8, 99, 99.6), bar(2, 99.6, 100.9, 99.4, 100.2)]


class TestRadar(unittest.TestCase):
    def r(self, bars, now, price=None, **kw):
        return painel.symbol_radar("AAPL", bars, price, now, RCFG, **kw)

    def test_req17_before_open_waits(self):
        it = self.r([], ny(9, 0))
        self.assertEqual(it["state"], "antes")
        self.assertIn("09:45", it["text"])

    def test_req17_opening_range_forming(self):
        it = self.r(OPENING[:2], ny(9, 41), price=100.1)
        self.assertEqual(it["state"], "formando")
        self.assertEqual(it["or_high"], 101)

    def test_req17_watching_shows_distance_to_trigger(self):
        bars = OPENING + [bar(3, 100.2, 100.6, 100, 100.4)]
        it = self.r(bars, ny(9, 52), price=100.0)
        self.assertEqual(it["state"], "vigiando")
        self.assertEqual((it["or_high"], it["or_low"], it["stop"]), (101, 99, 100.0))
        self.assertEqual(it["dist"], 1.0)
        self.assertIn("Falta US$ 1,00", it["text"])
        self.assertEqual(it["closes"], [100.5, 99.6, 100.2, 100.4])

    def test_req17_above_trigger_before_bar_closes(self):
        it = self.r(OPENING, ny(9, 47), price=101.4)
        self.assertEqual(it["state"], "acima")
        self.assertIn("09:50", it["text"])                         # a barra atual fecha às 09:50

    def test_req17_breakout_on_last_closed_bar_is_signal(self):
        bars = OPENING + [bar(3, 100.5, 101.6, 100.4, 101.5)]
        it = self.r(bars, ny(9, 51), price=101.5)
        self.assertEqual(it["state"], "sinal")
        self.assertIn("09:50", it["text"])

    def test_req17_old_breakout_is_gone_for_the_day(self):
        bars = OPENING + [bar(3, 100.5, 101.6, 100.4, 101.5), bar(4, 101.5, 101.7, 101, 101.2)]
        it = self.r(bars, ny(9, 56), price=101.2)
        self.assertEqual(it["state"], "perdeu")

    def test_req17_ignores_bar_still_forming(self):
        # barra 3 ainda aberta às 09:47, mesmo que a fonte já mande: não vale como sinal
        bars = OPENING + [bar(3, 100.5, 101.6, 100.4, 101.5)]
        self.assertEqual(self.r(bars, ny(9, 47), price=101.5)["state"], "acima")

    def test_req17_journal_veto_and_position(self):
        veto = [{"event": "timemachine", "symbol": "AAPL", "approved": False, "reason": "vantagem pequena"}]
        self.assertEqual(self.r(OPENING, ny(10, 0), events=veto)["state"], "vetado")
        it = self.r(OPENING, ny(10, 0), position={"qty": "10", "avg": "US$ 101,00"})
        self.assertEqual(it["state"], "posicao")

    def test_req17_after_entry_window(self):
        bars = OPENING + [bar(3, 100, 100.5, 99.8, 100.1)]
        self.assertEqual(self.r(bars, ny(15, 40), price=100.0)["state"], "fora")


class TestRadarPainel(Base):
    def test_req17_radar_reads_only_and_caches(self):
        calls, fetched = [], []
        p = self.make(send=fake_send(calls=calls))
        painel.save_setup(self.cfg_path, "sim", "AAPL, NVDA")

        def bars(sym, cfg):
            fetched.append(sym)
            return OPENING
        p.fetch_bars = bars
        p.fetch_prices = lambda syms, cfg: {"AAPL": 100.0}
        now = ny(9, 52, 30).astimezone(timezone.utc)
        r = p.radar(now)
        self.assertEqual([i["symbol"] for i in r["items"]], ["AAPL", "NVDA"])
        self.assertEqual(r["bar_close_at"], "09:55")
        self.assertEqual(r["bar_close_in"], 150)
        self.assertTrue(any("desligado" in n for n in r["notes"]))
        r2 = p.radar(now.replace(second=40))                          # dentro dos 30 s: cache
        self.assertEqual(len(fetched), 2)
        self.assertEqual(r2["bar_close_in"], 140)                     # mas a contagem anda
        self.assertFalse([c for c in calls if c[0] != "GET"])         # nunca envia ordem

    def test_req17_fetch_error_is_shown_not_raised(self):
        p = self.make()
        painel.save_setup(self.cfg_path, "sim", "AAPL")

        def boom(sym, cfg):
            raise OSError("sem rede")
        p.fetch_bars, p.fetch_prices = boom, boom
        r = p.radar(ny(10, 0).astimezone(timezone.utc))
        self.assertEqual(r["items"][0]["state"], "sem_dados")
        self.assertIn("sem rede", r["items"][0]["text"])


if __name__ == "__main__":
    unittest.main()
