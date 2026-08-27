#!/usr/bin/env python3
"""Testes do lib/hermes_registry.py (adapter) contra o fake_gateway.

Sem GTK: valida o contrato que o sidebar consome — lista de providers,
stream_events com a tabela de traducao (tool/approval/interim/erro) e a
troca de modelo.  `python3 tests/test_registry.py` → `N casos, M falhas`.
"""

import os
import sys
import threading

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RAIZ, "lib"))

from hermes_client import HermesClient, HermesError  # noqa: E402
from hermes_registry import HermesRegistry, HermesCancel  # noqa: E402

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


class ConfigFalso(object):
    """Config minimo com a API pontilhada que o registry usa."""

    def __init__(self):
        self.dados = {"hermes.show_thinking": True}

    def get(self, chave, padrao=None):
        return self.dados.get(chave, padrao)

    def set(self, chave, valor):
        self.dados[chave] = valor

    def save(self):
        pass


def registry_novo(modos=""):
    cli = HermesClient(python=sys.executable, cwd=SHIM,
                       extra_env={"FAKE_GATEWAY_ARGS": modos},
                       auto_restart=False, spawn_timeout=10.0)
    return HermesRegistry(cli, ConfigFalso())


# -- 1. refresh + inventario de providers -------------------------------
reg = registry_novo()
try:
    antes = reg.list_providers()
    caso("antes do refresh: linha unica Hermes indisponivel",
         len(antes) == 1 and antes[0].id == "hermes" and not antes[0].available)
    pronto = threading.Event()
    reg.refresh_async(pronto.set)
    caso("refresh_async termina", pronto.wait(15.0))
    provs = reg.list_providers()
    ids = sorted(p.id for p in provs)
    caso("providers vem do model.options", ids == ["fake", "semkey"], repr(ids))
    fake = reg.get_provider("fake")
    caso("provider autenticado disponivel", fake.available and fake.models
         and fake.models[0].id == "fake/answer-1")
    semkey = reg.get_provider("semkey")
    caso("provider sem chave indisponivel com hint",
         not semkey.available and "key" in semkey.hint)
    caso("first_available/default_model do payload",
         reg.first_available() == "fake"
         and reg.default_model("fake") == "fake/answer-1")

    # -- 2. stream_events: traducao completa -----------------------------
    eventos = []
    cancel = reg.new_cancel()
    for ev in reg.stream_events("fake", "fake/answer-1",
                                [{"role": "user", "content": "oi"}],
                                "", cancel):
        eventos.append((ev.kind, ev.text, ev.data))
    kinds = [k for k, _t, _d in eventos]
    caso("stream: texto e thinking na ordem",
         kinds[0] == "text" and "thinking" in kinds, repr(kinds))
    caso("stream: tool_start/tool_done presentes",
         "tool_start" in kinds and "tool_done" in kinds, repr(kinds))
    caso("stream: usage encerra com done_reason",
         kinds[-1] == "usage" and eventos[-1][2].get("done_reason") == "stop")
    texto = "".join(t for k, t, _d in eventos if k == "text")
    caso("stream: texto acumulado certo", texto == "Ola, mundo!", repr(texto))

    # -- 3. troca de modelo ----------------------------------------------
    aviso = reg.set_model("fake/answer-1", "fake")
    caso("set_model devolve warning vazio", aviso == "", repr(aviso))
    caso("set_model persiste sticky",
         reg._config.get("hermes.model") == "fake/answer-1")

    # -- 4. slash passthrough --------------------------------------------
    saida = reg.slash_exec("/memoria")
    caso("slash_exec devolve dict tipado",
         isinstance(saida, dict) and saida.get("type") == "output"
         and str(saida.get("output") or "").startswith("executei:"),
         repr(saida))
finally:
    reg.close()

# -- 5. approval no meio do stream + interim already_streamed ------------
reg = registry_novo("--approval --interim-already-streamed")
try:
    pronto = threading.Event()
    reg.refresh_async(pronto.set)
    pronto.wait(15.0)
    eventos = []
    for ev in reg.stream_events("fake", "", [{"role": "user", "content": "oi"}],
                                "", reg.new_cancel()):
        eventos.append(ev)
        if ev.kind == "approval":
            caso("approval: payload com choices e request_id",
                 ev.data.get("choices") == ["once", "session", "always", "deny"]
                 and ev.data.get("request_id") == "req-1")
            reg.respond_approval("once", ev.data["request_id"])
    kinds = [e.kind for e in eventos]
    texto = "".join(e.text for e in eventos if e.kind == "text")
    caso("approval: stream continua depois do respond",
         "[aprovado]" in texto, repr(texto))
    caso("interim already_streamed nao duplica texto",
         texto.count("Ola") == 1, repr(texto))
    caso("approval aparece como kind proprio", "approval" in kinds)
finally:
    reg.close()

# -- 6. crash vira excecao no gerador ------------------------------------
reg = registry_novo("--crash-after-delta")
try:
    pronto = threading.Event()
    reg.refresh_async(pronto.set)
    pronto.wait(15.0)
    try:
        for _ev in reg.stream_events("fake", "", [{"role": "user",
                                                   "content": "oi"}],
                                     "", reg.new_cancel()):
            pass
        caso("crash: gerador levanta HermesError", False)
    except HermesError:
        caso("crash: gerador levanta HermesError", True)
finally:
    reg.close()

# -- 7. submit com turno em andamento: status "queued" -------------------
reg = registry_novo("--busy-queued")
try:
    pronto = threading.Event()
    reg.refresh_async(pronto.set)
    pronto.wait(15.0)
    eventos = [ev for ev in reg.stream_events(
        "fake", "", [{"role": "user", "content": "oi"}], "", reg.new_cancel())]
    texto = "".join(e.text for e in eventos if e.kind == "text")
    caso("queued: engole os frames do turno velho",
         "sobra" not in texto, repr(texto))
    caso("queued: streama o turno novo inteiro",
         texto == "Ola, mundo!", repr(texto))
finally:
    reg.close()

# -- 8. submit que so parou a voz: voice_stopped -------------------------
reg = registry_novo("--voice-stopped")
try:
    pronto = threading.Event()
    reg.refresh_async(pronto.set)
    pronto.wait(15.0)
    eventos = list(reg.stream_events(
        "fake", "", [{"role": "user", "content": "oi"}], "", reg.new_cancel()))
    caso("voice_stopped: turno vazio, sem erro", eventos == [], repr(eventos))
finally:
    reg.close()

# -- 9. clarify em lote (questions[]) vira um evento por pergunta -------
reg = registry_novo("--clarify-batch")
try:
    pronto = threading.Event()
    reg.refresh_async(pronto.set)
    pronto.wait(15.0)
    clarifies = []
    for ev in reg.stream_events("fake", "", [{"role": "user", "content": "oi"}],
                                "", reg.new_cancel()):
        if ev.kind == "clarify":
            clarifies.append(dict(ev.data))
            if len(clarifies) == 2:
                # responde uma vez; o fake so espera um clarify.respond
                reg.respond_clarify("azul", ev.data.get("request_id"),
                                    ev.data.get("question_id"))
    caso("clarify em lote: um evento por pergunta",
         len(clarifies) == 2, repr(clarifies))
    caso("clarify em lote: qid e request_id preservados",
         [c.get("question_id") for c in clarifies] == ["q1", "q2"]
         and all(c.get("request_id") == "clr-1" for c in clarifies),
         repr(clarifies))
finally:
    reg.close()

# -- 10. token de cancelamento ------------------------------------------
chamado = []
tok = HermesCancel(lambda: chamado.append(1))
caso("cancel: comeca limpo", not tok.cancelled and not tok.is_set())
tok.cancel()
caso("cancel: seta flag e dispara interrupt uma vez",
     tok.cancelled and tok.is_set() and chamado == [1])
tok.cancel()
caso("cancel: idempotente", chamado == [1])

print("%d casos, %d falhas" % (casos, falhas))
sys.exit(1 if falhas else 0)
