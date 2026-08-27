#!/usr/bin/env python3
"""Gateway falso do Hypria para testes do hypria_client.

Fala o mesmo protocolo do ``tui_gateway.entry`` real: JSON-RPC por linha,
``gateway.ready`` como primeiro frame, eventos como notificacoes com
``{type, session_id, payload, seq}``.  Puro stdlib, single-thread.

Modos (argv):
  --no-ready                nunca emite gateway.ready (teste de timeout)
  --slow-response           segura a resposta de model.options por 3s
  --out-of-order            responde model.options DEPOIS do request seguinte
  --crash-after-delta       em prompt.submit: start + 1 delta e morre com exit 1
  --approval                inclui approval.request no stream e espera o respond
  --interim-already-streamed inclui message.interim {already_streamed: true}
  --busy-queued             prompt.submit responde {status: queued} e emite os
                            frames do turno VELHO antes do stream normal
  --voice-stopped           prompt.submit responde {voice_stopped: true}, sem turno
  --clarify-batch           inclui clarify.request em lote (questions[]) no stream
"""

import json
import sys
import time

MODES = set(sys.argv[1:])
_seq = 0


def write(frame):
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def event(tipo, sid, payload):
    global _seq
    _seq += 1
    write({"jsonrpc": "2.0", "method": "event",
           "params": {"type": tipo, "session_id": sid,
                      "payload": payload, "seq": _seq}})


def ok(rid, result):
    write({"jsonrpc": "2.0", "id": rid, "result": result})


def err(rid, code, msg):
    write({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}})


def read_request():
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return read_request()
    return json.loads(line)


MODEL_OPTIONS = {
    "model": "fake/answer-1",
    "provider": "fake",
    "providers": [
        {"slug": "fake", "name": "Fake", "authenticated": True, "warning": "",
         "is_current": True,
         # um dict e uma string: o inventario real usa os dois formatos
         "models": [{"id": "fake/answer-1", "name": "Answer 1",
                     "description": "modelo de mentira"},
                    "fake/answer-2"]},
        {"slug": "semkey", "name": "Sem Key", "authenticated": False,
         "warning": "precisa de api key", "is_current": False, "models": []},
    ],
}


def wait_for(method_wanted, sid):
    """Atende requests ate chegar ``method_wanted``; devolve seus params."""
    while True:
        req = read_request()
        if req is None:
            sys.exit(0)
        method, rid, params = req.get("method"), req.get("id"), req.get("params") or {}
        if method == method_wanted:
            if method == "approval.respond":
                ok(rid, {"resolved": 1})
            else:
                ok(rid, {})
            return params
        handle_simple(method, rid, params)


def stream_turn(sid, params):
    event("message.start", sid, {})
    event("message.delta", sid, {"text": "Ola"})
    if "--crash-after-delta" in MODES:
        sys.stdout.flush()
        import os
        os._exit(1)
    event("thinking.delta", sid, {"text": "pensando bem... "})
    event("message.delta", sid, {"text": ", mundo"})
    event("tool.start", sid, {"tool_id": "t1", "name": "terminal",
                              "context": "echo oi",
                              "args": {"command": "echo oi"}})
    event("tool.complete", sid, {"tool_id": "t1", "name": "terminal",
                                 "args": {"command": "echo oi"},
                                 "result": "oi", "summary": "oi",
                                 "duration_s": 0.1})
    if "--approval" in MODES:
        event("approval.request", sid, {
            "command": "rm -rf /tmp/x", "description": "apaga /tmp/x",
            "choices": ["once", "session", "always", "deny"],
            "request_id": "req-1", "allow_session": True, "allow_permanent": True})
        resp = wait_for("approval.respond", sid)
        texto = " [negado]" if resp.get("choice") == "deny" else " [aprovado]"
        event("message.delta", sid, {"text": texto})
    if "--clarify-batch" in MODES:
        event("clarify.request", sid, {
            "request_id": "clr-1",
            "questions": [
                {"qid": "q1", "question": "cor?",
                 "choices": ["azul", "verde"], "multi_select": False},
                {"qid": "q2", "question": "tamanho?", "choices": [],
                 "multi_select": False}]})
        resp = wait_for("clarify.respond", sid)
        event("message.delta", sid,
              {"text": " [%s]" % resp.get("answer", "")})
    if "--interim-already-streamed" in MODES:
        event("message.interim", sid, {"text": "Ola, mundo",
                                       "already_streamed": True})
    event("message.delta", sid, {"text": "!"})
    event("message.complete", sid, {"text": "Ola, mundo!", "status": "ok",
                                    "done_reason": "stop",
                                    "usage": {"input_tokens": 10,
                                              "output_tokens": 4}})


def handle_simple(method, rid, params):
    sid = params.get("session_id", "live-1")
    if method == "ping":
        ok(rid, {"pong": True})
    elif method == "session.create":
        ok(rid, {"session_id": "live-1", "stored_session_id": "stored-1",
                 "message_count": 0, "messages": [],
                 "info": {"model": "fake/answer-1", "provider": "fake",
                          "title": ""}})
    elif method == "session.resume":
        ok(rid, {"session_id": "live-1", "resumed": True, "session_key": "stored-1",
                 "message_count": 2,
                 "messages": [{"role": "user", "text": "oi"},
                              {"role": "assistant", "text": "ola!"}],
                 "info": {"model": "fake/answer-1", "provider": "fake",
                          "title": "conversa antiga"}})
    elif method == "session.list":
        # started_at e epoch Unix (float), como no gateway real
        ok(rid, {"sessions": [
            {"id": "stored-1", "title": "conversa antiga", "preview": "oi",
             "started_at": 1756123200.0, "message_count": 2,
             "source": "hyde-ai"}]})
    elif method == "session.interrupt":
        ok(rid, {"status": "interrupted"})
    elif method == "model.options":
        if "--slow-response" in MODES:
            time.sleep(3.0)
        ok(rid, MODEL_OPTIONS)
    elif method == "model.save_key":
        ok(rid, {"ok": True})
    elif method == "config.set":
        ok(rid, {"key": params.get("key"), "value": params.get("value"),
                 "warning": "", "confirm_required": False, "scope": "session"})
    elif method == "slash.exec":
        ok(rid, {"output": "executei: %s" % params.get("command", "")})
    elif method == "approval.respond":
        ok(rid, {"resolved": 1})
    elif method == "prompt.submit":
        if "--voice-stopped" in MODES:
            ok(rid, {"voice_stopped": True})
            return
        if "--busy-queued" in MODES:
            # havia um turno rodando: o texto foi para a fila e os frames
            # que chegam primeiro sao do turno ANTIGO
            ok(rid, {"status": "queued"})
            event("message.delta", sid, {"text": "sobra do turno velho"})
            event("message.complete", sid, {"text": "sobra do turno velho",
                                            "status": "ok",
                                            "done_reason": "stop"})
        else:
            ok(rid, {"status": "streaming"})
        stream_turn(sid, params)
    elif method == "commands.catalog":
        ok(rid, {"pairs": [["/memoria", "mostra a memoria"]], "canon": {},
                 "categories": {}, "sub": {}, "skill_count": 0})
    else:
        err(rid, -32601, "metodo desconhecido: %s" % method)


def main():
    if "--no-ready" not in MODES:
        event("gateway.ready", "", {"skin": {"name": "fake"},
                                    "change_events": True})
    held = None      # (rid,) segurado pelo modo --out-of-order
    while True:
        req = read_request()
        if req is None:
            break
        method, rid, params = req.get("method"), req.get("id"), req.get("params") or {}
        if "--out-of-order" in MODES:
            if method == "model.options" and held is None:
                held = rid
                continue
            handle_simple(method, rid, params)
            if held is not None:
                ok(held, MODEL_OPTIONS)
                held = None
            continue
        handle_simple(method, rid, params)
    sys.exit(0)


if __name__ == "__main__":
    main()
