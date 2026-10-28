"""Diário em JSONL e estado do dia. REQ-10."""

import json
import os
from datetime import datetime, timezone

if os.name == "nt":
    import msvcrt

    def _lock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def release_run_lock(f):
    """Solta a trava do robô (SEC-10)."""
    try:
        _unlock(f)
    except OSError:
        pass
    f.close()


class Journal:
    def __init__(self, state_dir):
        self.dir = state_dir
        os.makedirs(state_dir, exist_ok=True)

    def path(self, day):
        return os.path.join(self.dir, "journal-%s.jsonl" % day)

    def log(self, day, event, **fields):
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
        with open(self.path(day), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return rec

    def read(self, day):
        p = self.path(day)
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    # ---- estado do dia
    def _state_path(self, day):
        return os.path.join(self.dir, "day-%s.json" % day)

    def day_state(self, day):
        p = self._state_path(day)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        return {"traded": [], "trades": 0, "halted": False, "flattened": False}

    def save_day_state(self, day, st):
        tmp = self._state_path(day) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2)
        os.replace(tmp, self._state_path(day))

    # ---- um só robô por pasta (SEC-10)
    @property
    def lock_path(self):
        return os.path.join(self.dir, "robot.lock")

    def acquire_run_lock(self):
        """Trava exclusiva enquanto o robô roda. Devolve o arquivo (guarde-o) ou None se já houver outro robô.

        O sistema operacional solta a trava quando o processo termina, mesmo se ele morrer de repente.
        """
        f = open(self.lock_path, "a+")
        try:
            _lock(f)
        except OSError:
            f.close()
            return None
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        return f

    def run_lock_held(self):
        """True se outro processo está com a trava do robô."""
        f = self.acquire_run_lock()
        if f is None:
            return True
        release_run_lock(f)
        return False

    # ---- kill switch (SEC-05)
    @property
    def kill_path(self):
        return os.path.join(self.dir, "KILL")

    def killed(self):
        return os.path.exists(self.kill_path)

    def set_kill(self, on):
        if on:
            open(self.kill_path, "w").close()
        elif self.killed():
            os.remove(self.kill_path)
