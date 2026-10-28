import tempfile
import unittest
from collections import OrderedDict
from datetime import date, timezone

from helpers import cfg, flat_then_breakout, make_session, ny, random_sessions, N
from tradebot import backtest
from tradebot.broker import BrokerError, PaperSim
from tradebot.engine import Engine
from tradebot.journal import Journal

TODAY = date(2026, 10, 19)


class FakeData:
    def __init__(self, sessions=None, fail=False):
        self.s, self.fail, self.calls = sessions, fail, 0

    def sessions(self, symbol, now=None):
        self.calls += 1
        if self.fail:
            raise OSError("rede caiu")
        return self.s


def history_plus_today(today_session, drift=0.0015):
    days = random_sessions(30, seed=3, drift=drift, vol=0.001, start=date(2026, 8, 1))
    days[TODAY] = today_session
    return days


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.j = Journal(self.tmp.name)
        self.b = PaperSim(cash=10000)

    def tearDown(self):
        self.tmp.cleanup()

    def eng(self, data, **over):
        return Engine(cfg(**over), self.b, data, self.j, "sim")

    def at(self, hh, mm):
        return ny(TODAY, hh, mm).astimezone(timezone.utc)

    def events(self):
        return [e["event"] for e in self.j.read(TODAY.isoformat())]

    def test_places_bracket_on_fresh_signal(self):
        # rompimento na barra 3 (09:45-09:50), avaliada às 09:50
        e = self.eng(FakeData(history_plus_today(flat_then_breakout(TODAY))))
        self.assertEqual(e.tick(self.at(9, 50)), "ok")
        self.assertEqual(len(self.b.orders), 1)
        o = self.b.orders[0]
        self.assertEqual((o["symbol"], o["order_class"], o["stop_loss"]["stop_price"]), ("TEST", "bracket", "100.00"))
        self.assertEqual(self.events(), ["signal", "timemachine", "order"])
        # REQ-07: não opera o mesmo ativo de novo no mesmo dia
        e.tick(self.at(9, 55))
        self.assertEqual(len(self.b.orders), 1)

    def test_req12_slippage_capped_by_risk(self):
        e = self.eng(FakeData(history_plus_today(flat_then_breakout(TODAY))), max_slippage_pct=5.0)
        e.tick(self.at(9, 50))
        o = self.b.orders[0]
        # entrada 101, stop 100 -> risco 1.00 -> tolerância máx 0.20 (e não 5% = 5.05)
        self.assertEqual(o["limit_price"], "101.20")
        self.assertEqual(o["take_profit"]["limit_price"], "102.40")

    def test_req05_timemachine_blocks_without_edge(self):
        e = self.eng(FakeData(history_plus_today(flat_then_breakout(TODAY), drift=-0.0015)))
        e.tick(self.at(9, 50))
        self.assertEqual(self.b.orders, [])
        self.assertIn("timemachine", self.events())

    def test_risk_block_logged(self):
        e = self.eng(FakeData(history_plus_today(flat_then_breakout(TODAY))), max_trades_per_day=0)
        e.tick(self.at(9, 50))
        self.assertEqual(self.b.orders, [])

    def test_no_signal_no_order(self):
        e = self.eng(FakeData(history_plus_today(make_session(TODAY, [100.0] * N))))
        e.tick(self.at(10, 30))
        self.assertEqual(self.b.orders, [])

    def test_market_closed(self):
        self.b.is_open = False
        self.assertEqual(self.eng(FakeData()).tick(self.at(10, 0)), "closed")

    def test_req09_flat_by_flattens_once(self):
        e = self.eng(FakeData())
        self.assertEqual(e.tick(self.at(15, 55)), "flat")
        e.tick(self.at(15, 56))
        self.assertEqual(self.b.flattened, 1)

    def test_req09_flat_by_retries_until_broker_confirms(self):
        real, fails = self.b.flatten, [1]

        def flaky():
            if fails:
                fails.pop()
                raise BrokerError("zerar não confirmado: ainda há posições abertas (AMD 6)")
            return real()
        self.b.flatten = flaky
        e = self.eng(FakeData())
        self.assertEqual(e.tick(self.at(15, 55)), "error")             # não marca o dia como zerado
        self.assertFalse(self.j.day_state(TODAY.isoformat())["flattened"])
        self.assertEqual(e.tick(self.at(15, 56)), "flat")              # tenta de novo e confirma
        self.assertTrue(self.j.day_state(TODAY.isoformat())["flattened"])
        self.assertEqual(self.b.flattened, 1)
        self.assertIn("error", self.events())

    def test_req09_daily_loss_flatten_retried(self):
        self.b.last_equity = 10200
        real, fails = self.b.flatten, [1]

        def flaky():
            if fails:
                fails.pop()
                raise BrokerError("zerar não confirmado")
            return real()
        self.b.flatten = flaky
        e = self.eng(FakeData(history_plus_today(flat_then_breakout(TODAY))))
        self.assertEqual(e.tick(self.at(9, 50)), "halted")
        self.assertEqual(self.b.flattened, 0)
        self.assertEqual(e.tick(self.at(9, 51)), "halted")
        self.assertEqual(self.b.flattened, 1)
        e.tick(self.at(9, 52))
        self.assertEqual(self.b.flattened, 1)                          # depois de confirmar, não repete

    def test_req09_daily_loss_halts_and_flattens(self):
        self.b.last_equity = 10200          # perdeu 200 hoje > 150
        e = self.eng(FakeData(history_plus_today(flat_then_breakout(TODAY))))
        self.assertEqual(e.tick(self.at(9, 50)), "halted")
        self.assertEqual(self.b.flattened, 1)
        self.assertEqual(self.b.orders, [])

    def test_sec05_kill_switch(self):
        self.b.pos = {"TEST": 5}
        self.j.set_kill(True)
        e = self.eng(FakeData(history_plus_today(flat_then_breakout(TODAY))))
        self.assertEqual(e.tick(self.at(9, 50)), "killed")
        self.assertEqual(self.b.flattened, 1)
        self.assertEqual(self.b.orders, [])
        self.j.set_kill(False)
        self.assertFalse(self.j.killed())

    def test_sec06_data_error_never_trades(self):
        e = self.eng(FakeData(fail=True))
        self.assertEqual(e.tick(self.at(9, 50)), "ok")
        self.assertEqual(self.b.orders, [])
        self.assertEqual(self.events(), ["error"])

    def test_sec06_broker_error_logged(self):
        def boom():
            raise BrokerError("HTTP 500")
        self.b.clock = boom
        self.assertEqual(self.eng(FakeData()).tick(self.at(10, 0)), "error")

    def test_run_forever_survives_exceptions(self):
        e = self.eng(FakeData())
        e.tick = lambda now=None: 1 / 0
        e.run_forever(sleep=lambda s: None, max_ticks=2)

    def test_sec10_run_forever_stops_on_event(self):
        import threading
        e = self.eng(FakeData())
        ticks = []
        stop = threading.Event()

        def tick(now=None):
            ticks.append(1)
            stop.set()                                  # o painel fechou durante o ciclo
            return "ok"
        e.tick = tick
        e.cfg["poll_seconds"] = 3600                     # sem o evento, ficaria 1 hora dormindo
        e.run_forever(stop=stop)
        self.assertEqual(len(ticks), 1)


class TestRunLock(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_sec10_only_one_robot_per_state_dir(self):
        from tradebot.journal import release_run_lock
        j = Journal(self.dir)
        first = j.acquire_run_lock()
        self.assertIsNotNone(first)
        self.assertIsNone(Journal(self.dir).acquire_run_lock())
        self.assertTrue(j.run_lock_held())
        release_run_lock(first)
        self.assertFalse(j.run_lock_held())
        again = j.acquire_run_lock()
        self.assertIsNotNone(again)
        release_run_lock(again)

    def test_sec10_cli_run_refuses_second_robot(self):
        import io, json, os
        from contextlib import redirect_stderr
        from tradebot import __main__ as cli
        from tradebot.journal import release_run_lock
        c = cfg(state_dir=self.dir, mode="sim")
        path = os.path.join(self.dir, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(c, f)
        lock = Journal(self.dir).acquire_run_lock()
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                rc = cli.main(["--config", path, "run"])
        finally:
            release_run_lock(lock)
        self.assertEqual(rc, 3)
        self.assertIn("já existe um robô", err.getvalue())

    def test_sec10_watch_parent_stops_on_end_of_input(self):
        import io, threading
        from tradebot.__main__ import watch_parent
        stop = threading.Event()
        watch_parent(io.StringIO("qualquer coisa"), stop)
        self.assertTrue(stop.is_set())


class TestBacktest(unittest.TestCase):
    def test_req11_play_out(self):
        sig = {"entry": 101.0, "stop": 100.0, "target": 102.0, "slot": 3}
        up = flat_then_breakout(TODAY, after={10: 103.0})
        self.assertEqual(backtest.play_out(up, sig, 76), (1.0, "target"))
        down = flat_then_breakout(TODAY, after={10: 99.0})
        self.assertEqual(backtest.play_out(down, sig, 76), (-1.0, "stop"))

    def test_req11_walk_forward_summary(self):
        days = OrderedDict()
        for d, s in random_sessions(40, seed=11).items():
            days[d] = s
        res = backtest.run(days, cfg(), use_timemachine=False)
        self.assertGreater(res["trades"], 0)
        for k in ("win_rate", "avg_r", "total_r", "max_dd_r"):
            self.assertIn(k, res)
        filtered = backtest.run(days, cfg(), use_timemachine=True)
        self.assertLessEqual(filtered["trades"], res["trades"])

    def test_summarize_drawdown(self):
        s = backtest.summarize([{"r": 1}, {"r": -1}, {"r": -1}, {"r": 2}])
        self.assertEqual((s["total_r"], s["max_dd_r"], s["win_rate"]), (1, 2, 0.5))


if __name__ == "__main__":
    unittest.main()
