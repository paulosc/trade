"""CLI: python -m tradebot <comando>. Ver README.md."""

import argparse
import json
import os
import sys
import threading

from . import __version__, backtest, clock
from .broker import Alpaca, ConfigError, PaperSim, resolve_credentials
from .data import DataSource
from .engine import Engine
from .journal import Journal, release_run_lock

EXIT_WITH_PARENT = "TRADEBOT_EXIT_WITH_PARENT"


def watch_parent(stream, stop):
    """SEC-10: o painel mantém a entrada do robô aberta; quando ele fecha (por qualquer motivo), ela termina."""
    try:
        while stream.read(4096):
            pass
    except (OSError, ValueError):
        pass
    stop.set()

REQUIRED = ["watchlist", "flat_by", "no_entries_before", "no_entries_after", "max_loss_per_trade_usd",
            "max_daily_loss_usd", "max_trades_per_day", "max_open_positions", "max_order_usd"]


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED if k not in cfg]
    if missing:
        raise ConfigError("config sem: %s" % ", ".join(missing))
    return cfg


def build(cfg, env):
    mode, url, key, secret = resolve_credentials(cfg, env)
    broker = PaperSim(cash=float(cfg.get("sim_cash", 5000))) if mode == "sim" else Alpaca(url, key, secret)
    journal = Journal(cfg.get("state_dir", "state"))
    return Engine(cfg, broker, DataSource(cfg, env), journal, mode), mode


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tradebot", description="Robô ORB + Máquina do Tempo (Alpaca)")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="roda em loop durante o pregão")
    sub.add_parser("tick", help="roda um único ciclo")
    sub.add_parser("status", help="modo, conta e estado do dia")
    sub.add_parser("flatten", help="cancela ordens e zera posições agora")
    k = sub.add_parser("kill", help="liga/desliga o kill switch")
    k.add_argument("state", choices=["on", "off"])
    b = sub.add_parser("backtest", help="backtest walk-forward nos últimos ~60 dias")
    b.add_argument("symbols", nargs="*")
    b.add_argument("--no-timemachine", action="store_true")
    pn = sub.add_parser("painel", help="abre o painel no navegador (passo a passo para sim e paper)")
    pn.add_argument("--port", type=int, default=8765)
    pn.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "painel":                       # REQ-13: funciona mesmo sem config.json
        from . import painel
        return painel.serve(a.config, port=a.port, open_browser=not a.no_browser)

    try:
        cfg = load_config(a.config)
        env = dict(os.environ)
        if a.cmd == "kill":
            Journal(cfg.get("state_dir", "state")).set_kill(a.state == "on")
            print("kill switch:", a.state)
            return 0
        if a.cmd == "backtest":
            ds = DataSource(cfg, env)
            for sym in a.symbols or cfg["watchlist"]:
                res = backtest.run(ds.sessions(sym), cfg, use_timemachine=not a.no_timemachine)
                print("%-6s trades=%3d acerto=%5.1f%% R_medio=%+.3f R_total=%+.2f DD_max=%.2fR" % (
                    sym, res["trades"], 100 * res["win_rate"], res["avg_r"], res["total_r"], res["max_dd_r"]))
            return 0
        engine, mode = build(cfg, env)
        if mode == "live":
            print("!!! MODO LIVE: DINHEIRO REAL !!!", file=sys.stderr)
        if a.cmd == "run":
            lock = engine.journal.acquire_run_lock()                    # SEC-10: um só robô por pasta
            if lock is None:
                print("ERRO: já existe um robô rodando com esta pasta de estado (%s). "
                      "Desligue o outro antes." % engine.journal.dir, file=sys.stderr)
                return 3
            stop = threading.Event()
            if env.get(EXIT_WITH_PARENT) == "1":                       # ligado pelo painel
                threading.Thread(target=watch_parent, args=(sys.stdin, stop), daemon=True).start()
            try:
                engine.run_forever(stop=stop)
            finally:
                release_run_lock(lock)
            if stop.is_set():
                print("[%s] painel fechado: robô encerrado" % clock.now_utc().astimezone(clock.NY).strftime(
                    "%H:%M:%S"), flush=True)
        elif a.cmd == "tick":
            print(engine.tick())
        elif a.cmd == "flatten":
            engine.broker.flatten()
            print("zerado")
        elif a.cmd == "status":
            day = clock.now_utc().astimezone(clock.NY).date().isoformat()
            acct = engine.broker.account()
            print(json.dumps({"mode": mode, "equity": acct.get("equity"), "last_equity": acct.get("last_equity"),
                              "killed": engine.journal.killed(), "day": engine.journal.day_state(day)}, indent=2))
        return 0
    except ConfigError as e:
        print("ERRO de configuração:", e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
