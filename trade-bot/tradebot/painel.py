"""Painel local no navegador: passo a passo para ligar o robô em sim ou paper.

REQ-13..17, SEC-07..09. Só biblioteca padrão. Abre com `python -m tradebot painel`.
"""

import collections
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, clock, data, strategy
from .broker import PAPER_URL, Alpaca, BrokerError, _default_send
from .journal import Journal

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(PKG_DIR)            # trade-bot/
HTML_PATH = os.path.join(PKG_DIR, "painel.html")

PAPER_VARS = ("ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET_KEY")
LIVE_VARS = ("ALPACA_LIVE_KEY_ID", "ALPACA_LIVE_SECRET_KEY", "TRADEBOT_LIVE_CONFIRM")
PANEL_MODES = ("sim", "paper")                    # SEC-07: nunca "live"
SYMBOL_RE = re.compile(r"^[A-Z][A-Z.]{0,9}$")
MAX_SYMBOLS = 20
RADAR_TTL = 30                                    # REQ-17: segundos de cache dos dados do radar

STATUS_PT = {
    "closed": "Bolsa fechada. O robô está esperando o pregão abrir.",
    "outside-window": "Pregão aberto, mas fora da janela de entradas. O robô só observa.",
    "ok": "Verificou as ações da lista. Nenhuma entrada nova neste minuto.",
    "flat": "Passou do horário de zerar. Tudo zerado até amanhã.",
    "halted": "Parado hoje: o limite de perda do dia foi atingido.",
    "killed": "Kill switch ligado: tudo zerado e entradas bloqueadas.",
    "blocked": "A corretora bloqueou operações nesta conta.",
    "error": "Erro neste ciclo (rede ou dados). Por segurança, nada foi feito.",
}


class PainelError(Exception):
    """Erro com mensagem pronta para mostrar na tela."""


# ------------------------------------------------------------------ .env (SEC-09)

def read_env_file(path):
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_env_file(path, updates):
    """Atualiza só as chaves pedidas, preservando as outras linhas do arquivo."""
    lines, seen = [], set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                k = line.split("=", 1)[0].strip()
                if "=" in line and not line.lstrip().startswith("#") and k in updates:
                    lines.append("%s=%s\n" % (k, updates[k]))
                    seen.add(k)
                else:
                    lines.append(line if line.endswith("\n") else line + "\n")
    for k, v in updates.items():
        if k not in seen:
            lines.append("%s=%s\n" % (k, v))
    atomic_write(path, "".join(lines))


def atomic_write(path, text):
    """Grava via temporário de nome único na mesma pasta; apaga o temporário se algo falhar (SEC-09)."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)),
                               prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def mask(value):
    return "…" + value[-4:] if value and len(value) > 4 else ("…" if value else "")


# ------------------------------------------------------------------ config

def ensure_config(path):
    """REQ-13: cria config.json a partir do exemplo se ainda não existir."""
    if not os.path.exists(path):
        example = os.path.join(os.path.dirname(path), "config.example.json")
        if not os.path.exists(example):
            example = os.path.join(PROJECT_DIR, "config.example.json")
        shutil.copyfile(example, path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_watchlist(text):
    items = text if isinstance(text, list) else re.split(r"[\s,;]+", str(text or ""))
    syms = []
    for s in items:
        s = str(s).strip().upper()
        if not s:
            continue
        if not SYMBOL_RE.match(s):
            raise PainelError("Código inválido: %s. Use o código da ação nos EUA, por exemplo AAPL." % s)
        if s not in syms:
            syms.append(s)
    if not syms:
        raise PainelError("Coloque pelo menos uma ação na lista.")
    if len(syms) > MAX_SYMBOLS:
        raise PainelError("No máximo %d ações." % MAX_SYMBOLS)
    return syms


def save_setup(path, mode, watchlist):
    """Grava modo e watchlist. SEC-07: o painel nunca grava o modo live."""
    if mode not in PANEL_MODES:
        raise PainelError("O painel só liga os modos sim e paper. O modo live é só pela linha de comando.")
    cfg = ensure_config(path)
    cfg["mode"] = mode
    cfg["watchlist"] = parse_watchlist(watchlist)
    atomic_write(path, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    return cfg


# ------------------------------------------------------------------ chaves (REQ-14)

def check_paper_keys(key, secret, send=_default_send):
    """Testa as chaves na conta paper. Retorna o dict da conta ou levanta PainelError."""
    key, secret = (key or "").strip(), (secret or "").strip()
    if not key or not secret:
        raise PainelError("Cole as duas chaves: a Key ID e a Secret Key.")
    if key.startswith("AK"):                                         # SEC-07
        raise PainelError("Essa chave começa com AK, que é de conta real (live). "
                          "Gere as chaves na área Paper Trading da Alpaca (elas começam com PK).")
    try:
        acct = Alpaca(PAPER_URL, key, secret, send=send).account()
    except BrokerError as e:
        if "401" in str(e) or "403" in str(e):
            raise PainelError("A Alpaca recusou as chaves. Confira se copiou as duas inteiras "
                              "e se elas são da conta Paper.")
        raise PainelError("Não consegui falar com a Alpaca (%s). Confira a internet e tente de novo." % e)
    except OSError as e:
        raise PainelError("Sem conexão com a Alpaca (%s). Confira a internet e tente de novo." % e)
    return acct


# ------------------------------------------------------------------ journal (REQ-16)

def _money(v):
    try:
        return "US$ %s" % format(float(v), ",.2f").replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(v)


def _pct(v):
    try:
        return "%d%%" % round(float(v) * 100)
    except (TypeError, ValueError):
        return "?"


def describe_event(rec):
    """Traduz um registro do journal para uma frase simples. Retorna (tipo, texto)."""
    ev, sym = rec.get("event"), rec.get("symbol", "")
    if ev == "signal":
        return "sinal", "%s rompeu a máxima da faixa de abertura. Entrada %s, stop %s, alvo %s." % (
            sym, rec.get("entry"), rec.get("stop"), rec.get("target"))
    if ev == "timemachine":
        if rec.get("approved"):
            return "aprovado", "Máquina do Tempo aprovou %s: bate o alvo em %s dos cenários, o stop em %s." % (
                sym, _pct(rec.get("p_target")), _pct(rec.get("p_stop")))
        why = rec.get("reason") or "vantagem insuficiente"
        return "vetado", "Máquina do Tempo vetou %s: %s." % (sym, why)
    if ev == "blocked":
        return "bloqueado", "Regra de risco segurou %s: %s." % (sym, "; ".join(rec.get("reasons") or []))
    if ev == "order":
        return "ordem", "ORDEM enviada: compra de %s %s a até %s, stop %s, alvo %s." % (
            rec.get("qty"), sym, rec.get("entry"), rec.get("stop"), rec.get("target"))
    if ev == "flatten":
        return "zerar", "Zerou tudo (%s)." % rec.get("reason", "")
    if ev == "error":
        where = {"data": "nos dados", "broker": "na corretora", "submit": "ao enviar a ordem",
                 "flatten": "ao zerar", "tick": "no ciclo"}.get(rec.get("where"), "")
        return "erro", "Erro %s%s: %s" % (where, (" de " + sym) if sym else "", rec.get("error", ""))
    return "info", "%s %s" % (ev, sym)


def ny_hhmm(ts_iso):
    try:
        return datetime.fromisoformat(ts_iso).astimezone(clock.NY).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


# ------------------------------------------------------------------ radar (REQ-17)

def _hhmm(minutes):
    return "%02d:%02d" % divmod(int(minutes), 60)


def slot_end_hhmm(slot, bar_minutes):
    """Horário de NY em que a barra `slot` fecha (ex.: slot 2 de 5 min -> 09:45)."""
    return _hhmm(clock.OPEN_MIN + (slot + 1) * bar_minutes)


def latest_prices_alpaca(symbols, key, secret, feed="iex", get=data.http_get):
    """Último negócio de cada ação, numa consulta só. Só leitura."""
    q = urllib.parse.urlencode({"symbols": ",".join(symbols), "feed": feed})
    raw = get("https://data.alpaca.markets/v2/stocks/trades/latest?" + q,
              {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    trades = json.loads(raw).get("trades") or {}
    return {s: float(t["p"]) for s, t in trades.items() if t and t.get("p") is not None}


def _usd(v):
    return _money(v) if v is not None else "?"


def symbol_radar(sym, bars, price, now_ny, cfg, events=(), position=None, traded=False, pending=None):
    """O que o robô está vigiando em `sym` agora, com a mesma regra da estratégia (REQ-01..03).

    `bars`: barras de 5 min; só as fechadas contam para o sinal, como no robô.
    `price`: último negócio (ou None). `events`: registros do journal de hoje desta ação.
    """
    bm = cfg.get("bar_minutes", 5)
    n = cfg.get("opening_range_minutes", 15) // bm
    mins = clock.minute_of_day(now_ny)
    today = data.group_sessions(bars or [], bm).get(now_ny.date())
    out = {"symbol": sym, "state": "sem_dados", "price": price, "or_high": None, "or_low": None,
           "stop": None, "dist": None, "dist_pct": None, "closes": [], "text": ""}

    def done(state, text):
        out.update(state=state, text=text)
        return out

    if today is None:
        if now_ny.weekday() >= 5 or mins < clock.OPEN_MIN:
            return done("antes", "O pregão ainda não começou. A linha de gatilho sai às %s." % slot_end_hhmm(n - 1, bm))
        return done("sem_dados", "Sem cotações de hoje para esta ação. O robô também não vai operá-la.")

    closed = data.closed_only(today, now_ny, bm)
    last_closed = clock.last_closed_slot(now_ny, bm)
    got = [b for b in closed if b is not None]
    if price is None and got:
        price = got[-1]["c"]
    out["price"] = price
    out["closes"] = [None if b is None else b["c"] for b in closed[:last_closed + 1]]

    # faixa de abertura (ou o pedaço dela que já existe)
    rng = strategy.opening_range(closed, n) if last_closed >= n - 1 else None
    first = [b for b in closed[:n] if b is not None]
    if rng:
        oh, ol = rng
    elif first:
        oh, ol = max(b["h"] for b in first), min(b["l"] for b in first)
    else:
        oh = ol = None
    if oh is not None:
        oh, ol = round(oh, 2), round(ol, 2)                            # tela e frase com o mesmo centavo
        out.update(or_high=oh, or_low=ol, stop=strategy.stop_price(cfg.get("stop_rule", "or_mid"), oh, ol))
        if price:
            out.update(dist=round(oh - price, 2), dist_pct=round((oh - price) / price * 100, 2))

    slot = clock.slot_of(now_ny, bm)
    bar_close = slot_end_hhmm(slot, bm) if slot is not None else None

    if position:
        return done("posicao", "Comprado: %s ações a %s. Stop e alvo já estão na corretora." % (
            position.get("qty"), position.get("avg")))
    if pending:
        lim = pending.get("limit")
        why = (" O preço (%s) está acima do limite, e o robô não paga mais caro." % _usd(price)
               if price is not None and lim is not None and price > lim else "")
        return done("pendente", "Ordem de compra pendente: %s ações a até %s.%s Se o preço voltar, executa já com "
                    "stop e alvo; se não, é cancelada às %s." % (pending.get("qty"), _usd(lim), why, cfg["flat_by"]))
    for kind, text in reversed([describe_event(r) for r in events]):
        if kind == "ordem":
            return done("operou", "Já operou hoje. " + text)
        if kind in ("vetado", "bloqueado"):
            return done(kind, text + " Hoje o robô não entra mais nesta ação.")
    if traded:
        return done("operou", "Já operou esta ação hoje. Não entra de novo.")
    if mins >= clock.CLOSE_MIN:
        return done("fora", "Pregão encerrado.")
    if last_closed < n - 1:
        if oh is None:
            return done("formando", "Marcando a faixa de abertura até %s." % slot_end_hhmm(n - 1, bm))
        return done("formando", "Marcando a faixa de abertura até %s. Até agora: máxima %s, mínima %s." % (
            slot_end_hhmm(n - 1, bm), _usd(oh), _usd(ol)))
    if rng is None:
        return done("sem_dados", "Faltou cotação nos primeiros minutos: sem faixa de abertura, o robô não opera esta ação hoje.")

    sig = strategy.orb_signal(closed, last_closed, cfg) if last_closed >= n else {"status": "none"}
    if sig["status"] == "triggered":
        return done("sinal", "ROMPEU! A barra das %s fechou em %s, acima do gatilho %s. O robô decide neste minuto "
                    "(a Máquina do Tempo e as regras de risco ainda podem vetar)." % (
                        slot_end_hhmm(sig["slot"], bm), _usd(sig["entry"]), _usd(oh)))
    if sig["status"] == "expired":
        return done("perdeu", "Rompeu na barra das %s, mas o sinal só vale na hora. Hoje o robô não entra mais nesta ação."
                    % slot_end_hhmm(sig["slot"], bm))
    if sig["status"] == "invalid":
        return done("sem_dados", "Sinal inválido hoje (%s)." % sig.get("reason", ""))
    if mins > clock.hhmm_to_min(cfg["no_entries_after"]):
        return done("fora", "A janela de entradas acabou às %s. Hoje não entra mais." % cfg["no_entries_after"])
    if price is not None and price > oh:
        return done("acima", "Acima do gatilho agora! Se a barra fechar acima de %s às %s, o robô tenta comprar." % (
            _usd(oh), bar_close))
    if not price:
        return done("vigiando", "Vigiando o gatilho em %s." % _usd(oh))
    return done("vigiando", "Falta %s (%s%%) para o gatilho em %s." % (
        _usd(oh - price), format(out["dist_pct"], ".2f").replace(".", ","), _usd(oh)))


# ------------------------------------------------------------------ processo do robô (REQ-15)

def robot_env(base_env, env_file, mode):
    """Ambiente do processo do robô. SEC-07: tira tudo que habilitaria o modo live."""
    env = {k: v for k, v in base_env.items() if k not in LIVE_VARS}
    if mode == "paper":
        env.update({k: v for k, v in env_file.items() if k in PAPER_VARS})
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = PROJECT_DIR + os.pathsep + env.get("PYTHONPATH", "")
    env["TRADEBOT_EXIT_WITH_PARENT"] = "1"                              # SEC-10: morre junto com o painel
    return env


class Runner:
    def __init__(self):
        self.proc, self.started_at = None, None
        self.lines = collections.deque(maxlen=200)
        self.lock = threading.Lock()

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, cmd, cwd, env):
        with self.lock:
            if self.running:
                return
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            # SEC-10: a entrada do robô fica ligada ao painel; se o painel fechar, ela termina e o robô sai.
            self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                         errors="replace", creationflags=flags)
            self.started_at = clock.now_utc().isoformat(timespec="seconds")
            self.lines.clear()
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        with proc.stdout:
            for line in proc.stdout:
                self.lines.append(line.rstrip())
        if proc.stdin and not proc.stdin.closed:                        # o robô saiu sozinho
            try:
                proc.stdin.close()
            except OSError:
                pass

    def stop(self, timeout=10):
        with self.lock:
            p = self.proc
            if p is None or p.poll() is not None:
                return
            try:                                                        # SEC-10: pede para sair com calma
                if p.stdin:
                    p.stdin.close()
                p.wait(timeout)
                return
            except (OSError, subprocess.TimeoutExpired):
                pass
            p.terminate()
            try:
                p.wait(timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout)

    def last_status(self):
        for line in reversed(self.lines):
            m = re.match(r"^\[(\d\d:\d\d:\d\d)\] (\S+)$", line)
            if m:
                return m.group(1), m.group(2)
        return None, None


# ------------------------------------------------------------------ o painel

class Painel:
    def __init__(self, config_path, send=_default_send, base_env=None, robot_cmd=None):
        self.config_path = os.path.abspath(config_path)
        self.dir = os.path.dirname(self.config_path)
        self.env_path = os.path.join(self.dir, ".env")
        self.send = send
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.robot_cmd = robot_cmd or [sys.executable, "-m", "tradebot", "--config", self.config_path, "run"]
        self.token = secrets.token_urlsafe(24)                         # SEC-08
        self.runner = Runner()
        self.fetch_bars = self._fetch_bars                              # trocáveis nos testes
        self.fetch_prices = self._fetch_prices
        self._radar, self._radar_at, self._radar_lock = None, 0.0, threading.Lock()
        ensure_config(self.config_path)

    # -- helpers
    def cfg(self):
        return ensure_config(self.config_path)

    def keys(self):
        f = read_env_file(self.env_path)
        return f.get(PAPER_VARS[0], ""), f.get(PAPER_VARS[1], "")

    def journal(self, cfg):
        return Journal(os.path.join(self.dir, cfg.get("state_dir", "state")))

    def paper_broker(self):
        key, secret = self.keys()
        if not (key and secret):
            raise PainelError("Salve as chaves paper primeiro (passo 2).")
        return Alpaca(PAPER_URL, key, secret, send=self.send)

    # -- ações
    def save_keys(self, key, secret):
        acct = check_paper_keys(key, secret, send=self.send)
        write_env_file(self.env_path, {PAPER_VARS[0]: key.strip(), PAPER_VARS[1]: secret.strip()})
        return {"ok": True, "equity": _money(acct.get("equity")), "key_hint": mask(key.strip())}

    def save_setup(self, mode, watchlist):
        if mode == "paper" and not all(self.keys()):
            raise PainelError("Para o modo paper, salve as chaves da Alpaca primeiro.")
        cfg = save_setup(self.config_path, mode, watchlist)
        return {"ok": True, "mode": cfg["mode"], "watchlist": cfg["watchlist"]}

    def start(self):
        cfg = self.cfg()
        mode = cfg.get("mode", "paper")
        if mode not in PANEL_MODES:                                     # SEC-07
            raise PainelError("O config.json está em modo %r. O painel só liga sim ou paper; "
                              "o modo live é só pela linha de comando." % mode)
        if mode == "paper" and not all(self.keys()):
            raise PainelError("Salve as chaves paper da Alpaca antes de ligar (passo 2).")
        if self.journal(cfg).killed():
            raise PainelError("O kill switch está ligado. Desligue-o antes de ligar o robô.")
        if not self.runner.running and self.journal(cfg).run_lock_held():    # SEC-10
            raise PainelError("Já existe um robô rodando nesta pasta (talvez de um painel anterior ou do terminal). "
                              "Feche-o antes de ligar outro.")
        env = robot_env(self.base_env, read_env_file(self.env_path), mode)
        self.runner.start(self.robot_cmd, self.dir, env)
        return {"ok": True}

    def stop(self):
        self.runner.stop()
        return {"ok": True}

    def flatten(self):
        if self.cfg().get("mode") != "paper":
            raise PainelError("Zerar agora só existe no modo paper. No sim, use o kill switch.")
        try:
            self.paper_broker().flatten()
        except BrokerError as e:
            raise PainelError("A Alpaca não confirmou: %s" % e)
        return {"ok": True}

    def kill(self, on):
        if not isinstance(on, bool):                                    # REQ-15: só true/false
            raise PainelError("Pedido inválido para o kill switch: use on = true ou false.")
        cfg = self.cfg()
        self.journal(cfg).set_kill(on)
        if on and cfg.get("mode") == "paper" and all(self.keys()):
            try:
                self.paper_broker().flatten()
            except BrokerError:
                pass                                                    # o robô tenta de novo no próximo ciclo
        return {"ok": True}

    # -- radar (REQ-17): só leitura, nunca envia ordem
    def _fetch_bars(self, sym, cfg):
        bm = cfg.get("bar_minutes", 5)
        key, secret = self.keys()
        if cfg.get("mode") == "paper" and key and secret and cfg.get("data_source", "alpaca") == "alpaca":
            return data.fetch_alpaca(sym, key, secret, days=1, bar_minutes=bm, feed=cfg.get("alpaca_feed", "iex"))
        return data.fetch_yahoo(sym, days=1, bar_minutes=bm)

    def _fetch_prices(self, syms, cfg):
        key, secret = self.keys()
        if cfg.get("mode") == "paper" and key and secret:
            return latest_prices_alpaca(syms, key, secret, feed=cfg.get("alpaca_feed", "iex"))
        return {}                                                       # sim: usa o fechamento da última barra

    def radar(self, now=None):
        now = now or clock.now_utc()
        with self._radar_lock:
            age = now.timestamp() - self._radar_at
            closes_in = (self._radar or {}).get("bar_close_in")
            bar_closed = closes_in is not None and closes_in - age <= 0     # barra nova: busca de novo
            if self._radar is not None and 0 <= age < RADAR_TTL and not bar_closed:
                out = dict(self._radar)
                if out["bar_close_in"] is not None:                     # a contagem anda mesmo com cache
                    out["bar_close_in"] = max(0, int(out["bar_close_in"] - age))
            else:
                self._radar = out = self._build_radar(now)
                self._radar_at = now.timestamp()
        out = dict(out)
        if not self.runner.running:                                     # na hora, fora do cache
            out["notes"] = out["notes"] + ["O robô está desligado: o radar mostra, mas ninguém opera. Ligue no passo 4."]
        return out

    def _build_radar(self, now):
        cfg = self.cfg()
        bm = cfg.get("bar_minutes", 5)
        now_ny = now.astimezone(clock.NY)
        day = now_ny.date().isoformat()
        jr = self.journal(cfg)
        st = jr.day_state(day)
        recs = jr.read(day)
        syms = cfg.get("watchlist", [])

        positions, pending, notes = {}, {}, []
        key, secret = self.keys()
        if cfg.get("mode") == "paper" and key and secret:
            try:
                br = self.paper_broker()
                for p in br.positions():
                    positions[p.get("symbol")] = {"qty": p.get("qty"), "avg": _money(p.get("avg_entry_price"))}
                for o in br.open_orders():
                    if o.get("side") == "buy" and o.get("symbol") not in pending:
                        lim = o.get("limit_price")
                        pending[o.get("symbol")] = {"qty": o.get("qty"),
                                                    "limit": float(lim) if lim is not None else None}
            except (BrokerError, OSError, ValueError) as e:
                notes.append("Não consegui ler posições e ordens: %s" % e)
        try:
            prices = self.fetch_prices(syms, cfg) or {}
        except Exception as e:                                          # sem preço ao vivo, segue com as barras
            prices = {}
            notes.append("Sem último preço ao vivo (%s); usando o fechamento da última barra." % e)

        items = []
        for sym in syms:
            evs = [r for r in recs if r.get("symbol") == sym]
            try:
                bars = self.fetch_bars(sym, cfg)
            except Exception as e:
                items.append({"symbol": sym, "state": "sem_dados", "price": prices.get(sym), "closes": [],
                              "or_high": None, "or_low": None, "stop": None, "dist": None, "dist_pct": None,
                              "text": "Não consegui buscar as cotações (%s)." % e})
                continue
            items.append(symbol_radar(sym, bars, prices.get(sym), now_ny, cfg, events=evs,
                                      position=positions.get(sym), traded=sym in st.get("traded", []),
                                      pending=pending.get(sym)))

        mins = clock.minute_of_day(now_ny)
        slot = clock.slot_of(now_ny, bm)
        bar_close_at = bar_close_in = None
        if slot is not None and now_ny.weekday() < 5:
            end = clock.OPEN_MIN + (slot + 1) * bm
            bar_close_at = _hhmm(end)
            bar_close_in = max(0, (end - mins) * 60 - now_ny.second)
        if st.get("trades", 0) >= cfg.get("max_trades_per_day", 0):
            notes.append("Já fez as %d operações permitidas hoje: não abre mais nada." % cfg.get("max_trades_per_day", 0))
        if len(positions) >= cfg.get("max_open_positions", 99):
            notes.append("Já está com o máximo de posições abertas: só entra de novo quando alguma fechar.")
        if st.get("halted"):
            notes.append("Parado hoje: o limite de perda do dia foi atingido.")
        if jr.killed():
            notes.append("Kill switch ligado: nenhuma entrada.")
        return {"updated": now_ny.strftime("%H:%M:%S"), "bar_minutes": bm,
                "bar_close_at": bar_close_at, "bar_close_in": bar_close_in,
                "entries_from": cfg["no_entries_before"], "entries_to": cfg["no_entries_after"],
                "trades": st.get("trades", 0), "max_trades": cfg.get("max_trades_per_day"),
                "positions": len(positions), "max_positions": cfg.get("max_open_positions"),
                "pending": len(pending),
                "notes": notes, "items": items}

    # -- leitura
    def status(self, now=None):
        now = now or clock.now_utc()
        cfg = self.cfg()
        mode = cfg.get("mode", "paper")
        now_ny = now.astimezone(clock.NY)
        day = now_ny.date().isoformat()
        key, secret = self.keys()
        jr = self.journal(cfg)

        def at(hhmm):
            h, m = hhmm.split(":")
            return now_ny.replace(hour=int(h), minute=int(m), second=0, microsecond=0).isoformat()

        mins = clock.minute_of_day(now_ny)
        market = {"ny_time": now_ny.strftime("%H:%M"), "weekday": now_ny.weekday(),
                  "is_open": now_ny.weekday() < 5 and clock.OPEN_MIN <= mins < clock.CLOSE_MIN,
                  "source": "horário", "open_at": at("09:30"), "close_at": at("16:00"),
                  "entries_from": at(cfg["no_entries_before"]), "entries_to": at(cfg["no_entries_after"]),
                  "flat_by": at(cfg["flat_by"])}

        account, account_error = None, None
        if mode == "paper" and key and secret:
            try:
                br = self.paper_broker()
                ck = br.clock()
                market.update(is_open=bool(ck.get("is_open")), source="alpaca", next_open=ck.get("next_open"))
                a = br.account()
                eq, last = float(a.get("equity", 0)), float(a.get("last_equity", a.get("equity", 0)))
                account = {"equity": _money(eq), "day_pnl": _money(eq - last), "day_pnl_raw": eq - last,
                           "buying_power": _money(a.get("buying_power")),
                           "positions": [{"symbol": p.get("symbol"), "qty": p.get("qty"),
                                          "avg": _money(p.get("avg_entry_price")),
                                          "pnl": _money(p.get("unrealized_pl")),
                                          "pnl_raw": float(p.get("unrealized_pl") or 0)}
                                         for p in br.positions()]}
            except (BrokerError, OSError, ValueError) as e:
                account_error = str(e)

        events = []
        for rec in jr.read(day)[-150:]:
            kind, text = describe_event(rec)
            events.append({"time": ny_hhmm(rec.get("ts")), "kind": kind, "text": text})
        t, st = self.runner.last_status()
        st_pt = STATUS_PT.get(st, "")
        if st == "outside-window" and not market["is_open"]:      # no sim a corretora fictícia está sempre "aberta"
            st_pt = STATUS_PT["closed"]
        return {
            "version": __version__,
            "python": "%d.%d.%d" % sys.version_info[:3],
            "mode": mode,
            "watchlist": cfg.get("watchlist", []),
            "limits": {k: cfg.get(k) for k in ("max_loss_per_trade_usd", "max_daily_loss_usd", "max_trades_per_day",
                                               "max_open_positions", "max_order_usd")},
            "keys": {"saved": bool(key and secret), "key_hint": mask(key)},       # SEC-09: nunca a chave
            "market": market,
            "account": account, "account_error": account_error,
            "robot": {"running": self.runner.running, "started_at": self.runner.started_at,
                      "last_time": t, "last_status": st, "last_status_pt": st_pt,
                      "lines": list(self.runner.lines)[-40:]},
            "killed": jr.killed(),
            "events": events[::-1],
        }


# ------------------------------------------------------------------ HTTP (SEC-08)

def make_handler(painel, port):
    allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}

    class Handler(BaseHTTPRequestHandler):
        server_version = "tradebot-painel"

        def log_message(self, fmt, *args):      # silencioso: nada de chaves no terminal
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, api):
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(403, {"error": "host não permitido"})
                return False
            if api and not secrets.compare_digest(self.headers.get("X-Painel-Token", ""), painel.token):
                self._send(403, {"error": "token inválido. Recarregue a página."})
                return False
            return True

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                if not self._guard(False):
                    return
                with open(HTML_PATH, encoding="utf-8") as f:
                    page = f.read().replace("__PAINEL_TOKEN__", painel.token)
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/status":
                if self._guard(True):
                    self._send(200, painel.status())
            elif path == "/api/radar":                                   # REQ-17
                if self._guard(True):
                    self._send(200, painel.radar())
            else:
                self._send(404, {"error": "não encontrado"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n > 64 * 1024:
                self.close_connection = True
                return self._send(413, {"error": "pedido grande demais"})
            raw = self.rfile.read(n) if n > 0 else b""    # lê antes de recusar: senão o Windows derruba a conexão
            if not self._guard(True):
                return
            try:
                body = json.loads(raw or b"{}")
                if path == "/api/keys":
                    res = painel.save_keys(body.get("key", ""), body.get("secret", ""))
                elif path == "/api/setup":
                    res = painel.save_setup(body.get("mode"), body.get("watchlist", ""))
                elif path == "/api/start":
                    res = painel.start()
                elif path == "/api/stop":
                    res = painel.stop()
                elif path == "/api/flatten":
                    res = painel.flatten()
                elif path == "/api/kill":
                    res = painel.kill(body.get("on"))
                else:
                    return self._send(404, {"error": "não encontrado"})
                self._send(200, res)
            except PainelError as e:
                self._send(400, {"error": str(e)})
            except (ValueError, KeyError) as e:
                self._send(400, {"error": "pedido inválido: %s" % e})

    return Handler


def make_server(painel, port=8765):
    srv = ThreadingHTTPServer(("127.0.0.1", port), None)
    srv.RequestHandlerClass = make_handler(painel, srv.server_address[1])
    return srv


def serve(config_path, port=8765, open_browser=True):
    painel = Painel(config_path)
    try:
        srv = make_server(painel, port)
    except OSError:
        print("A porta %d já está em uso. O painel talvez já esteja aberto: http://127.0.0.1:%d" % (port, port))
        return 1
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    print("Painel do trade-bot v%s aberto em %s" % (__version__, url))
    print("Deixe esta janela aberta. Para fechar o painel (e desligar o robô), aperte Ctrl+C.")
    if open_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        painel.runner.stop()
        srv.server_close()
    return 0
