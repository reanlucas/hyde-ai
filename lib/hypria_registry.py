"""Registry do hyde-ai servido pelo Hypria.

Satisfaz o mesmo contrato que o antigo RegistryProxy/providers.py:

    list_providers() / models(pid) / default_model(pid) / first_available()
    set_api_key(pid, v) / refresh_async(cb) / new_cancel()
    stream_events(pid, model_id, messages, system, cancel, tools=None)

so que por baixo tudo vira JSON-RPC contra o gateway do Hypria via
``hypria_client``.  O Hypria e o dono do contexto e do loop agentico: o
``stream_events`` manda apenas o texto da ultima mensagem do usuario
(``prompt.submit``) e traduz os eventos do gateway para os eventos
duck-typed que o sidebar consome (``.kind``/``.text``/``.data``).

Kinds novos alem de text/thinking/usage/meta: tool_start, tool_done,
approval, approval_expire, clarify, status.  Sem GTK aqui — quem marshala
para o main loop e o sidebar.
"""

from __future__ import annotations

import os
import queue
import threading
import time

from hypria_client import HypriaClient, HypriaError, HypriaRpcError

_DRAIN_CAP_S = 10.0     # apos cancelar, drena eventos por no maximo isto


class Event(object):
    __slots__ = ("kind", "text", "data")

    def __init__(self, kind, text="", data=None):
        self.kind = kind
        self.text = text
        self.data = data or {}


class ProviderInfo(object):
    __slots__ = ("id", "name", "available", "hint", "models")

    def __init__(self, pid, name, available=True, hint="", models=None):
        self.id = pid
        self.name = name or pid
        self.available = bool(available)
        self.hint = hint or ""
        self.models = models or []


class ModelInfo(object):
    __slots__ = ("id", "name", "description")

    def __init__(self, mid, name=None, description=""):
        self.id = mid
        self.name = name or mid
        self.description = description or ""


class HypriaCancel(object):
    """Token de cancelamento: seta a flag local e dispara session.interrupt."""

    def __init__(self, on_cancel=None):
        self._evt = threading.Event()
        self._on_cancel = on_cancel

    def cancel(self):
        if self._evt.is_set():
            return
        self._evt.set()
        if self._on_cancel is not None:
            try:
                self._on_cancel()
            except Exception:
                pass

    def set(self):                      # compat com threading.Event
        self.cancel()

    def is_set(self):
        return self._evt.is_set()

    @property
    def cancelled(self):
        return self._evt.is_set()


def _cancelado(token):
    if token is None:
        return False
    flag = getattr(token, "cancelled", None)
    if flag is not None:
        return bool(flag() if callable(flag) else flag)
    checker = getattr(token, "is_set", None)
    return bool(checker()) if callable(checker) else False


def _ultimo_texto_user(messages):
    for msg in reversed(messages or []):
        if (msg or {}).get("role") == "user":
            return str(msg.get("content") or "")
    return ""


class HypriaRegistry(object):

    def __init__(self, client, config):
        self._client = client
        self._config = config
        self._start_lock = threading.Lock()
        self._started = False
        self._start_error = ""
        self._options = None            # payload cacheado de model.options
        self._options_lock = threading.Lock()
        self._sid = None                # sessao viva no gateway
        self._stored_id = None          # chave duravel (sobrevive a respawn)
        self._session_lock = threading.Lock()
        self._session_gen = 0           # invalida RPCs de sessao atrasadas
        self._session_info = {}
        self._ui_handler = None         # cb(params) na thread leitora
        client.set_background_handler(self._on_background)

    @classmethod
    def from_config(cls, config):
        return cls(HypriaClient.from_config(config), config)

    # -- ciclo de vida ---------------------------------------------------

    def ensure_started(self):
        with self._start_lock:
            if self._started and self._client.alive():
                return
            try:
                if self._started:
                    self._client.restart()
                else:
                    self._client.start()
                self._started = True
                self._start_error = ""
            except HypriaError as exc:
                self._start_error = str(exc)
                raise

    def close(self):
        self._client.close()

    def restart(self):
        with self._session_lock:
            self._sid = None
            self._session_gen += 1
        self._client.restart()
        self._started = True
        self._start_error = ""

    def set_ui_handler(self, cb):
        """Eventos fora de turno relevantes pra interface (session.title,
        notification.show/clear, gateway.died/restarted, session.info).
        ``cb(params)`` roda em thread de fundo — marshale com idle_add."""
        self._ui_handler = cb

    def _emit_ui(self, params):
        cb = self._ui_handler
        if cb is not None:
            try:
                cb(params)
            except Exception:
                pass

    def _on_background(self, params):
        tipo = params.get("type")
        if tipo == "gateway.died":
            with self._session_lock:
                self._sid = None        # o stored_id fica; resume no proximo turno
                self._session_gen += 1
        elif tipo == "session.info":
            self._session_info = dict(params.get("payload") or {})
        self._emit_ui(params)

    # -- sessoes ---------------------------------------------------------

    def ensure_session(self):
        """Sessao viva no gateway; resume pela chave duravel apos respawn.

        Nao segura o lock durante as RPCs — o main loop tambem o adquire
        (interrupt/reset). O contador de geracao descarta o resultado de
        uma RPC que ficou obsoleta porque a sessao foi trocada no meio.
        """
        self.ensure_started()
        with self._session_lock:
            if self._sid:
                return self._sid
            stored = self._stored_id
            gen = self._session_gen

        if stored:
            resp = None
            try:
                resp = self._client.request("session.resume",
                                            {"session_id": stored})
            except HypriaError:
                pass                    # sessao sumiu: cria uma nova
            if resp is not None:
                with self._session_lock:
                    if self._session_gen == gen and not self._sid:
                        self._sid = resp.get("session_id") or stored
                        self._session_info = dict(resp.get("info") or {})
                        return self._sid
                return self.ensure_session()

        # cwd explicito: sem ele a sessao nasce no cwd do gateway — o
        # checkout do hypr-ia — e o AGENTS.md de ~95K chars do repo entra
        # inteiro no system prompt (25k tokens de prefill por conversa).
        params = {"cols": 120,
                  "cwd": os.path.expanduser("~"),
                  "source": str(self._config.get(
                      "hypria.session_source", "hyde-ai") or "hyde-ai")}
        model = str(self._config.get("hypria.model", "") or "")
        provider = str(self._config.get("hypria.provider", "") or "")
        effort = str(self._config.get("hypria.reasoning_effort", "") or "")
        if model:
            params["model"] = model
        if provider:
            params["provider"] = provider
        if effort:
            params["reasoning_effort"] = effort
        resp = self._client.request("session.create", params)
        with self._session_lock:
            if self._session_gen == gen and not self._sid:
                self._sid = resp.get("session_id")
                self._stored_id = resp.get("stored_session_id") or self._sid
                self._session_info = dict(resp.get("info") or {})
                return self._sid
            if self._sid:
                return self._sid
        # a sessao criada ficou orfa (reset/adopt no meio); tenta de novo
        return self.ensure_session()

    def new_session(self):
        """Comeca uma conversa nova no Hypria agora (RPC sincrona)."""
        with self._session_lock:
            self._sid = None
            self._stored_id = None
            self._session_gen += 1
        return self.ensure_session()

    def reset_session(self):
        """Desvincula a sessao atual sem RPC; a proxima mensagem cria outra."""
        with self._session_lock:
            self._sid = None
            self._stored_id = None
            self._session_gen += 1

    def adopt_session(self, stored_id):
        """Continua uma sessao antiga pela chave duravel; o resume acontece
        de forma preguicosa, na primeira mensagem."""
        with self._session_lock:
            self._sid = None
            self._stored_id = str(stored_id or "") or None
            self._session_gen += 1

    def list_sessions(self, limit=30):
        self.ensure_started()
        resp = self._client.request("session.list", {"limit": limit})
        return list(resp.get("sessions") or [])

    def resume(self, stored_id):
        """Abre uma conversa antiga; devolve o payload do resume."""
        self.ensure_started()
        resp = self._client.request("session.resume", {"session_id": stored_id})
        with self._session_lock:
            self._sid = resp.get("session_id") or stored_id
            self._stored_id = resp.get("session_key") or stored_id
            self._session_info = dict(resp.get("info") or {})
            self._session_gen += 1      # descarta ensure_session atrasado
        return resp

    @property
    def stored_session_id(self):
        return self._stored_id

    @property
    def session_info(self):
        return dict(self._session_info)

    # -- contrato do sidebar ---------------------------------------------

    @property
    def inventory_loaded(self):
        """True quando model.options ja chegou (a linha "Hypria" sintetica
        pre-fetch nao deve virar modelo persistido no History)."""
        with self._options_lock:
            return self._options is not None

    def list_providers(self):
        with self._options_lock:
            options = self._options
        if not options:
            alive = self._client.alive()
            hint = self._start_error or ("" if alive else "iniciando o Hypria...")
            return [ProviderInfo("hypria", "Hypria", alive, hint,
                                 [ModelInfo(m) for m in
                                  ([options.get("model")] if options else [])
                                  if m])]
        result = []
        for prov in options.get("providers") or []:
            slug = str(prov.get("slug") or "")
            if not slug:
                continue
            # O inventario real manda os modelos como strings simples;
            # payloads mais ricos mandam dicts {id, name, description}.
            models = []
            for m in prov.get("models") or []:
                if isinstance(m, str):
                    if m:
                        models.append(ModelInfo(m))
                elif isinstance(m, dict) and m.get("id"):
                    models.append(ModelInfo(str(m["id"]),
                                            str(m.get("name") or m["id"]),
                                            str(m.get("description") or "")))
            authenticated = bool(prov.get("authenticated"))
            hint = str(prov.get("warning") or "")
            if not authenticated and not hint:
                hint = "sem chave (use /key %s <valor>)" % slug
            result.append(ProviderInfo(slug, str(prov.get("name") or slug),
                                       authenticated, hint, models))
        # O provider corrente da sessao pode nao estar no inventario (ex.:
        # "ollama", que e um alias de custom). Sem esta linha o picker
        # mostraria outro provider como ativo.
        atual = str(options.get("provider") or "")
        if atual and all(p.id != atual for p in result):
            modelo = str(options.get("model") or "")
            result.insert(0, ProviderInfo(
                atual, atual, True, "",
                [ModelInfo(modelo)] if modelo else []))
        return result

    def get_provider(self, pid):
        for prov in self.list_providers():
            if prov.id == pid:
                return prov
        return None

    def models(self, pid):
        prov = self.get_provider(pid)
        return list(prov.models) if prov is not None else []

    def default_model(self, pid):
        with self._options_lock:
            options = self._options or {}
        if options.get("provider") == pid and options.get("model"):
            return str(options["model"])
        modelos = self.models(pid)
        return modelos[0].id if modelos else ""

    def first_available(self):
        with self._options_lock:
            options = self._options or {}
        if options.get("provider"):
            return str(options["provider"])
        providers = self.list_providers()
        for prov in providers:
            if prov.available:
                return prov.id
        return providers[0].id if providers else None

    def api_key(self, pid):
        return ""                       # chaves moram no ~/.hypr-ia/.env

    def set_api_key(self, pid, value):
        self.ensure_started()
        self._client.request("model.save_key", {"slug": pid, "api_key": value})
        with self._options_lock:
            self._options = None        # forca re-fetch no proximo refresh

    def refresh_async(self, done_callback=None):
        """Sobe o gateway (se preciso) e recarrega o inventario de modelos."""
        def run():
            try:
                self.ensure_started()
                options = self._client.request("model.options",
                                               {"refresh": True})
                with self._options_lock:
                    self._options = options
            except Exception as exc:
                self._start_error = str(exc)
            if done_callback is not None:
                done_callback()

        thread = threading.Thread(target=run, name="hyde-ai-probe", daemon=True)
        thread.start()
        return thread

    # -- controle do turno ------------------------------------------------

    def new_cancel(self):
        return HypriaCancel(self._interrupt_current)

    # Leituras de self._sid abaixo sao atomicas sob o GIL; sem lock de
    # proposito — estes metodos rodam na thread do GTK e nunca podem
    # esperar por uma RPC em andamento no ensure_session.
    def _interrupt_current(self):
        sid = self._sid
        if sid:
            self._client.request_async("session.interrupt", {"session_id": sid})

    def respond_approval(self, choice, request_id, on_error=None):
        self._client.request_async(
            "approval.respond",
            {"session_id": self._sid, "choice": choice,
             "request_id": request_id},
            on_error=on_error)

    def respond_clarify(self, answer, request_id, question_id="",
                        on_error=None):
        params = {"session_id": self._sid, "answer": answer,
                  "request_id": request_id}
        if question_id:
            params["question_id"] = question_id
        self._client.request_async("clarify.respond", params,
                                   on_error=on_error)

    def set_model(self, model_id, provider_slug=""):
        """Troca o modelo da sessao atual; devolve o warning (ou "")."""
        sid = self.ensure_session()
        valor = model_id
        if provider_slug:
            valor += " --provider %s" % provider_slug
        valor += " --session"
        params = {"session_id": sid, "key": "model", "value": valor}
        resp = self._client.request("config.set", params)
        if resp.get("confirm_required"):
            # O gateway segurou a troca (modelo caro / politica) e NAO
            # trocou nada; o sidebar nao tem dialogo de confirmacao, entao
            # confirma explicitamente — o aviso segue como banner.
            resp2 = self._client.request(
                "config.set", dict(params, confirm_expensive_model=True))
            if not resp.get("warning"):
                resp = resp2
        self._config.set("hypria.model", model_id)
        if provider_slug:
            self._config.set("hypria.provider", provider_slug)
        try:
            self._config.save()
        except Exception:
            pass
        with self._options_lock:
            if self._options is not None:
                self._options = dict(self._options,
                                     model=model_id,
                                     provider=provider_slug
                                     or self._options.get("provider"))
        return str(resp.get("warning") or "")

    # -- modo agente / raciocinio ----------------------------------------

    EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max",
               "ultra")

    def list_toolsets(self):
        """[{name, description, tool_count, enabled}] direto do gateway."""
        self.ensure_started()
        params = {}
        if self._sid:
            params["session_id"] = self._sid
        resp = self._client.request("toolsets.list", params)
        return list(resp.get("toolsets") or [])

    def agent_mode(self):
        """Espelho local do modo agente (o gateway e a fonte da verdade)."""
        return bool(self._config.get("hypria.agent_mode", True))

    def set_agent_mode(self, ligado):
        """Liga/desliga todos os toolsets do Hypria (RPC sincrona; worker).

        Desligar guarda quais toolsets estavam ativos e desativa todos —
        o turno vira conversa pura, sem ferramentas. Ligar reativa o que
        estava ativo antes (ou tudo, se nao ha snapshot). O gateway
        persiste no config.yaml e recria o agente da sessao na hora.
        """
        self.ensure_started()
        itens = self.list_toolsets()
        todos = [str(t.get("name") or "") for t in itens if t.get("name")]
        ativos = [str(t.get("name")) for t in itens if t.get("enabled")]
        params = {}
        if self._sid:
            params["session_id"] = self._sid
        if ligado:
            antes = [n for n in (self._config.get("hypria.toolsets_antes")
                                 or []) if n in todos]
            nomes = antes or todos
        else:
            if ativos:
                self._config.set("hypria.toolsets_antes", ativos)
            nomes = todos
        if nomes:
            self._client.request("tools.configure", dict(
                params, action="enable" if ligado else "disable",
                names=nomes))
        self._config.set("hypria.agent_mode", bool(ligado))
        try:
            self._config.save()
        except Exception:
            pass
        return bool(ligado)

    def set_reasoning(self, esforco):
        """Esforco de raciocinio, aplicado na sessao atual ao vivo.

        "" limpa a escolha local: a sessao atual segue como esta e as
        proximas usam o padrao do Hypria (config.set nao aceita vazio).
        """
        esforco = str(esforco or "").strip().lower()
        if esforco:
            sid = self.ensure_session()
            self._client.request("config.set", {
                "session_id": sid, "key": "reasoning", "value": esforco})
        self._config.set("hypria.reasoning_effort", esforco)
        try:
            self._config.save()
        except Exception:
            pass

    def slash_exec(self, command):
        """Executa um slash do Hypria; devolve um dict tipado.

        ``{"type": "output", "output": str}`` para texto simples;
        ``{"type": "send"|"skill", "message": str, "display": str}``
        quando o comando vira um prompt a submeter;
        ``{"type": "alias", "target": str}`` quando e apelido de outro.
        """
        sid = self.ensure_session()
        try:
            resp = self._client.request("slash.exec", {"session_id": sid,
                                                       "command": command})
        except HypriaRpcError as exc:
            if exc.code != 4018:
                raise
            # slash.exec recusa comandos que mudam estado (skills,
            # bundles, snapshot restore) e manda usar command.dispatch.
            partes = command.lstrip("/").split(None, 1)
            resp = self._client.request("command.dispatch", {
                "session_id": sid,
                "name": partes[0] if partes else "",
                "arg": partes[1] if len(partes) > 1 else ""})
        if not isinstance(resp, dict):
            return {"type": "output", "output": str(resp or "")}
        if resp.get("type"):
            return resp                 # ja tipado (command.dispatch)
        return {"type": "output", "output": str(resp.get("output") or "")}

    def commands_catalog(self):
        try:
            self.ensure_started()
            resp = self._client.request("commands.catalog", {})
            return list(resp.get("pairs") or [])
        except Exception:
            return []

    # -- streaming --------------------------------------------------------

    def stream(self, pid, model_id, messages, system, cancel):
        for ev in self.stream_events(pid, model_id, messages, system, cancel):
            if ev.kind == "text" and ev.text:
                yield ev.text

    def stream_events(self, pid, model_id, messages, system, cancel, tools=None):
        texto = _ultimo_texto_user(messages)
        if not texto.strip():
            raise HypriaError("mensagem vazia")
        sid = self.ensure_session()
        fila = self._client.open_turn(sid)
        try:
            resp = self._client.request("prompt.submit",
                                        {"session_id": sid, "text": texto})
        except Exception:
            self._client.close_turn(sid)
            raise
        if resp.get("voice_stopped"):
            # O submit so parou a leitura por voz; nao ha turno novo.
            self._client.close_turn(sid)
            return iter(())
        # "queued": ja havia um turno rodando e o texto entrou na fila —
        # os frames que chegam primeiro sao do turno ANTIGO. Engole tudo
        # ate o primeiro message.complete e so entao streama o nosso.
        enfileirado = str(resp.get("status") or "") == "queued"
        return self._traduzir(sid, fila, cancel, pular_turno=enfileirado)

    def _traduzir(self, sid, fila, cancel, pular_turno=False):
        """Gerador: eventos do gateway -> eventos duck-typed do sidebar."""
        mostrar_think = bool(self._config.get("hypria.show_thinking", True))
        viu_texto = False
        prazo_drenagem = None
        try:
            while True:
                if prazo_drenagem is None and _cancelado(cancel):
                    prazo_drenagem = time.time() + _DRAIN_CAP_S
                if prazo_drenagem is not None and time.time() > prazo_drenagem:
                    return              # interrupt-ack nunca veio; desiste
                try:
                    ev = fila.get(timeout=1.0)
                except queue.Empty:
                    continue
                tipo = ev.get("type") or ""
                payload = ev.get("payload") or {}

                if pular_turno:
                    # Sobra do turno VELHO na fila. O nosso turno enfileirado
                    # comeca no message.start dele; o complete do velho tambem
                    # encerra o pulo — o que vier primeiro, porque o complete
                    # do velho pode ja ter sido consumido pelo gerador
                    # anterior antes desta fila abrir (o gateway responde
                    # "queued" ate limpar o running, que vem depois do
                    # complete).
                    if tipo == "message.start":
                        pular_turno = False
                        continue
                    if tipo == "message.complete":
                        # O complete sintetico de queda do gateway e um erro
                        # de verdade, nao o fim do turno velho.
                        if ev.get("synthetic") or \
                                str(payload.get("status") or "") == "error":
                            raise HypriaError(str(payload.get("error")
                                                  or "erro no Hypria"))
                        pular_turno = False
                        continue
                    if tipo not in ("approval.request", "approval.expire",
                                    "clarify.request", "clarify.expire",
                                    "sudo.request", "secret.request",
                                    "status.update"):
                        continue
                    # requests interativos do turno velho seguem o fluxo
                    # normal: sem resposta ele fica bloqueado e o nosso
                    # turno enfileirado nunca comeca
                if tipo == "message.delta":
                    viu_texto = True
                    yield Event("text", str(payload.get("text") or ""))
                elif tipo in ("reasoning.delta", "thinking.delta"):
                    if mostrar_think:
                        yield Event("thinking", str(payload.get("text") or ""))
                elif tipo == "message.interim":
                    if not payload.get("already_streamed"):
                        viu_texto = True
                        yield Event("text", str(payload.get("text") or ""))
                elif tipo == "message.complete":
                    if str(payload.get("status") or "ok") == "error":
                        raise HypriaError(str(payload.get("error")
                                              or "erro no Hypria"))
                    if not viu_texto and payload.get("text"):
                        yield Event("text", str(payload["text"]))
                    dados = {"done_reason": payload.get("done_reason") or "stop"}
                    if isinstance(payload.get("usage"), dict):
                        dados.update(payload["usage"])
                    yield Event("usage", data=dados)
                    return
                elif tipo == "tool.start":
                    yield Event("tool_start", data=payload)
                elif tipo == "tool.generating":
                    yield Event("status", data={"text": "preparando %s..."
                                                % (payload.get("name") or "tool")})
                elif tipo == "tool.complete":
                    yield Event("tool_done", data=payload)
                elif tipo == "approval.request":
                    yield Event("approval", data=payload)
                elif tipo == "approval.expire":
                    yield Event("approval_expire", data=payload)
                elif tipo == "clarify.expire":
                    yield Event("clarify_expire", data=payload)
                elif tipo == "clarify.request":
                    perguntas = payload.get("questions")
                    if isinstance(perguntas, list) and perguntas:
                        # AskUserQuestion em lote: um card por pergunta,
                        # todos respondendo ao mesmo request_id via qid.
                        for p in perguntas:
                            if not isinstance(p, dict):
                                continue
                            yield Event("clarify", data={
                                "request_id": payload.get("request_id"),
                                "question_id": p.get("qid") or "",
                                "question": p.get("question") or "",
                                "choices": p.get("choices") or [],
                                "multi_select": bool(p.get("multi_select")),
                            })
                    else:
                        yield Event("clarify", data=payload)
                elif tipo == "sudo.request":
                    self._client.request_async(
                        "sudo.respond",
                        {"session_id": sid, "password": "",
                         "request_id": payload.get("request_id")})
                    yield Event("status", data={
                        "text": "pedido de sudo negado (sem suporte no sidebar)"})
                elif tipo == "secret.request":
                    self._client.request_async(
                        "secret.respond",
                        {"session_id": sid, "value": "",
                         "request_id": payload.get("request_id")})
                    yield Event("status", data={
                        "text": "pedido de segredo negado (sem suporte no sidebar)"})
                elif tipo == "status.update":
                    yield Event("status", data=payload)
                elif tipo == "session.info":
                    self._session_info = dict(payload)
                    yield Event("meta", data=payload)
                elif tipo.startswith("subagent."):
                    nome = payload.get("goal") or payload.get("tool_name") or ""
                    yield Event("status", data={"text": ("subagente %s" % nome).strip()})
                elif tipo == "error":
                    yield Event("status", data={"text": str(payload.get("message")
                                                            or "erro")})
                elif tipo in ("session.title", "notification.show",
                              "notification.clear"):
                    self._emit_ui(ev)   # interface cuida fora do turno
                # message.start, session.usage, etc.: sem acao
        finally:
            self._client.close_turn(sid)
