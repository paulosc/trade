"""Corretora: Alpaca (REST) e PaperSim (offline). REQ-08, SEC-01..03."""

import hashlib
import json
import time
import urllib.error
import urllib.request

LIVE_CONFIRM_PHRASE = "I-ACCEPT-REAL-MONEY-RISK"
PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


class BrokerError(Exception):
    pass


class ConfigError(Exception):
    pass


def resolve_credentials(cfg, env):
    """Decide modo e chaves. Levanta ConfigError se o modo live não estiver 100% autorizado."""
    mode = cfg.get("mode", "paper")
    if mode == "live":
        if env.get("TRADEBOT_LIVE_CONFIRM") != LIVE_CONFIRM_PHRASE:
            raise ConfigError("modo live exige TRADEBOT_LIVE_CONFIRM=%s" % LIVE_CONFIRM_PHRASE)
        key, secret = env.get("ALPACA_LIVE_KEY_ID"), env.get("ALPACA_LIVE_SECRET_KEY")
        if not (key and secret):
            raise ConfigError("modo live exige ALPACA_LIVE_KEY_ID e ALPACA_LIVE_SECRET_KEY")
        return "live", LIVE_URL, key, secret
    if mode == "paper":
        key, secret = env.get("ALPACA_PAPER_KEY_ID"), env.get("ALPACA_PAPER_SECRET_KEY")
        if not (key and secret):
            raise ConfigError("modo paper exige ALPACA_PAPER_KEY_ID e ALPACA_PAPER_SECRET_KEY")
        return "paper", PAPER_URL, key, secret
    if mode == "sim":
        return "sim", None, None, None
    raise ConfigError("mode inválido: %r (use sim, paper ou live)" % mode)


def client_order_id(day, symbol, slot):
    raw = "%s|%s|%s" % (day, symbol, slot)
    return "tb-" + hashlib.sha1(raw.encode()).hexdigest()[:20]


def bracket_payload(symbol, qty, limit, stop, target, coid):
    return {
        "symbol": symbol, "qty": str(int(qty)), "side": "buy", "type": "limit",
        "limit_price": "%.2f" % limit, "time_in_force": "day", "order_class": "bracket",
        "take_profit": {"limit_price": "%.2f" % target},
        "stop_loss": {"stop_price": "%.2f" % stop},
        "client_order_id": coid,
    }


def _default_send(method, url, headers, body):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            txt = r.read()
            return r.status, (json.loads(txt) if txt else None)
    except urllib.error.HTTPError as e:
        txt = e.read()
        try:
            return e.code, json.loads(txt)
        except ValueError:
            return e.code, {"message": txt.decode(errors="replace")[:300]}


class Alpaca:
    def __init__(self, base_url, key, secret, send=_default_send, sleep=time.sleep):
        self.base, self.send, self.sleep = base_url, send, sleep
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                         "Content-Type": "application/json"}

    def __repr__(self):                       # SEC-03: nunca mostrar as chaves
        return "Alpaca(%s)" % self.base

    def _call(self, method, path, body=None, ok=(200, 204, 207)):
        status, doc = self.send(method, self.base + path, self._headers, body)
        if status not in ok:
            msg = (doc or {}).get("message", "") if isinstance(doc, dict) else ""
            raise BrokerError("%s %s -> HTTP %s %s" % (method, path, status, msg))
        return doc

    def account(self):
        return self._call("GET", "/v2/account")

    def clock(self):
        return self._call("GET", "/v2/clock")

    def positions(self):
        return self._call("GET", "/v2/positions") or []

    def submit_bracket(self, symbol, qty, limit, stop, target, coid):
        return self._call("POST", "/v2/orders", bracket_payload(symbol, qty, limit, stop, target, coid))

    def open_orders(self):
        """Ordens ainda abertas (só leitura; usado pelo radar do painel, REQ-17)."""
        return self._call("GET", "/v2/orders?status=open&limit=100") or []

    def flatten(self, tries=4, polls=12, pause=0.5):
        """Cancela tudo e fecha as posições, conferindo na corretora (REQ-09).

        A Alpaca não vende ações presas a um stop/alvo que ainda está sendo cancelado; por isso
        espera as ordens saírem antes de fechar, e confere que sobrou zero posição.
        """
        self._call("DELETE", "/v2/orders")
        self._wait(lambda: not self.open_orders(), polls, pause)
        for _ in range(tries):
            self._call("DELETE", "/v2/positions?cancel_orders=true")
            if self._wait(lambda: not self.positions(), polls, pause):
                return []
        left = ", ".join("%s %s" % (p.get("symbol"), p.get("qty")) for p in self.positions())
        if not left:
            return []
        raise BrokerError("zerar não confirmado: ainda há posições abertas (%s)" % left)

    def _wait(self, done, polls, pause):
        for i in range(polls):
            if done():
                return True
            if i < polls - 1:
                self.sleep(pause)
        return False


class PaperSim:
    """Corretora fictícia em memória para testes e para o modo `sim`."""

    def __init__(self, cash=5000.0, is_open=True):
        self.cash, self.is_open = cash, is_open
        self.orders, self.flattened, self.pos = [], 0, {}
        self.last_equity = cash

    def account(self):
        return {"equity": str(self.cash), "last_equity": str(self.last_equity),
                "buying_power": str(self.cash), "daytrade_count": 0, "trading_blocked": False}

    def clock(self):
        return {"is_open": self.is_open}

    def positions(self):
        return [{"symbol": s, "qty": str(q)} for s, q in self.pos.items()]

    def submit_bracket(self, symbol, qty, limit, stop, target, coid):
        if any(o["client_order_id"] == coid for o in self.orders):
            raise BrokerError("client_order_id duplicado")
        o = bracket_payload(symbol, qty, limit, stop, target, coid)
        self.orders.append(o)
        self.pos[symbol] = self.pos.get(symbol, 0) + int(qty)
        return {"id": "sim-%d" % len(self.orders), **o}

    def flatten(self):
        self.flattened += 1
        self.pos = {}
        return []
