"""Um ciclo (tick) do robô: dados -> sinal -> Máquina do Tempo -> tamanho -> risco -> ordem.

REQ-05, REQ-07, REQ-09, REQ-10, SEC-05, SEC-06.
"""

import time

from . import clock, risk, sizing, strategy, timemachine
from .broker import BrokerError, client_order_id
from .data import closed_only


class Engine:
    def __init__(self, cfg, broker, data_source, journal, mode):
        self.cfg, self.broker, self.data, self.journal, self.mode = cfg, broker, data_source, journal, mode

    def _flatten(self, day, why):
        """True só quando a corretora confirmou zero posições (REQ-09)."""
        try:
            self.broker.flatten()
            self.journal.log(day, "flatten", reason=why)
            return True
        except BrokerError as e:
            self.journal.log(day, "error", where="flatten", error=str(e))
            return False

    def tick(self, now=None):
        now = now or clock.now_utc()
        now_ny = now.astimezone(clock.NY)
        day = now_ny.date().isoformat()
        cfg, bm = self.cfg, self.cfg.get("bar_minutes", 5)
        st = self.journal.day_state(day)

        try:
            if self.journal.killed():                                   # SEC-05
                self._flatten(day, "kill switch")                        # cancela ordens pendentes também
                return "killed"
            if not self.broker.clock().get("is_open"):
                return "closed"
            m = clock.minute_of_day(now_ny)
            if m >= clock.hhmm_to_min(cfg["flat_by"]):                  # REQ-09
                if not st["flattened"]:
                    if not self._flatten(day, "flat_by %s" % cfg["flat_by"]):
                        return "error"                                  # tenta de novo no próximo ciclo
                    st["flattened"] = True
                    self.journal.save_day_state(day, st)
                return "flat"
            acct = self.broker.account()
            equity = float(acct["equity"])
            day_pnl = equity - float(acct.get("last_equity", equity))
            if acct.get("trading_blocked"):
                return "blocked"
            if day_pnl <= -cfg["max_daily_loss_usd"] and not st["halted"]:
                st["halted"] = True
                self.journal.save_day_state(day, st)
            if st["halted"]:
                if not st.get("halt_flattened"):                       # repete até a corretora confirmar
                    if self._flatten(day, "perda diária %.2f" % day_pnl):
                        st["halt_flattened"] = True
                        self.journal.save_day_state(day, st)
                return "halted"
            if m < clock.hhmm_to_min(cfg["no_entries_before"]) or m > clock.hhmm_to_min(cfg["no_entries_after"]):
                return "outside-window"
            positions = self.broker.positions()
        except BrokerError as e:                                        # SEC-06
            self.journal.log(day, "error", where="broker", error=str(e))
            return "error"

        held = {p["symbol"] for p in positions}
        buying_power = float(acct.get("buying_power", 0))
        last_slot = clock.slot_ending_at(cfg["flat_by"], bm)
        lookback = cfg.get("timemachine", {}).get("lookback_sessions", 59)

        for sym in cfg["watchlist"]:
            if sym in st["traded"] or sym in held:
                continue
            if st["trades"] >= cfg["max_trades_per_day"]:
                break
            try:
                sessions = self.data.sessions(sym, now=now)
            except Exception as e:                                      # SEC-06
                self.journal.log(day, "error", where="data", symbol=sym, error=str(e))
                continue
            today = sessions.get(now_ny.date())
            if today is None:
                continue
            past = [s for d, s in sessions.items() if d < now_ny.date()][-lookback:]
            last_closed = clock.last_closed_slot(now_ny, bm)
            sig = strategy.orb_signal(closed_only(today, now_ny, bm), last_closed, cfg)
            if sig["status"] != "triggered":
                continue
            self.journal.log(day, "signal", symbol=sym, **sig)

            tm_cfg = cfg.get("timemachine", {})
            if tm_cfg.get("enabled", True):
                ok, rep = timemachine.evaluate(past, sig, last_slot, tm_cfg)
                self.journal.log(day, "timemachine", symbol=sym, approved=ok, **rep)
                if not ok:
                    continue

            # tolerância de preço: % do preço, mas nunca mais que 20% do risco (REQ-12)
            slip = min(sig["entry"] * cfg.get("max_slippage_pct", 0.1) / 100.0, 0.2 * (sig["entry"] - sig["stop"]))
            limit = round(sig["entry"] + slip, 2)
            # alvo recalculado a partir do preço limite para manter o R:R configurado
            target = round(limit + cfg.get("target_r", 1.0) * (limit - sig["stop"]), 2)
            qty = sizing.position_size(limit, sig["stop"], buying_power, cfg)
            order = {"symbol": sym, "qty": qty, "entry": limit, "stop": sig["stop"], "target": target}
            ctx = {"now_ny": now_ny, "day_pnl": day_pnl, "trades_today": st["trades"],
                   "open_positions": len(held), "traded_symbols": st["traded"], "held_symbols": held,
                   "equity": equity, "daytrade_count": int(acct.get("daytrade_count", 0)),
                   "mode": self.mode, "killed": self.journal.killed(), "halted": st["halted"]}
            reasons = risk.check_entry(order, ctx, cfg)
            if reasons:
                self.journal.log(day, "blocked", symbol=sym, reasons=reasons, order=order)
                continue
            coid = client_order_id(day, sym, sig["slot"])
            try:
                res = self.broker.submit_bracket(sym, qty, limit, sig["stop"], target, coid)
            except BrokerError as e:
                self.journal.log(day, "error", where="submit", symbol=sym, error=str(e))
                continue
            st["traded"].append(sym)
            st["trades"] += 1
            self.journal.save_day_state(day, st)
            held.add(sym)
            buying_power -= qty * limit
            self.journal.log(day, "order", mode=self.mode, client_order_id=coid,
                             broker_id=(res or {}).get("id"), **order)
        return "ok"

    def run_forever(self, sleep=time.sleep, max_ticks=None, stop=None):
        """Roda até `max_ticks` ou até `stop` (threading.Event) ser acionado. SEC-10."""
        if stop is not None:
            sleep = stop.wait                                           # acorda na hora se pedirem para parar
        n = 0
        while (max_ticks is None or n < max_ticks) and not (stop is not None and stop.is_set()):
            try:
                status = self.tick()
            except Exception as e:                                      # nunca morrer em silêncio
                status = "error"
                self.journal.log(clock.now_utc().astimezone(clock.NY).date().isoformat(),
                                 "error", where="tick", error=repr(e))
            print("[%s] %s" % (clock.now_utc().astimezone(clock.NY).strftime("%H:%M:%S"), status), flush=True)
            n += 1
            sleep(self.cfg.get("poll_seconds", 60))
