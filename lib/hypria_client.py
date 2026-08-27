"""Cliente do gateway do Hypria (tui_gateway) por stdio.

Fala JSON-RPC delimitado por newline com ``python -m tui_gateway.entry``,
o mesmo protocolo que o TUI Ink e o desktop Electron do Hypria usam.
Puro stdlib e zero GTK: quem marshala para o main loop e o chamador.

Modelo de threads:
  - thread leitora (``hypria-reader``): stdout do gateway -> resolve Futures
    (frames com ``id``) ou roteia eventos para a fila do turno aberto /
    handler de background.
  - thread de stderr: espelha com prefixo ``[hypria]`` e guarda as ultimas
    linhas para diagnostico (``last_stderr``).
  - escritas no stdin serializadas por lock; qualquer thread pode chamar
    ``request``/``request_async``.

O gateway responde fora de ordem (``_LONG_HANDLERS`` no server) — por isso
toda correlacao e por ``id``, nunca por ordem de chegada.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import Future


class HypriaError(Exception):
    """Falha generica do backend Hypria."""


class HypriaRpcError(HypriaError):
    """Frame de erro JSON-RPC devolvido pelo gateway."""

    def __init__(self, code, message, data=None):
        super().__init__(message or ("erro %s" % code))
        self.code = code
        self.data = data


class HypriaTimeout(HypriaError):
    """Request sem resposta dentro do prazo."""


class HypriaGatewayDead(HypriaError):
    """O processo do gateway morreu com requests pendentes."""


# Timeouts por metodo (segundos).  ``model.options``/``session.resume`` sao
# _LONG_HANDLERS no gateway e podem segurar a resposta por bastante tempo.
_TIMEOUTS = {
    "ping": 5.0,
    "session.create": 15.0,
    "prompt.submit": 30.0,
    "session.interrupt": 10.0,
    "approval.respond": 10.0,
    "clarify.respond": 10.0,
    "sudo.respond": 10.0,
    "secret.respond": 10.0,
    "config.set": 10.0,
    "model.options": 60.0,
    "model.save_key": 60.0,
    "session.resume": 60.0,
    "session.list": 60.0,
    "slash.exec": 120.0,
}
_DEFAULT_TIMEOUT = 30.0

_STDERR_KEEP = 200          # linhas de stderr retidas para diagnostico
_CRASH_WINDOW_S = 60.0      # janela p/ contar respawns seguidos


def _timeout_for(method, override, default):
    if override is not None:
        return override
    return _TIMEOUTS.get(method, default)


class GatewayProcess(object):
    """Spawn/kill do processo do gateway; sem conhecimento de protocolo."""

    def __init__(self, python, cwd=None, extra_env=None):
        self.python = python
        self.cwd = cwd or None
        self.extra_env = dict(extra_env or {})
        self.proc = None

    def spawn(self):
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        # O estado do backend (config.yaml, .env, state.db, plugins) mora em
        # ~/.hypr-ia. O nome da variavel e o que o gateway upstream le.
        env.setdefault("HERMES_HOME", os.path.expanduser("~/.hypr-ia"))
        env.update(self.extra_env)
        self.proc = subprocess.Popen(
            [self.python, "-m", "tui_gateway.entry"],
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
            start_new_session=True,
        )
        return self.proc


class HypriaClient(object):
    """Transporte JSON-RPC + dono do ciclo de vida do processo."""

    def __init__(self, python, cwd=None, extra_env=None,
                 spawn_timeout=20.0, request_timeout=_DEFAULT_TIMEOUT,
                 restart_backoff=(0.5, 2.0, 5.0), auto_restart=True):
        self._gateway = GatewayProcess(python, cwd, extra_env)
        self._spawn_timeout = float(spawn_timeout)
        self._request_timeout = float(request_timeout)
        self._restart_backoff = list(restart_backoff or ())
        self._auto_restart = bool(auto_restart)

        self._proc = None
        self._lock = threading.Lock()          # _pending/_turns/last_seen_seq
        self._write_lock = threading.Lock()    # stdin
        self._spawn_lock = threading.Lock()    # serializa start/restart/respawn
        self._spawn_gen = 0                    # invalida respawns agendados
        self._pending = {}                     # id -> (Future | on_error callable | None)
        self._turns = {}                       # session_id -> queue.Queue
        self.last_seen_seq = {}                # session_id -> seq
        self._background_handler = None
        self._id_counter = 0
        self._ready_evt = threading.Event()
        self._closing = False
        self._crash_times = deque(maxlen=8)
        self._respawning = False
        self._stderr_ring = deque(maxlen=_STDERR_KEEP)
        self._reader_thread = None
        self._stderr_thread = None

    @classmethod
    def from_config(cls, config):
        """Constroi a partir do config do hyde-ai (chaves ``hypria.*``)."""
        python = str(config.get("hypria.python", "") or "")
        path = str(config.get("hypria.path", "") or "")
        if not python and path:
            # Mesmo fallback do doctor/setup: o venv que o uv sync cria
            # dentro do checkout. Sem ele o doctor diria "ok" para um
            # painel que nao sobe.
            candidato = os.path.join(path, ".venv", "bin", "python")
            if os.path.exists(candidato):
                python = candidato
        return cls(
            python=python,
            cwd=path or None,
            spawn_timeout=float(config.get("hypria.spawn_timeout", 20.0) or 20.0),
            request_timeout=float(config.get("hypria.request_timeout", _DEFAULT_TIMEOUT)
                                  or _DEFAULT_TIMEOUT),
            restart_backoff=list(config.get("hypria.restart_backoff", [0.5, 2.0, 5.0])
                                 or []),
        )

    # -- ciclo de vida ---------------------------------------------------

    def start(self, timeout=None):
        """Spawna o gateway e espera o ``gateway.ready`` + ``ping``.

        Levanta ``HypriaError`` com o rabo do stderr quando o gateway nao
        sobe; nesse caso o processo ja foi morto.
        """
        with self._spawn_lock:
            self._start_locked(timeout)

    def _start_locked(self, timeout=None):
        if not self._gateway.python:
            raise HypriaError("hypria.python nao configurado (rode hyde-ai --setup)")
        timeout = self._spawn_timeout if timeout is None else timeout
        self._ready_evt.clear()
        try:
            self._proc = self._gateway.spawn()
        except OSError as exc:
            raise HypriaError("falha ao spawnar o gateway: %s" % exc)
        self._start_io_threads()
        if not self._ready_evt.wait(timeout):
            self._kill_proc()
            raise HypriaError("gateway nao emitiu gateway.ready em %.0fs%s"
                              % (timeout, self._stderr_hint()))
        try:
            self.request("ping", {}, timeout=5.0)
        except HypriaError as exc:
            self._kill_proc()
            raise HypriaError("gateway subiu mas nao respondeu ping: %s%s"
                              % (exc, self._stderr_hint()))

    def alive(self):
        proc = self._proc
        return bool(proc is not None and proc.poll() is None
                    and self._ready_evt.is_set() and not self._closing)

    def restart(self):
        """Derruba e sobe de novo (usado pelo /restart e pela politica de crash)."""
        with self._lock:
            self._spawn_gen += 1        # respawn dormindo aborta ao acordar
        with self._spawn_lock:          # espera qualquer spawn em voo acabar
            self._closing = True        # EOF do reader antigo nao e crash
            self._kill_proc()
            self._proc = None           # reader antigo falha o `proc is self._proc`
            self._crash_times.clear()
            self._closing = False
            self._start_locked()

    def close(self, grace=2.0):
        """Encerra o gateway: fecha stdin (EOF encerra o entry.py limpo),
        espera, escala para terminate/kill se preciso."""
        self._closing = True
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(grace)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._fail_pending(HypriaGatewayDead("gateway encerrado"))

    # -- requests --------------------------------------------------------

    def request(self, method, params=None, timeout=None):
        """RPC bloqueante, correlacionada por id.  Levanta HypriaRpcError /
        HypriaTimeout / HypriaGatewayDead."""
        rid = self._next_id()
        fut = Future()
        with self._lock:
            self._pending[rid] = fut
        try:
            self._write_frame({"jsonrpc": "2.0", "id": rid,
                               "method": method, "params": params or {}})
        except HypriaError:
            with self._lock:
                self._pending.pop(rid, None)
            raise
        try:
            return fut.result(_timeout_for(method, timeout, self._request_timeout))
        except TimeoutError:
            with self._lock:
                self._pending.pop(rid, None)   # resposta atrasada sera descartada
            raise HypriaTimeout("%s sem resposta" % method)

    def request_async(self, method, params=None, on_error=None):
        """RPC dispara-e-esquece (approval.respond, interrupt...).  ``on_error``
        roda NA THREAD LEITORA — o chamador marshala se precisar de GTK."""
        rid = self._next_id()
        with self._lock:
            self._pending[rid] = on_error or (lambda exc: None)
        try:
            self._write_frame({"jsonrpc": "2.0", "id": rid,
                               "method": method, "params": params or {}})
        except HypriaError as exc:
            with self._lock:
                self._pending.pop(rid, None)
            if on_error:
                on_error(exc)

    # -- eventos ---------------------------------------------------------

    def open_turn(self, session_id):
        """Registra a fila que recebe os eventos desta sessao durante um turno."""
        q = queue.Queue()
        with self._lock:
            self._turns[session_id] = q
        return q

    def close_turn(self, session_id):
        with self._lock:
            self._turns.pop(session_id, None)

    def set_background_handler(self, cb):
        """Eventos sem turno aberto (session.title, notification.show...).
        ``cb(params_dict)`` roda na thread leitora — marshale com idle_add."""
        self._background_handler = cb

    @property
    def last_stderr(self):
        return list(self._stderr_ring)

    # -- internals -------------------------------------------------------

    def _next_id(self):
        with self._lock:
            self._id_counter += 1
            return "ha-%d" % self._id_counter

    def _write_frame(self, frame):
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            raise HypriaGatewayDead("gateway fora do ar%s" % self._stderr_hint())
        data = json.dumps(frame, ensure_ascii=False)
        with self._write_lock:
            try:
                proc.stdin.write(data + "\n")
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise HypriaGatewayDead("escrita no gateway falhou: %s" % exc)

    def _start_io_threads(self):
        self._reader_thread = threading.Thread(
            target=self._reader_loop, args=(self._proc,),
            name="hypria-reader", daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, args=(self._proc,),
            name="hypria-stderr", daemon=True)
        self._stderr_thread.start()

    def _reader_loop(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except ValueError:
                    self._note_stderr("frame ilegivel: %s" % line[:120])
                    continue
                if not isinstance(frame, dict):
                    continue
                self._route_frame(frame)
        except (OSError, ValueError):
            pass
        if proc is self._proc:
            self._on_process_exit()

    def _route_frame(self, frame):
        rid = frame.get("id")
        if rid is not None:
            with self._lock:
                waiter = self._pending.pop(rid, None)
            if waiter is None:
                return                          # resposta atrasada de um timeout
            err = frame.get("error")
            if isinstance(waiter, Future):
                if err:
                    waiter.set_exception(HypriaRpcError(
                        err.get("code"), err.get("message"), err.get("data")))
                else:
                    waiter.set_result(frame.get("result") or {})
            elif err:
                try:
                    waiter(HypriaRpcError(err.get("code"), err.get("message"),
                                          err.get("data")))
                except Exception:
                    pass
            return
        if frame.get("method") != "event":
            return
        params = frame.get("params") or {}
        sid = params.get("session_id") or ""
        seq = params.get("seq")
        if sid and isinstance(seq, int):
            self.last_seen_seq[sid] = seq
        if params.get("type") == "gateway.ready":
            self._ready_evt.set()
            return
        with self._lock:
            turn_q = self._turns.get(sid)
        if turn_q is not None:
            turn_q.put(params)
            return
        handler = self._background_handler
        if handler is not None:
            try:
                handler(params)
            except Exception:
                pass

    def _stderr_loop(self, proc):
        try:
            for line in proc.stderr:
                line = line.rstrip("\n")
                if line:
                    self._note_stderr(line)
        except (OSError, ValueError):
            pass

    def _note_stderr(self, line):
        self._stderr_ring.append(line)
        try:
            print("[hypria] %s" % line, file=sys.stderr, flush=True)
        except OSError:
            pass

    def _stderr_hint(self):
        tail = [l for l in self._stderr_ring][-3:]
        return (" — stderr: " + " | ".join(tail)) if tail else ""

    def _fail_pending(self, exc):
        with self._lock:
            pending = dict(self._pending)
            self._pending.clear()
            turns = dict(self._turns)
        for waiter in pending.values():
            if isinstance(waiter, Future):
                if not waiter.done():
                    waiter.set_exception(exc)
            else:
                try:
                    waiter(exc)
                except Exception:
                    pass
        # Encerra geradores parados no queue.get com um complete sintetico.
        for sid, q in turns.items():
            q.put({"type": "message.complete", "session_id": sid,
                   "payload": {"status": "error",
                               "error": "gateway do Hypria caiu (veja o log)"},
                   "synthetic": True})

    def _on_process_exit(self):
        self._ready_evt.clear()
        if self._closing:
            return
        self._fail_pending(HypriaGatewayDead("gateway morreu%s" % self._stderr_hint()))
        self._notify_background({"type": "gateway.died", "session_id": "",
                                 "payload": {}, "synthetic": True})
        if self._auto_restart:
            self._schedule_respawn()

    def _notify_background(self, params):
        handler = self._background_handler
        if handler is not None:
            try:
                handler(params)
            except Exception:
                pass

    def _schedule_respawn(self):
        with self._lock:
            if self._respawning or self._closing:
                return
            self._respawning = True
        threading.Thread(target=self._respawn_loop,
                         name="hypria-respawn", daemon=True).start()

    def _respawn_loop(self):
        with self._lock:
            gen = self._spawn_gen
        try:
            now = time.monotonic()
            recent = [t for t in self._crash_times if now - t < _CRASH_WINDOW_S]
            if len(recent) >= max(len(self._restart_backoff), 3):
                self._note_stderr("gateway caiu %d vezes em %.0fs; desisto ate /restart"
                                  % (len(recent), _CRASH_WINDOW_S))
                return
            self._crash_times.append(now)
            delay = (self._restart_backoff[min(len(recent),
                                               len(self._restart_backoff) - 1)]
                     if self._restart_backoff else 1.0)
            time.sleep(delay)
            with self._spawn_lock:      # nunca dois spawns ao mesmo tempo
                with self._lock:
                    if self._spawn_gen != gen or self._closing:
                        return          # um restart deliberado passou na frente
                try:
                    self._proc = self._gateway.spawn()
                except OSError as exc:
                    self._note_stderr("respawn falhou: %s" % exc)
                    return
                self._ready_evt.clear()
                self._start_io_threads()
                if not self._ready_evt.wait(self._spawn_timeout):
                    self._kill_proc()
                    self._note_stderr("respawn: gateway.ready nao veio")
                    return
            self._notify_background({"type": "gateway.restarted", "session_id": "",
                                     "payload": {}, "synthetic": True})
        finally:
            with self._lock:
                self._respawning = False

    def _kill_proc(self):
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(0.5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
