#!/usr/bin/env python3
"""Testes de protocolo do lib/hypria_client.py contra o fake_gateway.

Roda com `python3 tests/test_hypria_client.py`; imprime `N casos, M falhas`
e sai com 1 quando algo falha — mesmo estilo dos outros testes do repo.

Com `--real` (ou HYDE_AI_HYPRIA_REAL=1) roda um smoke extra contra o
gateway REAL do Hypria usando hypria.python/hypria.path do config.
"""

import os
import queue
import sys
import threading
import time

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RAIZ, "lib"))

from hypria_client import (  # noqa: E402
    HypriaClient, HypriaError, HypriaTimeout, HypriaGatewayDead, HypriaRpcError,
)

SHIM = os.path.join(RAIZ, "tests", "shim")

casos = 0
falhas = 0


def caso(nome, cond, extra=""):
    global casos, falhas
    casos += 1
    if cond:
        print("  ok  %s" % nome)
    else:
        falhas += 1
        print("FALHA %s %s" % (nome, extra))


def cliente(modos="", **kw):
    kw.setdefault("auto_restart", False)
    kw.setdefault("spawn_timeout", 10.0)
    return HypriaClient(
        python=sys.executable, cwd=SHIM,
        extra_env={"FAKE_GATEWAY_ARGS": modos}, **kw)


def coleta_turno(cli, sid, texto, ate_tipo="message.complete", timeout=10.0):
    """Submete um prompt e coleta os eventos do turno ate ``ate_tipo``."""
    q = cli.open_turn(sid)
    eventos = []
    try:
        cli.request("prompt.submit", {"session_id": sid, "text": texto})
        prazo = time.time() + timeout
        while time.time() < prazo:
            try:
                ev = q.get(timeout=0.5)
            except queue.Empty:
                continue
            eventos.append(ev)
            if ev.get("type") == ate_tipo:
                break
    finally:
        cli.close_turn(sid)
    return eventos


# -- 1. handshake ok + ping + close limpo -------------------------------
c = cliente()
try:
    c.start()
    caso("handshake: gateway.ready + ping", c.alive())
    r = c.request("ping", {})
    caso("ping devolve pong", r.get("pong") is True, repr(r))
finally:
    c.close()
caso("close: processo saiu com 0", c._proc is not None and c._proc.returncode == 0,
     repr(c._proc and c._proc.returncode))

# -- 2. handshake timeout ------------------------------------------------
c = cliente("--no-ready", spawn_timeout=1.0)
try:
    c.start()
    caso("timeout de ready levanta HypriaError", False)
except HypriaError:
    caso("timeout de ready levanta HypriaError", True)
caso("timeout de ready mata o processo",
     c._proc is None or c._proc.poll() is not None)

# -- 3. correlacao fora de ordem ----------------------------------------
c = cliente("--out-of-order")
try:
    c.start()
    resultado = {}

    def lenta():
        resultado["options"] = c.request("model.options", {})

    t = threading.Thread(target=lenta)
    t.start()
    time.sleep(0.3)                     # garante que model.options chegou antes
    r = c.request("ping", {})
    t.join(5)
    caso("fora de ordem: ping nao trava", r.get("pong") is True)
    caso("fora de ordem: model.options chega certo",
         resultado.get("options", {}).get("provider") == "fake",
         repr(resultado.get("options")))
finally:
    c.close()

# -- 4. timeout de request + resposta atrasada descartada ---------------
c = cliente("--slow-response")
try:
    c.start()
    try:
        c.request("model.options", {}, timeout=0.5)
        caso("timeout de request levanta HypriaTimeout", False)
    except HypriaTimeout:
        caso("timeout de request levanta HypriaTimeout", True)
    time.sleep(3.0)                     # a resposta atrasada chega e e descartada
    r = c.request("ping", {})
    caso("resposta atrasada nao corrompe o canal", r.get("pong") is True)
finally:
    c.close()

# -- 5. erro RPC ---------------------------------------------------------
c = cliente()
try:
    c.start()
    try:
        c.request("metodo.inexistente", {})
        caso("erro RPC levanta HypriaRpcError", False)
    except HypriaRpcError as exc:
        caso("erro RPC levanta HypriaRpcError", exc.code == -32601, repr(exc.code))
finally:
    c.close()

# -- 6. turno com stream + approval -------------------------------------
c = cliente("--approval --interim-already-streamed")
try:
    c.start()
    sid = c.request("session.create", {})["session_id"]
    q = c.open_turn(sid)
    c.request("prompt.submit", {"session_id": sid, "text": "oi"})
    tipos, aprovado = [], False
    prazo = time.time() + 10
    while time.time() < prazo:
        try:
            ev = q.get(timeout=0.5)
        except queue.Empty:
            continue
        tipos.append(ev.get("type"))
        if ev.get("type") == "approval.request" and not aprovado:
            aprovado = True
            c.request_async("approval.respond",
                            {"session_id": sid, "choice": "once",
                             "request_id": ev["payload"]["request_id"]})
        if ev.get("type") == "message.complete":
            break
    c.close_turn(sid)
    caso("stream: sequencia esperada",
         tipos[:3] == ["message.start", "message.delta", "thinking.delta"]
         and "tool.start" in tipos and "tool.complete" in tipos
         and "approval.request" in tipos and tipos[-1] == "message.complete",
         repr(tipos))
    caso("stream: continua apos approval.respond",
         "message.interim" in tipos and tipos.index("approval.request")
         < tipos.index("message.interim"))
    caso("seq registrado por sessao", c.last_seen_seq.get(sid, 0) > 0)
finally:
    c.close()

# -- 7. crash no meio do turno (sem auto-restart) -----------------------
c = cliente("--crash-after-delta")
try:
    c.start()
    sid = c.request("session.create", {})["session_id"]
    eventos = coleta_turno(c, sid, "oi", timeout=6.0)
    ultimo = eventos[-1] if eventos else {}
    caso("crash: complete sintetico de erro encerra o turno",
         ultimo.get("type") == "message.complete"
         and (ultimo.get("payload") or {}).get("status") == "error",
         repr(ultimo))
    try:
        c.request("ping", {})
        caso("crash: request depois da morte falha", False)
    except (HypriaGatewayDead, HypriaTimeout):
        caso("crash: request depois da morte falha", True)
finally:
    c.close()

# -- 8. respawn automatico ----------------------------------------------
c = cliente("--crash-after-delta", auto_restart=True, restart_backoff=[0.1])
try:
    vistos = []
    renasceu = threading.Event()

    def bg(params):
        vistos.append(params.get("type"))
        if params.get("type") == "gateway.restarted":
            renasceu.set()

    c.set_background_handler(bg)
    c.start()
    sid = c.request("session.create", {})["session_id"]
    coleta_turno(c, sid, "oi", timeout=6.0)      # derruba o gateway
    caso("respawn: gateway.restarted chega", renasceu.wait(8.0), repr(vistos))
    caso("respawn: gateway.died veio antes", "gateway.died" in vistos, repr(vistos))
    r = c.request("ping", {})
    caso("respawn: ping volta a funcionar", r.get("pong") is True)
finally:
    c.close()

print("%d casos, %d falhas" % (casos, falhas))

# -- smoke opcional contra o gateway REAL -------------------------------
if "--real" in sys.argv or os.environ.get("HYDE_AI_HYPRIA_REAL") == "1":
    sys.path.insert(0, os.path.join(RAIZ, "lib"))
    import config as config_mod
    cfg = config_mod.load()
    py = cfg.get("hypria.python", "")
    print("== smoke real: %s ==" % py)
    real = HypriaClient(python=py, cwd=cfg.get("hypria.path", "") or None,
                        auto_restart=False)
    try:
        real.start(30.0)
        info = real.request("session.create", {"source": "hyde-ai-test",
                                               "cols": 120})
        print("  sessao:", info.get("session_id"), "/",
              info.get("stored_session_id"))
        evs = []
        q = real.open_turn(info["session_id"])
        real.request("prompt.submit", {"session_id": info["session_id"],
                                       "text": "responda apenas: OK"})
        prazo = time.time() + 120
        while time.time() < prazo:
            try:
                ev = q.get(timeout=1.0)
            except queue.Empty:
                continue
            evs.append(ev.get("type"))
            if ev.get("type") == "message.complete":
                print("  turno real ok:", (ev.get("payload") or {}).get(
                    "text", "")[:80])
                break
        real.close_turn(info["session_id"])
        print("  eventos:", evs)
    finally:
        real.close()

sys.exit(1 if falhas else 0)
