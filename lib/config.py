"""
hyde-ai :: configuration
========================

Load/save ``~/.config/hyde-ai/config.json``.

The file holds API keys, so it is written atomically and kept at mode 0600.
Only the Python standard library is used.

Design notes
------------
* **Atomic writes.** A temp file is created in the *same* directory (so the
  final ``os.replace`` is a same-filesystem rename and therefore atomic),
  chmod'ed to 0600 *before* it is moved into place, fsync'ed, then renamed.
  At no point does a readable-by-others file containing keys exist on disk.
* **Never fatal.** A corrupt or unreadable config is backed up next to the
  original and replaced by defaults, rather than raising. A sidebar that
  refuses to start because of a stray comma is a worse outcome than one that
  starts with defaults and says so.
* **Forward/backward compatible.** Loaded data is deep-merged *over* the
  defaults, so keys added by a future version appear automatically, and keys
  written by another component are preserved verbatim on save.
"""

from __future__ import annotations

import copy
import datetime
import json
import os
import platform
import stat
import tempfile
import time
from typing import Any, Dict, List, Optional

__all__ = [
    "CONFIG_DIR",
    "CONFIG_PATH",
    "DEFAULTS",
    "Config",
    "load",
    "expand_system_prompt",
]


def _xdg(var: str, fallback: str) -> str:
    value = os.environ.get(var, "").strip()
    if value and os.path.isabs(value):
        return value
    return os.path.expanduser(fallback)


CONFIG_DIR: str = os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), "hyde-ai")
CONFIG_PATH: str = os.path.join(CONFIG_DIR, "config.json")

STATE_DIR: str = os.path.join(_xdg("XDG_STATE_HOME", "~/.local/state"), "hyde-ai")
CACHE_DIR: str = os.path.join(_xdg("XDG_CACHE_HOME", "~/.cache"), "hyde-ai")

# Where HyDE's wallbash renders our stylesheet. Kept here so every module
# agrees on the path without hardcoding it three times.
WALLBASH_CSS: str = os.path.join(
    _xdg("XDG_CACHE_HOME", "~/.cache"), "hyde", "wallbash", "hyde-ai.css"
)


DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant embedded in a desktop sidebar on {DISTRO} "
    "running {DE}. The current date and time is {DATETIME}.\n"
    "\n"
    "Be concise and conversational. Lead with the answer, then add only the "
    "detail that earns its place. Use GitHub-flavoured Markdown: fenced code "
    "blocks with a language tag, tables when comparing things, and LaTeX "
    "between $$ delimiters for mathematics. Do not pad replies with "
    "restatements of the question or offers to help further."
)


DEFAULTS: Dict[str, Any] = {
    "version": 1,
    # Which provider/model the UI should open with.
    "provider": "anthropic",
    # Last-used model per provider, so switching back and forth is sticky.
    "models": {
        "anthropic": "claude-opus-5",
        "gemini": "gemini-3.7-flash",
        "openai": "gpt-5.6-sol",
        "ollama": "",
    },
    # API keys. Empty string means "not set here"; the provider then falls
    # back to the environment variable, and is otherwise unavailable.
    "api_keys": {
        "anthropic": "",
        "gemini": "",
        "openai": "",
        "ollama": "",
    },
    # Panel geometry. Every value here is written back by /side and /width
    # (and by `hyde-ai --side/--width`), so changes survive a restart.
    "sidebar": {
        # "right" or "left"
        "edge": "right",
        # Fraction of the monitor width, clamped between width_min/width_max.
        "width_fraction": 0.28,
        "width_min": 420,
        "width_max": 900,
        "namespace": "hyde-ai",
    },
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "max_tokens": 4096,
    # None => use each provider's own default. A float 0-2 is sent only to
    # providers/models that still accept sampling parameters.
    "temperature": None,
    "request": {
        "connect_timeout": 15.0,
        "read_timeout": 180.0,
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        # "default" sends nothing (provider default), "summarized" asks for
        # visible reasoning summaries, "off" disables extended thinking.
        "thinking": "default",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "reasoning_effort": "",
    },
    "ollama": {
        "base_url": "http://localhost:11434",
        "num_ctx": 0,
        "keep_alive": "5m",
    },
    # Escape hatch: any endpoint speaking the OpenAI or Ollama wire format.
    # See providers.py :: build_custom_provider for the accepted shape.
    "custom_providers": [],
    # Chat behaviour. "chat.system_prompt" is the canonical prompt the UI
    # reads; the top-level "system_prompt" above is kept as an alias for
    # older configs and is resolved by Config.system_prompt().
    "chat": {
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "max_history_messages": 40,
    },
    # Layer-shell surface geometry.
    "sidebar": {
        "namespace": "hyde-ai",
        "edge": "right",
        "width_fraction": 0.28,
        "width_min": 420,
        "width_max": 760,
        "animation_ms": 260,
        "layer": "overlay",
    },
    "ui": {
        "follow_stream": True,
        "restore_last_session": True,
        "code_line_numbers": True,
        # "auto" tracks the wallbash light/dark mode.
        "code_style_scheme": "auto",
    },
}


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``base`` updated by ``overlay``, recursing into plain dicts.

    Lists and scalars from ``overlay`` replace those in ``base`` outright;
    only mappings are merged key-by-key. Keys present in ``overlay`` but not
    in ``base`` are preserved, so data written by other components survives a
    load/save round-trip.
    """
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _ensure_dir(path: str, mode: int = 0o700) -> None:
    try:
        os.makedirs(path, mode=mode, exist_ok=True)
    except OSError:
        return
    try:
        current = stat.S_IMODE(os.stat(path).st_mode)
        if current & 0o077:
            os.chmod(path, mode)
    except OSError:
        pass


class Config:
    """Mutable view over the on-disk JSON config.

    Values are reached with dotted paths::

        cfg.get("api_keys.anthropic", "")
        cfg.set("ui.width", 520)
        cfg.save()
    """

    def __init__(self, data: Optional[Dict[str, Any]] = None, path: str = CONFIG_PATH):
        self.path = path
        self.data: Dict[str, Any] = _deep_merge(DEFAULTS, data or {})
        # Populated by load() when the on-disk file could not be parsed, so
        # the UI can show a non-fatal warning instead of silently resetting.
        self.load_error: Optional[str] = None
        self.backup_path: Optional[str] = None

    # -- access ---------------------------------------------------------

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    def __contains__(self, dotted: str) -> bool:
        sentinel = object()
        return self.get(dotted, sentinel) is not sentinel

    # -- convenience used by the provider layer -------------------------

    def api_key(self, provider_id: str) -> str:
        value = self.get("api_keys.%s" % provider_id, "")
        return value.strip() if isinstance(value, str) else ""

    #: Alias. Some callers look for ``get_api_key``; keeping both names means
    #: they read from this object rather than falling back to a stale copy.
    def get_api_key(self, provider_id: str) -> str:
        return self.api_key(provider_id)

    def set_api_key(self, provider_id: str, key: str) -> None:
        self.set("api_keys.%s" % provider_id, (key or "").strip())

    def model_for(self, provider_id: str, default: str = "") -> str:
        value = self.get("models.%s" % provider_id, "")
        return value.strip() if isinstance(value, str) and value.strip() else default

    def set_model_for(self, provider_id: str, model: str) -> None:
        self.set("models.%s" % provider_id, model or "")

    def system_prompt(self) -> str:
        """The active system prompt, with placeholders already expanded.

        Prefers ``chat.system_prompt`` and falls back to the legacy top-level
        ``system_prompt`` so an older config keeps working.
        """
        value = self.get("chat.system_prompt", None)
        if not isinstance(value, str) or not value.strip():
            value = self.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
        if not isinstance(value, str):
            value = DEFAULT_SYSTEM_PROMPT
        return expand_system_prompt(value)

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
        return (max(1.0, connect), max(1.0, read))

    # -- persistence ----------------------------------------------------

    def save(self) -> None:
        """Write the config atomically with 0600 permissions.

        Raises OSError only if the directory itself cannot be created or
        written; callers in the UI should catch and surface that.
        """
        _ensure_dir(os.path.dirname(self.path) or ".", 0o700)
        payload = json.dumps(self.data, indent=2, ensure_ascii=False, sort_keys=False)

        fd, tmp = tempfile.mkstemp(
            prefix=".config.", suffix=".json.tmp", dir=os.path.dirname(self.path) or "."
        )
        try:
            # mkstemp already gives 0600; assert it explicitly so the key
            # material is never briefly group/world readable.
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            tmp = None
        finally:
            if tmp is not None and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        # Belt and braces: if the file pre-existed with looser permissions,
        # os.replace kept *our* 0600, but re-assert in case of odd umasks.
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def as_json(self) -> str:
        return json.dumps(self.data, indent=2, ensure_ascii=False)


def load(path: str = CONFIG_PATH) -> Config:
    """Load the config, never raising.

    A missing file yields defaults (and is *not* written; the first save()
    creates it). A corrupt file is renamed aside and defaults are returned
    with ``load_error`` set.
    """
    _ensure_dir(os.path.dirname(path) or ".", 0o700)
    _ensure_dir(STATE_DIR, 0o700)

    if not os.path.exists(path):
        return Config({}, path=path)

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as exc:
        cfg = Config({}, path=path)
        cfg.load_error = "Could not read %s: %s" % (path, exc.strerror or exc)
        return cfg

    if not raw.strip():
        return Config({}, path=path)

    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        cfg = Config({}, path=path)
        backup = "%s.corrupt-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        try:
            os.replace(path, backup)
            cfg.backup_path = backup
        except OSError:
            backup = None
        cfg.load_error = "%s was not valid JSON (%s); defaults loaded%s." % (
            path,
            exc,
            " and the old file was kept at %s" % backup if backup else "",
        )
        return cfg

    if not isinstance(parsed, dict):
        cfg = Config({}, path=path)
        cfg.load_error = "%s did not contain a JSON object; defaults loaded." % path
        return cfg

    # Repair the permissions of a config that was created by hand or by an
    # older version before this was enforced.
    try:
        if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        pass

    return Config(parsed, path=path)


def _detect_distro() -> str:
    for candidate in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                fields = {}
                for line in handle:
                    if "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    fields[key.strip()] = value.strip().strip('"').strip("'")
            name = fields.get("PRETTY_NAME") or fields.get("NAME")
            if name:
                return name
        except OSError:
            continue
    return platform.system() or "Linux"


def expand_system_prompt(template: str, **extra: str) -> str:
    """Substitute ``{DISTRO}``/``{DATETIME}``/``{DE}`` (and any extras).

    Unknown placeholders are left untouched rather than raising, so a user
    editing the prompt by hand cannot break the app with a stray brace.
    """
    values = {
        "DISTRO": _detect_distro(),
        "DATETIME": datetime.datetime.now().strftime("%A %d %B %Y, %H:%M"),
        "DE": os.environ.get("XDG_CURRENT_DESKTOP")
        or os.environ.get("XDG_SESSION_DESKTOP")
        or "Hyprland",
        "WINDOWCLASS": "",
    }
    values.update({k: v for k, v in extra.items() if v is not None})

    out = template or ""
    for key, value in values.items():
        out = out.replace("{%s}" % key, str(value))
    return out


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    cfg = load()
    if cfg.load_error:
        print("load_error:", cfg.load_error)
    print("path:", cfg.path)
    print("exists:", os.path.exists(cfg.path))
    print(cfg.as_json())
