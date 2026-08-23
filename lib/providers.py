"""
hyde-ai :: provider layer
=========================

Streaming chat against Anthropic Claude, Google Gemini, OpenAI, and a local
Ollama daemon, plus a config-driven escape hatch for any endpoint speaking
the OpenAI or Ollama wire format.

Standard library only -- HTTP is ``urllib.request``. ``resp.read1()`` on a
``http.client.HTTPResponse`` returns as soon as bytes are available (verified:
chunks emitted 250 ms apart arrive 250 ms apart), which gives us true
incremental streaming without pulling in ``requests``.

Public surface
--------------
    ProviderError                  -- the only exception this module raises
    CancelToken                    -- cooperative stop for an in-flight stream
    Event(kind, text, data)        -- typed stream event
    Provider                       -- abstract base
        .id / .name
        .models          -> list[str]
        .default_model   -> str
        .available()     -> bool
        .unavailable_reason() -> str | None
        .stream(messages, model, system, cancel=None) -> Iterator[str]
        .stream_events(...)                           -> Iterator[Event]
    all_providers(config) -> list[Provider]

``stream()`` yields plain text deltas and is what the chat view consumes.
``stream_events()`` is the richer form: it additionally surfaces reasoning
("thinking") deltas and a final usage record, so the UI can render collapsible
think blocks and a token counter without a second request.

Threading
---------
Nothing here touches GTK. Run a stream on a worker thread and marshal each
delta back with ``GLib.idle_add``. Every generator is safe to abandon: closing
it, or cancelling its ``CancelToken``, tears the socket down promptly.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Sequence

__all__ = [
    "ProviderError",
    "CancelToken",
    "Event",
    "Provider",
    "AnthropicProvider",
    "GeminiProvider",
    "OpenAIProvider",
    "OllamaProvider",
    "CustomOpenAIProvider",
    "all_providers",
    "get_provider",
    "ProviderInfo",
    "ModelInfo",
    "ProviderRegistry",
]

USER_AGENT = "hyde-ai/1.0 (+https://github.com/HyDE-Project)"

# Anthropic's Claude 5 family rejects sampling parameters with HTTP 400.
_NO_SAMPLING_PREFIXES = (
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
)


# ---------------------------------------------------------------------------
# errors, cancellation, events
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """A user-presentable failure.

    ``str(err)`` is always safe to show verbatim in the UI: it names the
    provider and explains what went wrong in plain language. ``detail`` keeps
    the raw provider payload for the log, and is deliberately *not* part of
    the message so a wall of JSON never lands in a chat bubble.
    """

    def __init__(self, message: str, detail: str = "", status: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or ""
        self.status = status

    def __str__(self) -> str:
        return self.message


class CancelToken:
    """Cooperative cancellation for a stream running on a worker thread.

    The UI holds the token, the worker passes it to ``stream()``. Calling
    ``cancel()`` closes the underlying socket, which unblocks a thread parked
    in ``read1()``; the generator then returns cleanly instead of raising.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._closables: List[Any] = []

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            closables, self._closables = self._closables, []
        for item in closables:
            try:
                item.close()
            except Exception:
                pass

    def reset(self) -> None:
        self._event.clear()
        with self._lock:
            self._closables = []

    def _attach(self, closable: Any) -> None:
        with self._lock:
            self._closables.append(closable)
        if self._event.is_set():
            try:
                closable.close()
            except Exception:
                pass

    def _detach(self, closable: Any) -> None:
        with self._lock:
            try:
                self._closables.remove(closable)
            except ValueError:
                pass


class Event:
    """One item from ``stream_events()``.

    kind:
        ``"text"``     -- visible assistant output; ``text`` is the delta
        ``"thinking"`` -- reasoning delta (Claude/Gemini/Ollama expose this)
        ``"usage"``    -- token accounting; payload in ``data``
        ``"meta"``     -- provider bookkeeping (model name, response id, ...)
    """

    __slots__ = ("kind", "text", "data")

    def __init__(self, kind: str, text: str = "", data: Optional[Dict[str, Any]] = None):
        self.kind = kind
        self.text = text
        self.data = data or {}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Event(%r, %r)" % (self.kind, self.text[:40])


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

_SSL_CONTEXT = ssl.create_default_context()


def _friendly_network_error(provider: str, exc: Exception) -> ProviderError:
    if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
        return ProviderError(
            "%s timed out waiting for a response. Check your connection and try again."
            % provider,
            detail=repr(exc),
        )
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, socket.gaierror):
        return ProviderError(
            "Could not resolve the %s endpoint. Are you online?" % provider,
            detail=repr(exc),
        )
    if isinstance(reason, ConnectionRefusedError):
        return ProviderError(
            "%s refused the connection." % provider, detail=repr(exc)
        )
    if isinstance(reason, ssl.SSLError):
        return ProviderError(
            "TLS error talking to %s: %s" % (provider, reason), detail=repr(exc)
        )
    return ProviderError(
        "Could not reach %s: %s" % (provider, reason), detail=repr(exc)
    )


def _extract_api_error(body: bytes) -> str:
    """Pull the human-readable bit out of a provider's error payload.

    Handles the four shapes in play: Anthropic/OpenAI ``{"error":{"message"}}``,
    Google's ``[{"error":{"message"}}]`` list wrapper, and Ollama's bare
    ``{"error": "text"}`` string.
    """
    if not body:
        return ""
    try:
        text = body.decode("utf-8", "replace").strip()
    except Exception:
        return ""
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:500]

    if isinstance(parsed, list) and parsed:
        parsed = parsed[0]
    if isinstance(parsed, dict):
        err = parsed.get("error", parsed)
        if isinstance(err, str):
            return err
        if isinstance(err, dict):
            for key in ("message", "detail", "reason", "status"):
                value = err.get(key)
                if isinstance(value, str) and value:
                    return value
        message = parsed.get("message")
        if isinstance(message, str) and message:
            return message
    return text[:500]


def _open_stream(
    provider: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]],
    connect_timeout: float,
    read_timeout: float,
    cancel: Optional[CancelToken],
    method: str = "POST",
):
    """POST (or GET) and return a live, unread response object."""
    body = None
    if payload is not None:
        try:
            body = json.dumps(payload).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ProviderError(
                "Could not encode the request for %s: %s" % (provider, exc)
            ) from exc

    hdrs = {
        "User-Agent": USER_AGENT,
        # Force an uncompressed stream: urllib will not transparently inflate
        # a gzip body while it is still arriving.
        "Accept-Encoding": "identity",
    }
    hdrs.update(headers)

    request = urllib.request.Request(url, data=body, headers=hdrs, method=method)

    # urlopen's timeout is the socket timeout, which re-arms on every recv --
    # i.e. it behaves as a read timeout once the stream is open. We use the
    # larger read timeout for the lifetime of the socket, because a slow model
    # legitimately produces long gaps between tokens.
    timeout = max(connect_timeout, read_timeout)

    try:
        response = urllib.request.urlopen(request, timeout=timeout, context=_SSL_CONTEXT)
    except urllib.error.HTTPError as exc:
        # The exception object *is* the body; read it before it is discarded.
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        finally:
            try:
                exc.close()
            except Exception:
                pass
        detail = _extract_api_error(raw)
        raise ProviderError(
            _status_message(provider, exc.code, detail),
            detail=raw.decode("utf-8", "replace")[:2000],
            status=exc.code,
        ) from exc
    except urllib.error.URLError as exc:
        if cancel is not None and cancel.cancelled:
            raise _Cancelled()
        raise _friendly_network_error(provider, exc) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise _friendly_network_error(provider, exc) from exc
    except OSError as exc:
        if cancel is not None and cancel.cancelled:
            raise _Cancelled()
        raise _friendly_network_error(provider, exc) from exc
    except Exception as exc:
        if cancel is not None and cancel.cancelled:
            raise _Cancelled()
        raise _friendly_network_error(provider, exc) from exc

    if cancel is not None:
        cancel._attach(response)
    return response


def _status_message(provider: str, code: int, detail: str) -> str:
    detail = (detail or "").strip()
    suffix = ": %s" % detail if detail else "."
    if code in (401, 403):
        return (
            "%s rejected the API key (HTTP %d)%s"
            % (provider, code, suffix)
        )
    if code == 404:
        return "%s could not find that model or endpoint (HTTP 404)%s" % (provider, suffix)
    if code == 429:
        return "%s is rate limiting you (HTTP 429)%s" % (provider, suffix)
    if code == 400:
        return "%s rejected the request (HTTP 400)%s" % (provider, suffix)
    if code in (500, 502, 503, 529):
        return "%s is unavailable right now (HTTP %d)%s" % (provider, code, suffix)
    return "%s returned HTTP %d%s" % (provider, code, suffix)


class _Cancelled(Exception):
    """Internal: the user stopped the stream. Never escapes this module."""


def _iter_raw(response, cancel: Optional[CancelToken]) -> Iterator[bytes]:
    """Yield bytes as they arrive, honouring cancellation."""
    while True:
        if cancel is not None and cancel.cancelled:
            raise _Cancelled()
        try:
            chunk = response.read1(65536)
        except (socket.timeout, TimeoutError) as exc:
            if cancel is not None and cancel.cancelled:
                raise _Cancelled()
            raise ProviderError(
                "The stream stalled (no data for a long time); giving up.",
                detail=repr(exc),
            ) from exc
        except Exception as exc:
            # Cancelling closes the socket under this thread. http.client
            # then clears its file object, so the parked read1() surfaces as
            # AttributeError/ValueError/OSError depending on exactly where it
            # was. All of those mean "the user pressed stop", not "failure".
            if cancel is not None and cancel.cancelled:
                raise _Cancelled()
            raise ProviderError(
                "The connection dropped mid-response: %s" % exc, detail=repr(exc)
            ) from exc
        if not chunk:
            return
        yield chunk


def _iter_sse(response, cancel: Optional[CancelToken]) -> Iterator[tuple]:
    """Parse Server-Sent Events, yielding ``(event_name, data_string)``.

    Splits only on ``\\n`` and strips a trailing ``\\r`` -- never on a bare
    ``\\r`` -- so a carriage return inside a JSON string cannot corrupt a
    frame. Multiple ``data:`` lines in one frame are joined with newlines,
    per the SSE spec.
    """
    buffer = b""
    event_name: Optional[str] = None
    data_lines: List[str] = []

    for chunk in _iter_raw(response, cancel):
        buffer += chunk
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            line = raw.rstrip(b"\r").decode("utf-8", "replace")

            if line == "":
                if data_lines:
                    yield event_name, "\n".join(data_lines)
                event_name, data_lines = None, []
            elif line.startswith(":"):
                continue  # comment / keep-alive
            elif line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            # any other field (id:, retry:) is irrelevant here

    if data_lines:  # unterminated final frame
        yield event_name, "\n".join(data_lines)


def _iter_ndjson(response, cancel: Optional[CancelToken]) -> Iterator[Dict[str, Any]]:
    """Parse newline-delimited JSON (Ollama's native format)."""
    buffer = b""
    for chunk in _iter_raw(response, cancel):
        buffer += chunk
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            raw = raw.strip()
            if not raw:
                continue
            obj = _loads_lenient(raw)
            if obj is not None:
                yield obj
    tail = buffer.strip()
    if tail:
        obj = _loads_lenient(tail)
        if obj is not None:
            yield obj


def _loads_lenient(raw: bytes) -> Optional[Dict[str, Any]]:
    """Decode one JSON object, skipping unparsable noise.

    A malformed line is dropped rather than killing an otherwise-good
    response; providers occasionally emit keep-alive junk.
    """
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _http_json(
    provider: str,
    url: str,
    headers: Dict[str, str],
    timeout: float,
    payload: Optional[Dict[str, Any]] = None,
    method: str = "GET",
) -> Any:
    """Small non-streaming JSON GET/POST, used for model discovery."""
    response = _open_stream(
        provider, url, headers, payload, timeout, timeout, None, method=method
    )
    try:
        raw = response.read()
    except (socket.timeout, TimeoutError, OSError) as exc:
        raise _friendly_network_error(provider, exc) from exc
    finally:
        try:
            response.close()
        except Exception:
            pass
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise ProviderError(
            "%s returned a response that was not valid JSON." % provider,
            detail=raw[:500].decode("utf-8", "replace"),
        ) from exc


# ---------------------------------------------------------------------------
# message helpers
# ---------------------------------------------------------------------------


def _clean_messages(messages: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Coerce to ``[{"role": "user"|"assistant", "content": str}, ...]``.

    Entries with empty content are dropped; any unrecognised role is treated
    as ``user`` so a stray value cannot 400 the request.
    """
    out: List[Dict[str, str]] = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user").lower()
        if role not in ("user", "assistant"):
            role = "user"
        content = item.get("content")
        if content is None:
            continue
        if not isinstance(content, str):
            content = str(content)
        if not content.strip():
            continue
        out.append({"role": role, "content": content})
    return out


def _strict_turns(messages: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Normalise to strictly alternating turns starting with ``user``.

    Anthropic and Gemini both reject histories that lead with an assistant
    turn or repeat a role. Consecutive same-role messages are merged with a
    blank line between them.
    """
    cleaned = _clean_messages(messages)
    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)

    merged: List[Dict[str, str]] = []
    for item in cleaned:
        if merged and merged[-1]["role"] == item["role"]:
            merged[-1]["content"] += "\n\n" + item["content"]
        else:
            merged.append(dict(item))
    return merged


def _as_dict(config: Any) -> Any:
    """Accept a config.Config, a plain dict, or None."""
    if config is None:
        return _NullConfig()
    if hasattr(config, "get") and hasattr(config, "api_key"):
        return config
    if isinstance(config, dict):
        return _DictConfig(config)
    return _NullConfig()


class _NullConfig:
    def get(self, dotted: str, default: Any = None) -> Any:
        return default

    def api_key(self, provider_id: str) -> str:
        return ""

    def model_for(self, provider_id: str, default: str = "") -> str:
        return default

    def timeouts(self) -> tuple:
        return (15.0, 180.0)


class _DictConfig(_NullConfig):
    def __init__(self, data: Dict[str, Any]):
        self.data = data

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def api_key(self, provider_id: str) -> str:
        value = self.get("api_keys.%s" % provider_id, "")
        return value.strip() if isinstance(value, str) else ""

    def model_for(self, provider_id: str, default: str = "") -> str:
        value = self.get("models.%s" % provider_id, "")
        return value.strip() if isinstance(value, str) and value.strip() else default

    def timeouts(self) -> tuple:
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
        return (connect, read)


# ---------------------------------------------------------------------------
# base class
# ---------------------------------------------------------------------------


class Provider:
    """Abstract base. Subclasses implement ``_stream_impl``."""

    id: str = ""
    name: str = ""
    env_vars: Sequence[str] = ()
    requires_key: bool = True
    key_help: str = ""
    key_url: str = ""

    #: Static catalogue; Ollama overrides ``models`` with live discovery.
    catalogue: Sequence[str] = ()
    labels: Dict[str, str] = {}
    default_model: str = ""

    def __init__(self, config: Any = None):
        self.config = _as_dict(config)
        self._reason: Optional[str] = None

    # -- identity -------------------------------------------------------

    @property
    def models(self) -> List[str]:
        return list(self.catalogue)

    def label_for(self, model: str) -> str:
        return self.labels.get(model, model)

    def describe(self, model: str) -> str:
        return getattr(self, "descriptions", {}).get(model, "")

    def preferred_model(self) -> str:
        chosen = self.config.model_for(self.id, "")
        available = self.models
        if chosen and chosen in available:
            return chosen
        if self.default_model and self.default_model in available:
            return self.default_model
        return available[0] if available else (chosen or self.default_model)

    # -- credentials ----------------------------------------------------

    def api_key(self) -> str:
        """config.json first, then the environment, then nothing."""
        key = self.config.api_key(self.id)
        if key:
            return key
        for var in self.env_vars:
            value = os.environ.get(var, "").strip()
            if value:
                return value
        return ""

    def available(self, refresh: bool = False) -> bool:
        """True when this provider can actually service a request.

        Never raises. When it returns False, ``unavailable_reason()`` explains
        why in language fit for the UI.
        """
        if not self.requires_key:
            self._reason = None
            return True
        if self.api_key():
            self._reason = None
            return True
        self._reason = self._no_key_reason()
        return False

    def _no_key_reason(self) -> str:
        env_hint = ""
        if self.env_vars:
            env_hint = " or export %s" % self.env_vars[0]
        return "No API key. Add one in Settings%s." % env_hint

    def unavailable_reason(self) -> Optional[str]:
        return self._reason

    def available_cached(self) -> bool:
        """Availability without ever performing network I/O.

        Safe to call from the GTK main thread. Key-based providers can answer
        exactly; Ollama answers from its last probe (see ``probe()``), so call
        ``ProviderRegistry.refresh()`` on a worker thread to keep it current.
        """
        return self.available()

    def base_url(self) -> str:
        raise NotImplementedError

    def _timeouts(self) -> tuple:
        return self.config.timeouts()

    def _max_tokens(self) -> int:
        value = self.config.get("max_tokens", 4096)
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = 4096
        return max(1, min(value, 128000))

    def _temperature(self, model: str) -> Optional[float]:
        value = self.config.get("temperature", None)
        if value is None or value == "":
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        if not 0.0 <= value <= 2.0:
            return None
        return value

    # -- streaming ------------------------------------------------------

    def stream(
        self,
        messages: Sequence[Dict[str, Any]],
        model: str = "",
        system: str = "",
        cancel: Optional[CancelToken] = None,
    ) -> Iterator[str]:
        """Yield visible text deltas. This is the chat view's entry point."""
        for event in self.stream_events(messages, model, system, cancel):
            if event.kind == "text" and event.text:
                yield event.text

    def stream_events(
        self,
        messages: Sequence[Dict[str, Any]],
        model: str = "",
        system: str = "",
        cancel: Optional[CancelToken] = None,
    ) -> Iterator[Event]:
        """Yield typed events. Raises ProviderError on any failure."""
        model = (model or self.preferred_model() or "").strip()
        if not model:
            raise ProviderError("No model selected for %s." % self.name)

        if self.requires_key and not self.api_key():
            raise ProviderError(
                "%s is not configured. %s" % (self.name, self._no_key_reason())
            )

        turns = _clean_messages(messages)
        if not turns:
            raise ProviderError("There is nothing to send.")

        try:
            for event in self._stream_impl(turns, model, system or "", cancel):
                yield event
        except _Cancelled:
            return
        except ProviderError:
            raise
        except GeneratorExit:
            raise
        except Exception as exc:  # never leak a traceback to the UI
            raise ProviderError(
                "%s: unexpected error while streaming (%s)."
                % (self.name, type(exc).__name__),
                detail=repr(exc),
            ) from exc

    def _stream_impl(
        self,
        messages: List[Dict[str, str]],
        model: str,
        system: str,
        cancel: Optional[CancelToken],
    ) -> Iterator[Event]:
        raise NotImplementedError

    def _finish(self, response, cancel: Optional[CancelToken]) -> None:
        if cancel is not None:
            cancel._detach(response)
        try:
            response.close()
        except Exception:
            pass

    def __repr__(self) -> str:  # pragma: no cover
        return "<%s id=%r>" % (type(self).__name__, self.id)


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


class AnthropicProvider(Provider):
    id = "anthropic"
    name = "Claude"
    env_vars = ("ANTHROPIC_API_KEY",)
    key_url = "https://console.anthropic.com/settings/keys"
    key_help = "Create a key at console.anthropic.com -> Settings -> API keys."

    catalogue = (
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-fable-5",
        "claude-haiku-4-5",
    )
    labels = {
        "claude-opus-5": "Claude Opus 5",
        "claude-sonnet-5": "Claude Sonnet 5",
        "claude-fable-5": "Claude Fable 5",
        "claude-haiku-4-5": "Claude Haiku 4.5",
    }
    default_model = "claude-opus-5"
    descriptions = {
        "claude-opus-5": "Most capable general model, 1M context",
        "claude-sonnet-5": "Balanced speed and capability",
        "claude-fable-5": "Highest capability, highest cost",
        "claude-haiku-4-5": "Fast and inexpensive",
    }

    API_VERSION = "2023-06-01"

    def base_url(self) -> str:
        return str(
            self.config.get("anthropic.base_url", "https://api.anthropic.com")
        ).rstrip("/")

    def _stream_impl(self, messages, model, system, cancel):
        connect_timeout, read_timeout = self._timeouts()
        turns = _strict_turns(messages)
        if not turns:
            raise ProviderError("Claude needs the conversation to start with your message.")

        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": self._max_tokens(),
            "stream": True,
            "messages": turns,
        }
        if system.strip():
            payload["system"] = system.strip()

        # The Claude 5 family removed sampling parameters and returns 400 if
        # they are present, so only send temperature to older models.
        temperature = self._temperature(model)
        if temperature is not None and not model.startswith(_NO_SAMPLING_PREFIXES):
            payload["temperature"] = temperature

        thinking = str(self.config.get("anthropic.thinking", "default")).lower()
        if thinking == "summarized":
            payload["thinking"] = {"type": "adaptive", "display": "summarized"}
        elif thinking == "off":
            payload["thinking"] = {"type": "disabled"}

        headers = {
            "x-api-key": self.api_key(),
            "anthropic-version": self.API_VERSION,
            "content-type": "application/json",
            "accept": "text/event-stream",
        }

        response = _open_stream(
            self.name,
            self.base_url() + "/v1/messages",
            headers,
            payload,
            connect_timeout,
            read_timeout,
            cancel,
        )

        saw_stop = False
        block_kinds: Dict[int, str] = {}
        try:
            for _name, data in _iter_sse(response, cancel):
                obj = _loads_lenient(data.encode("utf-8"))
                if obj is None:
                    continue

                kind = obj.get("type")

                if kind == "error":
                    err = obj.get("error") or {}
                    raise ProviderError(
                        "Claude stopped mid-response: %s"
                        % (err.get("message") or err.get("type") or "unknown error"),
                        detail=json.dumps(obj)[:2000],
                    )

                if kind == "message_start":
                    usage = ((obj.get("message") or {}).get("usage")) or {}
                    if usage:
                        yield Event("usage", data={"input_tokens": usage.get("input_tokens")})

                elif kind == "content_block_start":
                    block = obj.get("content_block") or {}
                    block_kinds[obj.get("index", 0)] = str(block.get("type") or "text")

                elif kind == "content_block_delta":
                    delta = obj.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        text = delta.get("text") or ""
                        if text:
                            yield Event("text", text)
                    elif dtype == "thinking_delta":
                        text = delta.get("thinking") or ""
                        if text:
                            yield Event("thinking", text)
                    # input_json_delta / signature_delta are tool-call and
                    # attestation plumbing: deliberately not rendered.

                elif kind == "content_block_stop":
                    block_kinds.pop(obj.get("index", 0), None)

                elif kind == "message_delta":
                    usage = obj.get("usage") or {}
                    stop_reason = (obj.get("delta") or {}).get("stop_reason")
                    payload_out: Dict[str, Any] = {}
                    if usage:
                        payload_out["output_tokens"] = usage.get("output_tokens")
                    if stop_reason:
                        payload_out["stop_reason"] = stop_reason
                    if payload_out:
                        yield Event("usage", data=payload_out)
                    if stop_reason == "max_tokens":
                        yield Event(
                            "meta",
                            data={
                                "warning": "Response hit the max_tokens limit "
                                "and was cut short."
                            },
                        )

                elif kind == "message_stop":
                    saw_stop = True

            if not saw_stop and not (cancel is not None and cancel.cancelled):
                raise ProviderError(
                    "Claude's response ended unexpectedly (the stream was cut short)."
                )
        finally:
            self._finish(response, cancel)


# ---------------------------------------------------------------------------
# Google Gemini
# ---------------------------------------------------------------------------


class GeminiProvider(Provider):
    """Gemini over ``:streamGenerateContent?alt=sse``.

    The newer Interactions API keeps history server-side via
    ``previous_interaction_id``; passing a full client-side history to it is
    undocumented. Our interface is a stateless list of messages, which maps
    exactly onto ``generateContent``'s ``contents[]`` -- a documented,
    fully-supported path. The response framing is parsed defensively: both
    bare ``data:`` frames carrying ``candidates`` and named ``content.delta``
    events carrying ``delta.text`` are accepted, because Google's own docs
    disagree about which one this endpoint emits.
    """

    id = "gemini"
    name = "Gemini"
    # GOOGLE_API_KEY wins over GEMINI_API_KEY, matching Google's own clients.
    env_vars = ("GOOGLE_API_KEY", "GEMINI_API_KEY")
    key_url = "https://aistudio.google.com/apikey"
    key_help = "Create a key at aistudio.google.com/apikey."

    catalogue = (
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-pro-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    )
    labels = {
        "gemini-3.7-flash": "Gemini 3.7 Flash",
        "gemini-3.6-flash": "Gemini 3.6 Flash",
        "gemini-3.5-flash": "Gemini 3.5 Flash",
        "gemini-3.5-flash-lite": "Gemini 3.5 Flash-Lite",
        "gemini-3.1-pro-preview": "Gemini 3.1 Pro (preview)",
        "gemini-2.5-pro": "Gemini 2.5 Pro",
        "gemini-2.5-flash": "Gemini 2.5 Flash",
    }
    default_model = "gemini-3.7-flash"
    descriptions = {
        "gemini-3.7-flash": "Current default, fast and cheap",
        "gemini-3.6-flash": "Previous Flash generation",
        "gemini-3.5-flash": "Older stable Flash",
        "gemini-3.5-flash-lite": "Cheapest 3.5 tier",
        "gemini-3.1-pro-preview": "Deepest reasoning (preview)",
        "gemini-2.5-pro": "Previous-generation Pro",
        "gemini-2.5-flash": "Previous-generation Flash",
    }

    def base_url(self) -> str:
        return str(
            self.config.get(
                "gemini.base_url", "https://generativelanguage.googleapis.com"
            )
        ).rstrip("/")

    def _stream_impl(self, messages, model, system, cancel):
        connect_timeout, read_timeout = self._timeouts()
        turns = _strict_turns(messages)
        if not turns:
            raise ProviderError("Gemini needs the conversation to start with your message.")

        contents = [
            {
                # Gemini calls the assistant role "model".
                "role": "model" if turn["role"] == "assistant" else "user",
                "parts": [{"text": turn["content"]}],
            }
            for turn in turns
        ]

        generation_config: Dict[str, Any] = {"maxOutputTokens": self._max_tokens()}
        temperature = self._temperature(model)
        if temperature is not None:
            generation_config["temperature"] = temperature

        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system.strip():
            payload["systemInstruction"] = {"parts": [{"text": system.strip()}]}

        url = "%s/v1beta/models/%s:streamGenerateContent?alt=sse" % (
            self.base_url(),
            urllib.parse.quote(model, safe=""),
        )
        headers = {
            "x-goog-api-key": self.api_key(),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        response = _open_stream(
            self.name, url, headers, payload, connect_timeout, read_timeout, cancel
        )

        finish_reason: Optional[str] = None
        produced_text = False
        try:
            for name, data in _iter_sse(response, cancel):
                if data.strip() == "[DONE]":
                    break
                obj = _loads_lenient(data.encode("utf-8"))
                if obj is None:
                    continue

                if "error" in obj and isinstance(obj.get("error"), dict):
                    err = obj["error"]
                    raise ProviderError(
                        "Gemini stopped mid-response: %s"
                        % (err.get("message") or "unknown error"),
                        detail=json.dumps(obj)[:2000],
                    )

                # Framing A: named content.delta events.
                if name and name.startswith("content."):
                    delta = obj.get("delta") or {}
                    dtype = delta.get("type")
                    text = delta.get("text") or ""
                    if dtype in (None, "text") and text:
                        produced_text = True
                        yield Event("text", text)
                    elif dtype in ("thought", "thought_summary") and text:
                        yield Event("thinking", text)
                    continue

                # A prompt blocked before generation yields no candidates.
                feedback = obj.get("promptFeedback") or {}
                if feedback.get("blockReason"):
                    raise ProviderError(
                        "Gemini blocked the prompt (%s)." % feedback["blockReason"],
                        detail=json.dumps(obj)[:2000],
                    )

                # Framing B: bare GenerateContentResponse objects.
                for candidate in obj.get("candidates") or []:
                    content = candidate.get("content") or {}
                    for part in content.get("parts") or []:
                        if not isinstance(part, dict):
                            continue
                        text = part.get("text")
                        if not isinstance(text, str) or not text:
                            continue
                        if part.get("thought"):
                            yield Event("thinking", text)
                        else:
                            produced_text = True
                            yield Event("text", text)
                    if candidate.get("finishReason"):
                        finish_reason = candidate["finishReason"]

                usage = obj.get("usageMetadata") or {}
                if usage:
                    yield Event(
                        "usage",
                        data={
                            "input_tokens": usage.get("promptTokenCount"),
                            "output_tokens": usage.get("candidatesTokenCount"),
                            "total_tokens": usage.get("totalTokenCount"),
                        },
                    )

            if cancel is not None and cancel.cancelled:
                return

            # There is no [DONE] sentinel on this endpoint: completion is
            # signalled by finishReason on the final chunk.
            if finish_reason is None:
                raise ProviderError(
                    "Gemini's response ended unexpectedly (the stream was cut short)."
                )
            if finish_reason not in ("STOP", "MAX_TOKENS"):
                raise ProviderError(
                    "Gemini stopped early (%s)." % finish_reason
                )
            if finish_reason == "MAX_TOKENS":
                yield Event(
                    "meta",
                    data={"warning": "Response hit the token limit and was cut short."},
                )
            if not produced_text:
                yield Event(
                    "meta", data={"warning": "Gemini returned no text for that prompt."}
                )
        finally:
            self._finish(response, cancel)


# ---------------------------------------------------------------------------
# OpenAI (and OpenAI-compatible endpoints)
# ---------------------------------------------------------------------------


class OpenAIProvider(Provider):
    id = "openai"
    name = "ChatGPT"
    env_vars = ("OPENAI_API_KEY",)
    key_url = "https://platform.openai.com/api-keys"
    key_help = "Create a key at platform.openai.com/api-keys."

    catalogue = (
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.4",
    )
    labels = {
        "gpt-5.6-sol": "GPT-5.6 Sol",
        "gpt-5.6-terra": "GPT-5.6 Terra",
        "gpt-5.6-luna": "GPT-5.6 Luna",
        "gpt-5.5": "GPT-5.5",
        "gpt-5.4": "GPT-5.4",
    }
    default_model = "gpt-5.6-sol"
    descriptions = {
        "gpt-5.6-sol": "Frontier tier, complex work",
        "gpt-5.6-terra": "Balances intelligence and cost",
        "gpt-5.6-luna": "Cheapest, high volume",
        "gpt-5.5": "Previous flagship",
        "gpt-5.4": "Older generation",
    }

    #: Real OpenAI understands max_completion_tokens + stream_options;
    #: third-party clones usually only understand max_tokens.
    _openai_native = True

    def base_url(self) -> str:
        return str(self.config.get("openai.base_url", "https://api.openai.com/v1")).rstrip(
            "/"
        )

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": "Bearer %s" % self.api_key()}

    def _extra_body(self) -> Dict[str, Any]:
        return {}

    def _stream_impl(self, messages, model, system, cancel):
        connect_timeout, read_timeout = self._timeouts()

        wire: List[Dict[str, str]] = []
        if system.strip():
            # "developer" is the current instruction role; "system" is still
            # accepted but deprecated for reasoning models.
            wire.append(
                {
                    "role": "developer" if self._openai_native else "system",
                    "content": system.strip(),
                }
            )
        wire.extend(_clean_messages(messages))

        payload: Dict[str, Any] = {
            "model": model,
            "messages": wire,
            "stream": True,
        }

        if self._openai_native:
            payload["max_completion_tokens"] = self._max_tokens()
            payload["stream_options"] = {
                "include_usage": True,
                "include_obfuscation": False,
            }
            effort = str(self.config.get("openai.reasoning_effort", "") or "").strip()
            if effort:
                payload["reasoning_effort"] = effort
        else:
            payload["max_tokens"] = self._max_tokens()

        temperature = self._temperature(model)
        if temperature is not None:
            payload["temperature"] = temperature

        payload.update(self._extra_body())

        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        headers.update(self._auth_headers())

        response = _open_stream(
            self.name,
            self.base_url() + "/chat/completions",
            headers,
            payload,
            connect_timeout,
            read_timeout,
            cancel,
        )

        saw_done = False
        finish_reason: Optional[str] = None
        try:
            for _name, data in _iter_sse(response, cancel):
                if data.strip() == "[DONE]":
                    saw_done = True
                    break
                obj = _loads_lenient(data.encode("utf-8"))
                if obj is None:
                    continue

                if isinstance(obj.get("error"), (dict, str)):
                    err = obj["error"]
                    message = err if isinstance(err, str) else (
                        err.get("message") or "unknown error"
                    )
                    raise ProviderError(
                        "%s stopped mid-response: %s" % (self.name, message),
                        detail=json.dumps(obj)[:2000],
                    )

                usage = obj.get("usage")
                if isinstance(usage, dict) and usage:
                    yield Event(
                        "usage",
                        data={
                            "input_tokens": usage.get("prompt_tokens"),
                            "output_tokens": usage.get("completion_tokens"),
                            "total_tokens": usage.get("total_tokens"),
                        },
                    )

                # choices is [] on the usage chunk -- never index blindly.
                for choice in obj.get("choices") or []:
                    delta = choice.get("delta") or {}

                    refusal = delta.get("refusal")
                    if refusal:
                        raise ProviderError(
                            "%s declined to answer: %s" % (self.name, refusal)
                        )

                    reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        yield Event("thinking", reasoning)

                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        yield Event("text", text)

                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]

            if cancel is not None and cancel.cancelled:
                return

            if not saw_done and finish_reason is None:
                raise ProviderError(
                    "%s's response ended unexpectedly (the stream was cut short)."
                    % self.name
                )
            if finish_reason == "length":
                yield Event(
                    "meta",
                    data={"warning": "Response hit the token limit and was cut short."},
                )
            elif finish_reason == "content_filter":
                yield Event(
                    "meta",
                    data={"warning": "Response was stopped by the content filter."},
                )
        finally:
            self._finish(response, cancel)


class CustomOpenAIProvider(OpenAIProvider):
    """Any OpenAI-wire-format endpoint declared in ``custom_providers``.

    This is the escape hatch that makes "which providers are supported?" a
    non-question: OpenRouter, Groq, LM Studio, llama.cpp's server, vLLM and
    friends all work without code changes.
    """

    _openai_native = False

    def __init__(self, spec: Dict[str, Any], config: Any = None):
        super().__init__(config)
        self.spec = spec or {}
        self.id = str(self.spec.get("id") or "custom").strip() or "custom"
        self.name = str(self.spec.get("name") or self.id).strip() or self.id
        env = self.spec.get("env")
        self.env_vars = (str(env),) if isinstance(env, str) and env else ()
        self.requires_key = bool(self.spec.get("requires_key", True))
        self.key_url = str(self.spec.get("key_url") or "")
        self.key_help = str(self.spec.get("key_help") or "")
        models = self.spec.get("models") or []
        self.catalogue = tuple(str(m) for m in models if str(m).strip())
        labels = self.spec.get("labels")
        self.labels = dict(labels) if isinstance(labels, dict) else {}
        self.default_model = (
            str(self.spec.get("default_model") or "")
            or (self.catalogue[0] if self.catalogue else "")
        )
        self._base_url = str(self.spec.get("base_url") or "").rstrip("/")

    def base_url(self) -> str:
        return self._base_url

    def api_key(self) -> str:
        inline = self.spec.get("api_key")
        if isinstance(inline, str) and inline.strip():
            return inline.strip()
        return super().api_key()

    def _extra_body(self) -> Dict[str, Any]:
        extra = self.spec.get("extra_body")
        return dict(extra) if isinstance(extra, dict) else {}

    def available(self, refresh: bool = False) -> bool:
        if not self._base_url:
            self._reason = "This custom provider has no base_url set in config.json."
            return False
        if not self.catalogue:
            self._reason = "This custom provider has no models listed in config.json."
            return False
        return super().available(refresh=refresh)


# ---------------------------------------------------------------------------
# Ollama (local)
# ---------------------------------------------------------------------------


class OllamaProvider(Provider):
    """Local Ollama daemon, native ``/api/chat`` (NDJSON) transport.

    Models are discovered live from ``/api/tags``. Three distinct states are
    reported, because they need three different fixes:

    * daemon unreachable      -> start it with ``ollama serve``
    * daemon up, zero models  -> pull one with ``ollama pull ...``
    * daemon up, models found -> available
    """

    id = "ollama"
    name = "Ollama"
    env_vars = ("OLLAMA_API_KEY",)
    requires_key = False
    key_help = "Ollama runs locally and needs no API key."

    default_model = ""

    #: Discovery is cached briefly so a UI that polls available() does not
    #: hammer the daemon on every repaint.
    CACHE_TTL = 5.0
    PROBE_TIMEOUT = 2.0

    def __init__(self, config: Any = None):
        super().__init__(config)
        self._models: List[str] = []
        self._checked_at = 0.0
        self._probe_lock = threading.Lock()
        self._reason = "Not checked yet."

    def base_url(self) -> str:
        configured = str(self.config.get("ollama.base_url", "") or "").strip()
        if configured:
            return configured.rstrip("/")
        host = os.environ.get("OLLAMA_HOST", "").strip()
        if host:
            if not host.startswith(("http://", "https://")):
                host = "http://" + host
            return host.rstrip("/")
        return "http://localhost:11434"

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = self.api_key()
        if key:  # only needed for Ollama Cloud / a proxied daemon
            headers["Authorization"] = "Bearer %s" % key
        return headers

    def probe(self) -> bool:
        """Query ``/api/tags``. Never raises; sets ``_reason`` on failure."""
        with self._probe_lock:
            try:
                data = _http_json(
                    self.name,
                    self.base_url() + "/api/tags",
                    self._headers(),
                    self.PROBE_TIMEOUT,
                )
            except ProviderError as exc:
                self._models = []
                self._checked_at = time.time()
                if exc.status in (401, 403):
                    self._reason = (
                        "Ollama rejected the credentials for %s." % self.base_url()
                    )
                else:
                    self._reason = (
                        "Ollama is not running at %s. Start it with: ollama serve"
                        % self.base_url()
                    )
                return False

            names: List[str] = []
            if isinstance(data, dict):
                for entry in data.get("models") or []:
                    if isinstance(entry, dict):
                        name = entry.get("model") or entry.get("name")
                        if isinstance(name, str) and name.strip():
                            names.append(name.strip())

            self._models = sorted(set(names))
            self._checked_at = time.time()

            if not self._models:
                self._reason = (
                    "Ollama is running but has no models installed. "
                    "Pull one with: ollama pull gemma4:12b"
                )
                return False

            self._reason = None
            return True

    def available(self, refresh: bool = False) -> bool:
        if refresh or (time.time() - self._checked_at) > self.CACHE_TTL:
            return self.probe()
        return bool(self._models)

    def available_cached(self) -> bool:
        """Last known state only -- never touches the network."""
        if self._checked_at == 0.0:
            self._reason = (
                "Checking for a local Ollama daemon at %s..." % self.base_url()
            )
            return False
        return bool(self._models)

    @property
    def models(self) -> List[str]:
        # NEVER probe inline: this property is reached from the GTK main loop
        # (Sidebar._sync_selectors). A black-holed endpoint would freeze the
        # panel for PROBE_TIMEOUT seconds on startup and on every switch.
        # Discovery happens off-thread via ProviderRegistry.refresh_async().
        return list(self._models)

    def label_for(self, model: str) -> str:
        """Turn ``gemma4:12b`` into ``Gemma4 12B``, leaving ``latest`` off."""
        if not model:
            return model
        base, _, tag = model.partition(":")
        pretty = base.replace("-", " ").replace("_", " ").strip()
        pretty = " ".join(word[:1].upper() + word[1:] for word in pretty.split() if word)
        if tag and tag.lower() != "latest":
            size = tag.upper() if any(c.isdigit() for c in tag) else tag.title()
            return "%s %s" % (pretty, size)
        return pretty

    def preferred_model(self) -> str:
        chosen = self.config.model_for(self.id, "")
        available = self.models
        if chosen and chosen in available:
            return chosen
        return available[0] if available else ""

    def _stream_impl(self, messages, model, system, cancel):
        connect_timeout, read_timeout = self._timeouts()

        wire: List[Dict[str, str]] = []
        if system.strip():
            wire.append({"role": "system", "content": system.strip()})
        wire.extend(_clean_messages(messages))

        options: Dict[str, Any] = {"num_predict": self._max_tokens()}
        temperature = self._temperature(model)
        if temperature is not None:
            options["temperature"] = temperature
        try:
            num_ctx = int(self.config.get("ollama.num_ctx", 0) or 0)
        except (TypeError, ValueError):
            num_ctx = 0
        if num_ctx > 0:
            options["num_ctx"] = num_ctx

        payload: Dict[str, Any] = {
            "model": model,
            "messages": wire,
            "stream": True,
            "options": options,
        }
        keep_alive = str(self.config.get("ollama.keep_alive", "") or "").strip()
        if keep_alive:
            payload["keep_alive"] = keep_alive

        response = _open_stream(
            self.name,
            self.base_url() + "/api/chat",
            self._headers(),
            payload,
            connect_timeout,
            read_timeout,
            cancel,
        )

        saw_done = False
        try:
            for obj in _iter_ndjson(response, cancel):
                # Ollama reports mid-stream failures with HTTP still at 200,
                # so every single line has to be checked.
                if "error" in obj:
                    err = obj.get("error")
                    message = err if isinstance(err, str) else json.dumps(err)
                    raise ProviderError(
                        "Ollama stopped mid-response: %s" % message,
                        detail=json.dumps(obj)[:2000],
                    )

                message = obj.get("message") or {}
                if isinstance(message, dict):
                    thinking = message.get("thinking")
                    if isinstance(thinking, str) and thinking:
                        yield Event("thinking", thinking)
                    text = message.get("content")
                    if isinstance(text, str) and text:
                        yield Event("text", text)

                if obj.get("done"):
                    saw_done = True
                    yield Event(
                        "usage",
                        data={
                            "input_tokens": obj.get("prompt_eval_count"),
                            "output_tokens": obj.get("eval_count"),
                            "done_reason": obj.get("done_reason"),
                        },
                    )
                    break

            if not saw_done and not (cancel is not None and cancel.cancelled):
                raise ProviderError(
                    "Ollama's response ended unexpectedly (the stream was cut short)."
                )
        finally:
            self._finish(response, cancel)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

_BUILTIN = (AnthropicProvider, GeminiProvider, OpenAIProvider, OllamaProvider)


def build_custom_provider(spec: Dict[str, Any], config: Any = None) -> Optional[Provider]:
    """Build one provider from a ``custom_providers`` entry.

    Accepted shape (only ``id``, ``base_url`` and ``models`` are required)::

        {
          "id": "openrouter",
          "name": "OpenRouter",
          "wire": "openai",
          "base_url": "https://openrouter.ai/api/v1",
          "api_key": "",
          "env": "OPENROUTER_API_KEY",
          "models": ["deepseek/deepseek-r1"],
          "labels": {"deepseek/deepseek-r1": "DeepSeek R1"},
          "requires_key": true,
          "extra_body": {}
        }

    Returns None for an unusable entry rather than raising, so one bad hand-
    edited block cannot stop the sidebar from starting.
    """
    if not isinstance(spec, dict):
        return None
    wire = str(spec.get("wire") or "openai").lower()
    if wire not in ("openai", "openai-compatible", "compatible"):
        return None
    if not spec.get("id") or not spec.get("base_url"):
        return None
    try:
        return CustomOpenAIProvider(spec, config)
    except Exception:
        return None


def all_providers(config: Any = None) -> List[Provider]:
    """Every provider the UI should show, in display order.

    Providers are returned whether or not they are usable -- the sidebar lists
    unusable ones greyed out with ``unavailable_reason()`` as the hint, which
    is how a user discovers that they need to paste a key or start the daemon.
    Construction never performs network I/O.
    """
    providers: List[Provider] = []
    for cls in _BUILTIN:
        try:
            providers.append(cls(config))
        except Exception:
            continue

    cfg = _as_dict(config)
    seen = {p.id for p in providers}
    for spec in cfg.get("custom_providers", []) or []:
        provider = build_custom_provider(spec, config)
        if provider is not None and provider.id not in seen:
            providers.append(provider)
            seen.add(provider.id)

    return providers


class ProviderInfo:
    """Immutable snapshot of a provider, for the UI's selector.

    ``available`` is a plain bool *attribute* rather than a method, because
    view code naturally writes ``if info.available:`` -- and a bound method
    is always truthy, which would silently show every provider as ready.
    """

    __slots__ = ("id", "name", "available", "hint", "models", "requires_key", "key_url")

    def __init__(self, provider: "Provider", available: bool):
        self.id = provider.id
        self.name = provider.name
        self.available = bool(available)
        self.hint = provider.unavailable_reason() or ""
        self.requires_key = bool(provider.requires_key)
        self.key_url = getattr(provider, "key_url", "") or ""
        self.models = list(provider.catalogue)

    def __repr__(self) -> str:  # pragma: no cover
        return "<ProviderInfo %s available=%s>" % (self.id, self.available)


class ModelInfo:
    """One selectable model."""

    __slots__ = ("id", "name", "description", "provider_id")

    def __init__(self, model_id: str, name: str, description: str, provider_id: str):
        self.id = model_id
        self.name = name
        self.description = description or ""
        self.provider_id = provider_id

    def __repr__(self) -> str:  # pragma: no cover
        return "<ModelInfo %s/%s>" % (self.provider_id, self.id)


class ProviderRegistry:
    """Convenience facade over ``all_providers()`` for the UI layer.

    Everything here is safe to call from the GTK main thread: no method
    performs network I/O. Ollama discovery happens in :meth:`refresh`, which
    is meant to be run on a worker thread (or via :meth:`refresh_async`).
    """

    def __init__(self, config: Any = None):
        self.config = config
        self._providers: List[Provider] = all_providers(config)

    # -- lookup ---------------------------------------------------------

    def reload(self) -> None:
        """Rebuild from config, e.g. after custom_providers was edited."""
        self._providers = all_providers(self.config)

    def provider(self, provider_id: str) -> Optional[Provider]:
        for provider in self._providers:
            if provider.id == provider_id:
                return provider
        return None

    def list_providers(self) -> List[ProviderInfo]:
        out: List[ProviderInfo] = []
        for provider in self._providers:
            try:
                available = provider.available_cached()
            except Exception:
                available = False
            out.append(ProviderInfo(provider, available))
        return out

    def models(self, provider_id: str) -> List[ModelInfo]:
        provider = self.provider(provider_id)
        if provider is None:
            return []
        try:
            ids = provider.models
        except Exception:
            ids = []
        return [
            ModelInfo(m, provider.label_for(m), provider.describe(m), provider.id)
            for m in ids
        ]

    def default_model(self, provider_id: str) -> str:
        provider = self.provider(provider_id)
        return provider.preferred_model() if provider is not None else ""

    def first_available(self) -> Optional[str]:
        for info in self.list_providers():
            if info.available:
                return info.id
        return self._providers[0].id if self._providers else None

    # -- credentials ----------------------------------------------------

    def set_api_key(self, provider_id: str, value: str) -> None:
        """Store a key in config.json and persist it (mode 0600).

        Raises ProviderError with a readable message if it cannot be saved.
        """
        provider = self.provider(provider_id)
        if provider is None:
            raise ProviderError("There is no provider called %r." % provider_id)
        cfg = self.config
        if cfg is None or not hasattr(cfg, "set_api_key"):
            raise ProviderError(
                "No writable config is loaded, so the key cannot be saved."
            )
        try:
            cfg.set_api_key(provider_id, value)
            cfg.save()
        except OSError as exc:
            raise ProviderError(
                "Could not write the config file: %s" % (exc.strerror or exc)
            ) from exc
        # Drop any cached "no key" verdict so the UI updates immediately.
        try:
            provider.available(refresh=True)
        except Exception:
            pass

    # -- discovery ------------------------------------------------------

    def refresh(self) -> None:
        """Re-probe every provider. Performs network I/O -- worker thread only."""
        for provider in self._providers:
            try:
                provider.available(refresh=True)
            except Exception:
                continue

    def refresh_async(self, done_callback=None) -> threading.Thread:
        """Probe on a background thread.

        ``done_callback`` is invoked from that thread; a GTK caller should
        marshal back with ``GLib.idle_add``.
        """

        def run() -> None:
            self.refresh()
            if done_callback is not None:
                try:
                    done_callback()
                except Exception:
                    pass

        thread = threading.Thread(target=run, name="hyde-ai-probe", daemon=True)
        thread.start()
        return thread

    # -- streaming ------------------------------------------------------

    def stream(
        self,
        provider_id: str,
        model_id: str,
        messages: Sequence[Dict[str, Any]],
        system: str = "",
        cancel: Optional[CancelToken] = None,
    ) -> Iterator[str]:
        """Yield text deltas from one provider. Raises ProviderError."""
        provider = self.provider(provider_id)
        if provider is None:
            raise ProviderError("There is no provider called %r." % provider_id)
        return provider.stream(messages, model_id, system, cancel)

    def stream_events(
        self,
        provider_id: str,
        model_id: str,
        messages: Sequence[Dict[str, Any]],
        system: str = "",
        cancel: Optional[CancelToken] = None,
    ) -> Iterator[Event]:
        """As :meth:`stream`, but including thinking/usage events."""
        provider = self.provider(provider_id)
        if provider is None:
            raise ProviderError("There is no provider called %r." % provider_id)
        return provider.stream_events(messages, model_id, system, cancel)

    def __len__(self) -> int:
        return len(self._providers)

    def __iter__(self):
        return iter(self._providers)


def get_provider(provider_id: str, config: Any = None) -> Optional[Provider]:
    """Look one provider up by id, or None if there is no such provider."""
    for provider in all_providers(config):
        if provider.id == provider_id:
            return provider
    return None


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import sys

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import config as config_module

        cfg = config_module.load()
    except Exception:
        cfg = None

    for provider in all_providers(cfg):
        ok = provider.available(refresh=True)
        mark = "ready" if ok else "unavailable"
        print("%-12s %-10s %s" % (provider.id, mark, provider.unavailable_reason() or ""))
        for model in provider.models[:8]:
            print("                 - %s (%s)" % (model, provider.label_for(model)))
