#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hyde-ai :: application entrypoint.

Wires together ``config.py`` (settings), ``hypria_registry.py`` (the Hypria
gateway backend) and ``sidebar.py`` (the GTK4 layer-shell UI), and implements
the single-instance toggle semantics.

Invocation
----------
    hyde-ai                 toggle the panel (default)
    hyde-ai --show          slide in
    hyde-ai --hide          slide out
    hyde-ai --toggle        explicit toggle
    hyde-ai --new           new conversation, then show
    hyde-ai --daemon        start hidden (for autostart)
    hyde-ai --quit          terminate the running instance

The same verbs are exported as GActions, so they also work over D-Bus:

    gapplication action dev.hyde.HydeAi toggle

Everything here is defensive about its collaborators: if ``config.py`` or the
Hypria backend is missing or unusable, the app still starts and says so in the
UI rather than dying with a traceback.
"""

# --------------------------------------------------------------------------
# gtk4-layer-shell must be dlopen'd before GTK pulls in libwayland-client,
# otherwise the surface silently degrades to an ordinary toplevel window.
# This has to stay above every other import.
# --------------------------------------------------------------------------
import ctypes as _ctypes

try:
    _ctypes.CDLL("libgtk4-layer-shell.so.0", mode=_ctypes.RTLD_GLOBAL)
except OSError:  # pragma: no cover
    pass

import io
import os
import sys
import json
import time
import uuid
import signal
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, Gio, GLib, Adw  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import sidebar as sidebar_mod  # noqa: E402
from sidebar import Sidebar, ThemeManager  # noqa: E402

APP_ID = "dev.hyde.HydeAi"
VERSION = "1.0.0"


def _warn(msg):
    print("[hyde-ai] %s" % msg, file=sys.stderr, flush=True)


def _xdg(var, fallback):
    value = (os.environ.get(var) or "").strip()
    if value and os.path.isabs(value):
        return value
    return os.path.expanduser(fallback)


# --------------------------------------------------------------------------
# Paths.  config.py is the single source of truth when it is importable, so
# every module agrees on where things live.
# --------------------------------------------------------------------------

try:
    import config as config_mod
except Exception as _exc:  # pragma: no cover - config.py is a sibling file
    config_mod = None
    if not isinstance(_exc, ImportError):
        _warn("config.py failed to import: %r" % (_exc,))

CONFIG_DIR = getattr(config_mod, "CONFIG_DIR", None) or os.path.join(
    _xdg("XDG_CONFIG_HOME", "~/.config"), "hyde-ai")
CONFIG_PATH = getattr(config_mod, "CONFIG_PATH", None) or os.path.join(CONFIG_DIR, "config.json")
STATE_DIR = getattr(config_mod, "STATE_DIR", None) or os.path.join(
    _xdg("XDG_STATE_HOME", "~/.local/state"), "hyde-ai")
HISTORY_PATH = os.path.join(STATE_DIR, "history.json")
CSS_PATH = getattr(config_mod, "WALLBASH_CSS", None) or sidebar_mod.CSS_PATH

MAX_ARCHIVED_CONVERSATIONS = 40


def _atomic_write_json(path, payload, mode=0o600):
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError as exc:
        _warn("cannot create %s: %s" % (directory, exc))
        return False
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        return True
    except OSError as exc:
        _warn("cannot write %s: %s" % (path, exc))
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


# ==========================================================================
# Config
# ==========================================================================

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant embedded in a desktop sidebar on Arch Linux "
    "running Hyprland. Be concise and conversational: lead with the answer, "
    "then add only the detail that earns its place. Use GitHub-flavoured "
    "Markdown, and always put code in fenced blocks with a language tag."
)

DEFAULT_CONFIG = {
    "version": 1,
    "provider": "",
    "models": {},
    "api_keys": {},
    "max_tokens": 4096,
    "temperature": None,
    "request": {"connect_timeout": 15.0, "read_timeout": 180.0},
    "chat": {
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "max_history_messages": 40,
    },
    "sidebar": {
        "namespace": "hyde-ai",
        "edge": "right",
        "width_fraction": 0.30,
        "width_min": 420,
        "width_max": 900,
    },
    "ui": {
        "follow_stream": True,
        "restore_last_session": True,
        "code_line_numbers": True,
        "code_style_scheme": "auto",
    },
}


def _deep_merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


class _JsonConfig(object):
    """Dotted-key JSON store used when ``config.py`` is unavailable.

    It mirrors ``config.Config``'s surface (``get`` / ``set`` / ``save``)
    so the Hypria backend and the sidebar accept it unchanged.
    """

    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self.data = json.loads(json.dumps(DEFAULT_CONFIG))
        self.load()

    def load(self):
        try:
            with io.open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                _deep_merge(self.data, loaded)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            _warn("config unreadable (%s); using defaults" % exc)

    # -- dotted access ---------------------------------------------------
    def get(self, dotted, default=None):
        node = self.data
        for part in str(dotted).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted, value):
        parts = str(dotted).split(".")
        node = self.data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    def __contains__(self, dotted):
        sentinel = object()
        return self.get(dotted, sentinel) is not sentinel

    # -- provider-facing surface ----------------------------------------
    def api_key(self, provider_id):
        value = self.get("api_keys.%s" % provider_id, "")
        return value.strip() if isinstance(value, str) else ""

    def get_api_key(self, provider_id):
        return self.api_key(provider_id)

    def set_api_key(self, provider_id, key):
        self.set("api_keys.%s" % provider_id, (key or "").strip())

    def model_for(self, provider_id, default=""):
        value = self.get("models.%s" % provider_id, "")
        return value.strip() if isinstance(value, str) and value.strip() else default

    def set_model_for(self, provider_id, model):
        self.set("models.%s" % provider_id, model or "")

    def system_prompt(self):
        value = self.get("chat.system_prompt", None)
        if not isinstance(value, str) or not value.strip():
            value = self.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
        return value if isinstance(value, str) else DEFAULT_SYSTEM_PROMPT

    def timeouts(self):
        connect = self.get("request.connect_timeout", 15.0)
        read = self.get("request.read_timeout", 180.0)
        try:
            connect = float(connect)
        except (TypeError, ValueError):
            connect = 15.0
        try:
            read = float(read)
        except (TypeError, ValueError):
            read = 180.0
        return (max(1.0, connect), max(1.0, read))

    def save(self):
        return _atomic_write_json(self.path, self.data, 0o600)


class ConfigProxy(object):
    """Normalises ``config.py`` and guarantees the keys the UI needs exist.

    Unknown attributes are delegated to the wrapped object, which is what lets
    ``providers._as_dict`` recognise this proxy as a real config (it checks for
    both ``get`` and ``api_key``).
    """

    def __init__(self, inner, fallback):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_fallback", fallback)

    # -- delegation ------------------------------------------------------
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        inner = object.__getattribute__(self, "_inner")
        try:
            return getattr(inner, name)
        except AttributeError:
            fallback = object.__getattribute__(self, "_fallback")
            return getattr(fallback, name)

    def unwrap(self):
        return self._inner

    # -- explicit surface ------------------------------------------------
    def get(self, key, default=None):
        getter = getattr(self._inner, "get", None)
        if callable(getter):
            try:
                sentinel = object()
                value = getter(key, sentinel)
                if value is not sentinel and value is not None:
                    return value
            except TypeError:
                pass
            except Exception as exc:
                _warn("config.get(%r) failed: %r" % (key, exc))
        return self._fallback.get(key, default)

    def set(self, key, value):
        setter = getattr(self._inner, "set", None)
        if callable(setter):
            try:
                setter(key, value)
            except Exception as exc:
                _warn("config.set(%r) failed: %r" % (key, exc))
        self._fallback.set(key, value)

    def save(self):
        saver = getattr(self._inner, "save", None)
        ok = True
        if callable(saver):
            try:
                saver()
            except Exception as exc:
                _warn("config.save() failed: %r" % (exc,))
                ok = False
        if not ok:
            return self._fallback.save()
        return True

    def api_key(self, provider_id):
        getter = getattr(self._inner, "api_key", None) or getattr(self._inner, "get_api_key", None)
        if callable(getter):
            try:
                value = getter(provider_id)
                if value:
                    return value
            except Exception as exc:
                _warn("config.api_key failed: %r" % (exc,))
        return self._fallback.api_key(provider_id)

    def get_api_key(self, provider_id):
        return self.api_key(provider_id)

    def set_api_key(self, provider_id, value):
        setter = getattr(self._inner, "set_api_key", None)
        if callable(setter):
            try:
                setter(provider_id, value)
                return
            except Exception as exc:
                _warn("config.set_api_key failed: %r" % (exc,))
        self._fallback.set_api_key(provider_id, value)

    def set_model_for(self, provider_id, model):
        setter = getattr(self._inner, "set_model_for", None)
        if callable(setter):
            try:
                setter(provider_id, model)
                return
            except Exception as exc:
                _warn("config.set_model_for failed: %r" % (exc,))
        self._fallback.set_model_for(provider_id, model)


def build_config():
    fallback = _JsonConfig(CONFIG_PATH)
    if config_mod is None:
        return fallback

    inner = None
    for attr, args in (
        ("load", (CONFIG_PATH,)), ("load", ()),
        ("load_config", ()), ("get_config", ()),
        ("Config", ()), ("AppConfig", ()),
    ):
        factory = getattr(config_mod, attr, None)
        if factory is None:
            continue
        try:
            inner = factory(*args)
        except TypeError:
            continue
        except Exception as exc:
            _warn("config.%s failed: %r" % (attr, exc))
            continue
        if inner is not None:
            break

    if inner is None:
        inner = getattr(config_mod, "CONFIG", None) or getattr(config_mod, "config", None)
    if inner is None or not hasattr(inner, "get"):
        return fallback
    return ConfigProxy(inner, fallback)


# ==========================================================================
# Providers
# ==========================================================================


class EmptyRegistry(object):
    """Stand-in used when the Hypria backend cannot be constructed."""

    def __init__(self, reason):
        self.reason = reason

    def list_providers(self):
        return []

    def get_provider(self, pid):
        return None

    def models(self, pid):
        return []

    def default_model(self, pid):
        return ""

    def first_available(self):
        return None

    def api_key(self, pid):
        return ""

    def set_api_key(self, pid, value):
        raise RuntimeError(self.reason)

    def refresh_async(self, done_callback=None):
        if done_callback is not None:
            done_callback()
        return None

    def new_cancel(self):
        return threading.Event()

    def stream(self, pid, model_id, messages, system, cancel):
        raise RuntimeError(self.reason)

    def stream_events(self, pid, model_id, messages, system, cancel, tools=None):
        raise RuntimeError(self.reason)


def build_registry(config):
    """O backend agora e 100% Hypria: gateway em processo separado.

    A construcao nao spawna nada -- o gateway sobe no primeiro
    ``refresh_async`` (chamado em do_startup), fora do main loop.
    """
    try:
        import hypria_registry
        return hypria_registry.HypriaRegistry.from_config(config)
    except Exception as exc:
        reason = "backend Hypria indisponivel (%s)" % exc
        _warn(reason)
        return EmptyRegistry(reason)


# ==========================================================================
# Conversation history
# ==========================================================================

class History(object):
    """Persisted conversation, stored at ~/.local/state/hyde-ai/history.json.

    Unlike the shell this is modelled on, the last session *is* restored on
    start; ``new_conversation`` archives the old thread instead of dropping it.
    """

    def __init__(self, path=HISTORY_PATH, config=None):
        self.path = path
        self._config = config
        self.data = {"version": 1, "current": None, "archive": []}
        self.load()

    # -- persistence -----------------------------------------------------
    def load(self):
        try:
            with io.open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data = loaded
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            _warn("history unreadable (%s); starting fresh" % exc)

        self.data.setdefault("version", 1)
        if not isinstance(self.data.get("archive"), list):
            self.data["archive"] = []

        current = self.data.get("current")
        if not isinstance(current, dict):
            current = self._blank_conversation()
            self.data["current"] = current
        current.setdefault("id", uuid.uuid4().hex)
        current.setdefault("created", time.time())
        current.setdefault("provider", "")
        current.setdefault("model", "")
        if not isinstance(current.get("messages"), list):
            current["messages"] = []
        clean = []
        for message in current["messages"]:
            if not isinstance(message, dict):
                continue
            message.setdefault("id", uuid.uuid4().hex)
            message.setdefault("role", "assistant")
            message.setdefault("content", "")
            clean.append(message)
        current["messages"] = clean

    def save(self):
        return _atomic_write_json(self.path, self.data, 0o600)

    # -- accessors -------------------------------------------------------
    @staticmethod
    def _blank_conversation(provider="", model=""):
        return {
            "id": uuid.uuid4().hex,
            "created": time.time(),
            "provider": provider or "",
            "model": model or "",
            "messages": [],
        }

    @property
    def current(self):
        return self.data["current"]

    @property
    def messages(self):
        return self.current["messages"]

    @property
    def provider(self):
        return self.current.get("provider") or ""

    @property
    def model(self):
        return self.current.get("model") or ""

    def set_model(self, provider, model):
        if provider is not None:
            self.current["provider"] = provider or ""
        self.current["model"] = model or ""
        config = self._config
        if config is None:
            return
        try:
            config.set("provider", self.current["provider"])
            setter = getattr(config, "set_model_for", None)
            if callable(setter) and self.current["provider"]:
                setter(self.current["provider"], self.current["model"])
        except Exception as exc:
            _warn("could not persist model selection: %r" % (exc,))

    # -- mutation --------------------------------------------------------
    def add(self, role, content, name=None):
        record = {
            "id": uuid.uuid4().hex,
            "role": role,
            "content": content or "",
            "ts": time.time(),
        }
        if name:
            record["name"] = name
        if role == "assistant":
            record["provider"] = self.provider
            record["model"] = self.model
        self.messages.append(record)
        return record

    def replace_content(self, msg_id, text):
        """Regrava o conteudo da mensagem ``msg_id`` (stream em andamento).

        Enderecada por id de proposito: "a ultima mensagem" muda embaixo
        do stream quando o usuario troca de conversa no meio do turno.
        """
        for m in self.messages:
            if m.get("id") == msg_id:
                m["content"] = text
                return True
        return False

    def set_metricas(self, msg_id, espera, geracao, vel):
        """Guarda tempo e velocidade DAQUELA resposta junto da mensagem.

        Sem isto os numeros se perdiam ao recarregar a conversa: eram apenas
        estado de tela, nao dado.
        """
        for m in self.messages:
            if m.get("id") == msg_id:
                m["metricas"] = {
                    "espera": round(espera, 3),
                    "geracao": round(geracao, 3),
                    "tps": round(vel, 2) if vel else None,
                }
                return True
        return False

    def delete(self, msg_id):
        if not msg_id:
            return
        self.current["messages"] = [m for m in self.messages if m.get("id") != msg_id]

    def truncate_from(self, msg_id):
        """Drop the message with ``msg_id`` and everything after it."""
        if not msg_id:
            return
        for index, message in enumerate(self.messages):
            if message.get("id") == msg_id:
                del self.messages[index:]
                return

    # -- navegacao pelo historico ---------------------------------------
    def titulo(self, conversa):
        """Primeira fala do usuario serve de titulo, como nos fronts de chat."""
        for m in conversa.get("messages", []):
            if m.get("role") == "user":
                t = " ".join((m.get("content") or "").split())
                return (t[:52] + "...") if len(t) > 52 else (t or "sem titulo")
        return "conversa vazia"

    def conversas(self, provider=""):
        """Lista conversas (atual primeiro), opcionalmente de um provedor."""
        itens = []
        atual = self.data.get("current") or {}
        for c in [atual] + list(reversed(self.data.get("archive", []))):
            if not c or not c.get("messages"):
                continue
            if provider and c.get("provider") != provider:
                continue
            itens.append({
                "id": c.get("id", ""),
                "titulo": self.titulo(c),
                "provider": c.get("provider", ""),
                "model": c.get("model", ""),
                "quando": c.get("created", 0),
                "n": len(c.get("messages", [])),
                "atual": c is atual,
            })
        return itens

    def abrir(self, conv_id):
        """Traz uma conversa do arquivo de volta para a atual."""
        atual = self.data.get("current") or {}
        if atual.get("id") == conv_id:
            return True
        arquivo = self.data.setdefault("archive", [])
        for i, c in enumerate(arquivo):
            if c.get("id") == conv_id:
                # a atual volta para o arquivo, para nao se perder na troca
                if atual.get("messages"):
                    arquivo.append(atual)
                self.data["current"] = arquivo.pop(i)
                del arquivo[:-MAX_ARCHIVED_CONVERSATIONS]
                return True
        return False

    def apagar(self, conv_id):
        arquivo = self.data.setdefault("archive", [])
        for i, c in enumerate(arquivo):
            if c.get("id") == conv_id:
                del arquivo[i]
                return True
        return False

    def new_conversation(self, provider="", model=""):
        current = self.current
        if current.get("messages"):
            archive = self.data.setdefault("archive", [])
            archive.append(current)
            del archive[:-MAX_ARCHIVED_CONVERSATIONS]
        self.data["current"] = self._blank_conversation(
            provider or current.get("provider", ""),
            model or current.get("model", ""),
        )

    # -- vinculo com a sessao do Hypria ----------------------------------
    # O transcript de verdade mora no SQLite do Hypria; o cache local guarda
    # a chave duravel da sessao para a conversa poder continuar de onde
    # parou depois de um restore ou restart.
    def set_hypria_session(self, stored_id):
        self.current["hypria_session"] = str(stored_id or "")

    def hypria_session(self):
        return str(self.current.get("hypria_session") or "")


# ==========================================================================
# Application
# ==========================================================================

class HydeAiApplication(Adw.Application):

    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.window = None
        self.config = None
        self.registry = None
        self.history = None
        self.theme = None

    # -- lifecycle -------------------------------------------------------
    def do_startup(self):
        Adw.Application.do_startup(self)

        for directory in (CONFIG_DIR, STATE_DIR, os.path.dirname(CSS_PATH)):
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as exc:
                _warn("cannot create %s: %s" % (directory, exc))

        self.config = build_config()
        self.registry = build_registry(self.config)
        self.history = History(HISTORY_PATH, self.config)
        self.theme = ThemeManager(Gdk.Display.get_default(), CSS_PATH)

        if not self.history.provider:
            provider_id = self.config.get("provider", "") or ""
            if not provider_id:
                provider_id = self.registry.first_available() or ""
            model_id = ""
            if provider_id:
                getter = getattr(self.config, "model_for", None)
                if callable(getter):
                    try:
                        model_id = getter(provider_id, "") or ""
                    except Exception:
                        model_id = ""
                if not model_id:
                    model_id = self.registry.default_model(provider_id) or ""
            self.history.set_model(provider_id, model_id)

        for name, verb in (
            ("toggle", "toggle"), ("show", "show"), ("hide", "hide"),
            ("new-chat", "new"), ("quit", "quit"),
            ("reload-theme", "reload-theme"),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", self._make_action_handler(verb))
            self.add_action(action)

        self._install_signal_handlers()

        # Keep the process alive while the panel is slid off-screen.
        self.hold()

        # Ollama (and anything else needing a probe) can only report
        # availability after network I/O, so do it off the main loop.
        self.registry.refresh_async(self._on_probe_done)

    def do_activate(self):
        self._dispatch("toggle")

    def do_command_line(self, command_line):
        argv = command_line.get_arguments() or []
        verb = "toggle"
        payload = None
        rest = list(argv[1:])
        if rest and rest[0].strip().lower() in ("--ask", "ask"):
            payload = " ".join(rest[1:]).strip()
            if not payload:
                command_line.printerr_literal("hyde-ai: --ask needs a question\n")
                return 2
            self._dispatch("ask", payload)
            return 0
        for arg in rest:
            arg = arg.strip().lower()
            if arg in ("--toggle", "-t", "toggle"):
                verb = "toggle"
            elif arg in ("--show", "-s", "show", "--open"):
                verb = "show"
            elif arg in ("--hide", "hide", "--close"):
                verb = "hide"
            elif arg in ("--new", "--new-chat", "new"):
                verb = "new"
            elif arg in ("--daemon", "-d", "daemon", "--background"):
                verb = "daemon"
            elif arg in ("--quit", "-q", "quit", "--exit"):
                verb = "quit"
            elif arg in ("--help", "-h", "help"):
                command_line.print_literal((__doc__ or "").strip() + "\n")
                return 0
            elif arg in ("--version", "-v", "--v"):
                command_line.print_literal("hyde-ai %s\n" % VERSION)
                return 0
            else:
                command_line.printerr_literal("hyde-ai: unknown argument %r\n" % arg)
                return 2
        self._dispatch(verb)
        return 0

    def do_shutdown(self):
        if self.window is not None:
            try:
                self.window.shutdown()
            except Exception as exc:
                _warn("shutdown failed: %r" % (exc,))
        if self.registry is not None:
            closer = getattr(self.registry, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as exc:
                    _warn("hypria close failed: %r" % (exc,))
        if self.history is not None:
            try:
                self.history.save()
            except Exception:
                pass
        if self.config is not None:
            try:
                self.config.save()
            except Exception:
                pass
        Adw.Application.do_shutdown(self)

    # -- helpers ---------------------------------------------------------
    def _make_action_handler(self, verb):
        def handler(_action, _param):
            self._dispatch(verb)
        return handler

    def _install_signal_handlers(self):
        def stop():
            self.quit()
            return GLib.SOURCE_REMOVE

        try:
            from gi.repository import GLibUnix
            adder = GLibUnix.signal_add
        except Exception:
            adder = getattr(GLib, "unix_signal_add", None)
        if adder is None:  # pragma: no cover
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                adder(GLib.PRIORITY_DEFAULT, sig, stop)
            except Exception:  # pragma: no cover
                pass

    def _on_probe_done(self):
        """Called from the probe thread -- marshal back to the main loop."""
        GLib.idle_add(self._apply_probe_result)

    def _apply_probe_result(self):
        if self.window is not None:
            try:
                self.window.refresh_providers()
            except Exception as exc:
                _warn("refresh_providers failed: %r" % (exc,))
        return GLib.SOURCE_REMOVE

    def _ensure_window(self):
        if self.window is None:
            self.window = Sidebar(
                self,
                config=self.config,
                registry=self.registry,
                history=self.history,
                theme=self.theme,
            )
        return self.window

    def _dispatch(self, verb, payload=None):
        if verb == "quit":
            self.quit()
            return
        if verb == "reload-theme":
            # Must run BEFORE _ensure_window so a wallbash re-render never
            # maps the panel; the hook fires on every theme switch.
            if getattr(self, "theme", None) is not None:
                try:
                    self.theme.reload()
                except Exception:
                    pass
            if getattr(self, "window", None) is not None:
                handler = getattr(self.window, "_on_theme_reload", None)
                if callable(handler):
                    try:
                        handler()
                    except Exception:
                        pass
            return
        window = self._ensure_window()
        if verb == "toggle":
            window.toggle()
        elif verb == "show":
            window.show_panel()
        elif verb == "hide":
            window.hide_panel()
        elif verb == "new":
            window.new_conversation()
            window.show_panel()
        elif verb == "ask":
            window.show_panel()
            window.ask(payload)
        elif verb == "daemon":
            pass          # the window exists but stays hidden


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if not sidebar_mod.LAYER_SHELL_OK:
        _warn("gtk4-layer-shell is not usable; the panel will be a normal window")
    app = HydeAiApplication()
    return app.run(argv)


if __name__ == "__main__":
    sys.exit(main())
