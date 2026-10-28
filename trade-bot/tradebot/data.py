"""Barras de preço: Alpaca Market Data (padrão) ou Yahoo Finance (sem chave).

Uma barra é um dict {"t": epoch UTC do início, "o", "h", "l", "c", "v"}.
Uma sessão é uma lista de tamanho fixo (slots do pregão) com a barra ou None.
"""

import json
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timedelta

from . import clock

USER_AGENT = "trade-bot/0.1 (+https://github.com/)"


class DataError(Exception):
    pass


def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------- parsers

def parse_yahoo(raw):
    doc = json.loads(raw)
    try:
        res = doc["chart"]["result"][0]
        ts = res.get("timestamp") or []
        q = res["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError) as e:
        raise DataError("resposta do Yahoo sem dados: %s" % e)
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        bars.append({"t": int(t), "o": float(o), "h": float(h), "l": float(l), "c": float(c),
                     "v": int(q["volume"][i] or 0)})
    return bars


def _iso_to_epoch(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def parse_alpaca(raw):
    doc = json.loads(raw)
    out = []
    for b in doc.get("bars") or []:
        out.append({"t": _iso_to_epoch(b["t"]), "o": float(b["o"]), "h": float(b["h"]),
                    "l": float(b["l"]), "c": float(b["c"]), "v": int(b.get("v", 0))})
    return out, doc.get("next_page_token")


# ---------------------------------------------------------------- fetchers

def fetch_yahoo(symbol, days=60, bar_minutes=5, get=http_get):
    q = urllib.parse.urlencode({"range": "%dd" % days, "interval": "%dm" % bar_minutes,
                                "includePrePost": "false"})
    url = "https://query2.finance.yahoo.com/v8/finance/chart/%s?%s" % (urllib.parse.quote(symbol), q)
    return parse_yahoo(get(url))


def fetch_alpaca(symbol, key_id, secret, days=60, bar_minutes=5, feed="iex", get=http_get, now=None):
    now = now or clock.now_utc()
    start = (now - timedelta(days=int(days * 1.5) + 2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
    bars, token = [], None
    for _ in range(50):
        params = {"timeframe": "%dMin" % bar_minutes, "start": start, "limit": 10000,
                  "feed": feed, "adjustment": "raw"}
        if token:
            params["page_token"] = token
        url = "https://data.alpaca.markets/v2/stocks/%s/bars?%s" % (
            urllib.parse.quote(symbol), urllib.parse.urlencode(params))
        page, token = parse_alpaca(get(url, headers))
        bars.extend(page)
        if not token:
            break
    return bars


# ---------------------------------------------------------------- sessions

def group_sessions(bars, bar_minutes=5):
    """OrderedDict data_NY -> lista de slots (barra ou None), em ordem de data."""
    n = clock.session_slots(bar_minutes)
    days = OrderedDict()
    for b in sorted(bars, key=lambda x: x["t"]):
        dt = clock.to_ny(b["t"])
        s = clock.slot_of(dt, bar_minutes)
        if s is None:
            continue
        days.setdefault(dt.date(), [None] * n)[s] = b
    return days


def is_full(session):
    return all(b is not None for b in session)


class DataSource:
    """Escolhe Alpaca (se houver chaves) ou Yahoo e devolve sessões agrupadas."""

    def __init__(self, cfg, env, get=http_get):
        self.cfg, self.get = cfg, get
        self.kind = cfg.get("data_source", "alpaca")
        self.key = env.get("ALPACA_DATA_KEY_ID") or env.get("ALPACA_PAPER_KEY_ID") or env.get("ALPACA_LIVE_KEY_ID")
        self.secret = (env.get("ALPACA_DATA_SECRET_KEY") or env.get("ALPACA_PAPER_SECRET_KEY")
                       or env.get("ALPACA_LIVE_SECRET_KEY"))
        if self.kind == "alpaca" and not (self.key and self.secret):
            self.kind = "yahoo"
        self._hist = {}                     # REQ-18: símbolo -> (data NY, sessões anteriores)

    def _fetch(self, symbol, days, now):
        bm = self.cfg.get("bar_minutes", 5)
        if self.kind == "alpaca":
            return fetch_alpaca(symbol, self.key, self.secret, days=days, bar_minutes=bm,
                                feed=self.cfg.get("alpaca_feed", "iex"), get=self.get, now=now)
        return fetch_yahoo(symbol, days=min(days, 60), bar_minutes=bm, get=self.get)

    def sessions(self, symbol, now=None):
        """Sessões anteriores (baixadas 1x por dia) + a sessão de hoje (buscada a cada chamada). REQ-18."""
        now = now or clock.now_utc()
        bm = self.cfg.get("bar_minutes", 5)
        today = clock.ny_date(now)
        cached = self._hist.get(symbol)
        if cached is None or cached[0] != today:
            days = self.cfg.get("timemachine", {}).get("lookback_sessions", 59) + 1
            bars = self._fetch(symbol, days, now)
            if not bars:
                raise DataError("sem barras para %s" % symbol)
            grouped = group_sessions(bars, bm)
            past = OrderedDict((d, s) for d, s in grouped.items() if d < today)
            self._hist[symbol] = (today, past)
            return grouped
        out = OrderedDict(cached[1])
        recent = group_sessions(self._fetch(symbol, 1, now), bm)   # só a janela curta
        if today in recent:
            out[today] = recent[today]
        return out


def closed_only(session, now_ny, bar_minutes):
    """Copia a sessão de hoje mantendo só as barras já fechadas."""
    last = clock.last_closed_slot(now_ny, bar_minutes)
    return [b if i <= last else None for i, b in enumerate(session)]
