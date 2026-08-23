#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hyde-ai :: GTK4 layer-shell sidebar UI.

This module owns *only* the user interface.  It knows nothing about HTTP,
providers or API keys -- everything it needs is injected by ``main.py``:

    Sidebar(app, config=..., registry=..., history=..., theme=...)

Injected collaborator contracts
-------------------------------
config   -- ``get(dotted_key, default)`` / ``set(dotted_key, value)`` / ``save()``
registry -- ``list_providers()``  -> list of provider info objects/dicts with
                                     ``id``, ``name``, ``available`` (bool),
                                     ``hint`` (str, why unavailable), ``models``
            ``models(pid)``       -> list of model info (``id``, ``name``, ``description``)
            ``set_api_key(pid, value)``
            ``new_cancel()``      -> optional; a cancel token (either
                                     ``providers.CancelToken`` or anything with
                                     ``cancel()``/``cancelled`` or ``set()``/
                                     ``is_set()`` -- see ``cancel_is_set``)
            ``default_model(pid)``-> optional; the provider's preferred model
            ``stream(pid, model_id, messages, system, cancel)``
                                  -> iterator of str deltas.  ``messages`` is a
                                     list of ``{"role": "user"|"assistant",
                                     "content": str}``.  Raises on failure; the
                                     exception text is shown to the user.
history  -- ``messages`` (list of dicts), ``add(role, content, **meta)``,
            ``replace_last_content(text)``, ``new_conversation(p, m)``,
            ``delete(msg_id)``, ``truncate_from(msg_id)``, ``save()``,
            ``provider`` / ``model`` properties.
theme    -- ``ThemeManager`` instance (defined below).

Styling rule: this file contains **no palette whatsoever**.  Every colour comes
from the wallbash-generated stylesheet at ~/.cache/hyde/wallbash/hyde-ai.css.
The built-in fallback stylesheet is strictly structural (metrics only).
"""

# --------------------------------------------------------------------------
# gtk4-layer-shell MUST be loaded before GTK pulls in libwayland-client,
# otherwise the surface silently degrades to an ordinary toplevel and only a
# warning is printed on stderr.  This has to be the first executable code.
# --------------------------------------------------------------------------
import ctypes as _ctypes

LAYER_SHELL_OK = True
try:
    _ctypes.CDLL("libgtk4-layer-shell.so.0", mode=_ctypes.RTLD_GLOBAL)
except OSError:  # pragma: no cover - only on a broken install
    LAYER_SHELL_OK = False

import os
import re
import sys
import html
import weakref
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LS
except (ValueError, ImportError):  # pragma: no cover
    LS = None
    LAYER_SHELL_OK = False

HAVE_SOURCEVIEW = True
try:
    gi.require_version("GtkSource", "5")
    from gi.repository import GtkSource
except (ValueError, ImportError):  # pragma: no cover
    GtkSource = None
    HAVE_SOURCEVIEW = False

from gi.repository import Gtk, Gdk, Gio, GLib, Pango  # noqa: E402


# --------------------------------------------------------------------------
# Paths / constants
# --------------------------------------------------------------------------

CSS_PATH = os.path.join(
    GLib.get_user_cache_dir() or os.path.expanduser("~/.cache"),
    "hyde", "wallbash", "hyde-ai.css",
)
WALL_DCOL = os.path.join(
    GLib.get_user_cache_dir() or os.path.expanduser("~/.cache"),
    "hyde", "wall.dcol",
)

DEFAULT_WIDTH_FRACTION = 0.30
DEFAULT_WIDTH_MIN = 420
DEFAULT_WIDTH_MAX = 900
ANIM_US = 240_000            # slide duration, microseconds
DELTA_FLUSH_MS = 40          # streaming coalescing interval
# teto do bloco de raciocinio, em px: mostra o suficiente sem engolir a tela
THINK_MAX_H = 180

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_INTERFACE = "interface"   # shell-generated, never sent to the model

# ---------------------------------------------------------------------------
# Structural fallback stylesheet.
#
# NO COLOURS.  Metrics, spacing and typography only, so that the app is usable
# before/without the wallbash render while still deriving every colour from the
# generated stylesheet (or, failing that, from the GTK theme).
# ---------------------------------------------------------------------------
FALLBACK_CSS = """
window.hyde-ai { background: transparent; }

.panel {
  margin: 5px 5px 5px 0;
  border-radius: 19px;
  padding: 0;
}

.header { padding: 8px 8px 4px 12px; }
.header-title { font-weight: bold; margin-right: 6px; }
.selector { padding: 2px 8px; min-height: 26px; border-radius: 12px; }
.selector-label { font-size: 0.9em; }
.icon-btn { min-width: 28px; min-height: 28px; padding: 2px; border-radius: 12px; }

.banner { padding: 8px 12px; margin: 0 10px 6px 10px; border-radius: 12px; }
.banner-label { font-size: 0.9em; }

.chat { padding: 10px 10px 4px 10px; }

.placeholder { padding: 32px 20px; }
.placeholder-title { font-size: 1.3em; font-weight: bold; margin-top: 10px; }
.placeholder-body { font-size: 0.92em; margin-top: 6px; }

.msg { border-radius: 17px; padding: 7px; }
.msg-header { padding: 4px 6px; border-radius: 12px; min-height: 30px; }
.msg-name { font-weight: bold; font-size: 0.9em; }
.msg-actions button { min-width: 24px; min-height: 24px; padding: 0; border-radius: 10px; }
.msg-body { padding: 4px 4px 2px 4px; }
.msg-text { }

.md { }

.code-block { border-radius: 12px; margin: 4px 0; }
.code-header { padding: 3px 4px 3px 10px; border-radius: 12px 12px 2px 2px; min-height: 26px; }
.code-lang { font-size: 0.82em; font-family: monospace; }
.code-view { font-family: monospace; font-size: 0.9em; padding: 6px 4px; }
.code-view text { background: none; }

.think { border-radius: 12px; margin: 2px 0; }
.think-header { font-size: 0.86em; padding: 2px 4px; }
.think-body { font-size: 0.9em; padding: 6px 8px; border-radius: 10px; }

.input-area { padding: 6px 10px 10px 10px; }
.input-frame { border-radius: 13px; padding: 2px; }
.input { padding: 8px; font-size: 0.98em; background: none; }
.input text { background: none; }
.input-placeholder { padding: 10px; font-size: 0.98em; }
.input-controls { padding: 2px 4px 2px 6px; }
.chip { font-family: monospace; font-size: 0.82em; padding: 1px 8px; min-height: 22px; border-radius: 10px; }
.send-btn { min-width: 32px; min-height: 32px; padding: 0; border-radius: 12px; }
.scroll-bottom { padding: 3px 12px; border-radius: 14px; margin-bottom: 8px; }
.dim { font-size: 0.85em; }
"""


# ==========================================================================
# Theme / CSS live reload
# ==========================================================================

class ThemeManager:
    """Loads the wallbash stylesheet and hot-reloads it when wallbash rewrites it.

    HyDE's renderer (``color.set.sh``) writes via ``mktemp`` + ``mv``, replacing
    the inode.  A monitor that only listens for CHANGES_DONE_HINT misses that,
    so the whole union of relevant events is handled.
    """

    def __init__(self, display=None, path=CSS_PATH):
        self.path = path
        self.display = display or Gdk.Display.get_default()
        self._pending = 0
        self._listeners = []

        self.base = Gtk.CssProvider()
        self.theme = Gtk.CssProvider()
        self.base.connect("parsing-error", self._on_parse_error)
        self.theme.connect("parsing-error", self._on_parse_error)

        if self.display is not None:
            Gtk.StyleContext.add_provider_for_display(
                self.display, self.base, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            # USER priority so the generated palette always beats our metrics.
            Gtk.StyleContext.add_provider_for_display(
                self.display, self.theme, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1
            )

        self._load_base()
        self.reload()

        self.monitor = None
        try:
            gfile = Gio.File.new_for_path(self.path)
            self.monitor = gfile.monitor_file(Gio.FileMonitorFlags.NONE, None)
            self.monitor.set_rate_limit(150)
            self.monitor.connect("changed", self._on_file_changed)
        except GLib.Error as exc:  # pragma: no cover
            _warn("could not watch %s: %s" % (self.path, exc.message))

    # -- public ----------------------------------------------------------
    def connect_reload(self, callback):
        """Register ``callback()`` to run after every successful CSS reload."""
        self._listeners.append(callback)

    def reload(self):
        if not os.path.isfile(self.path):
            self.theme.load_from_string("")
            return False
        try:
            self.theme.load_from_path(self.path)
        except GLib.Error as exc:
            _warn("failed to load %s: %s" % (self.path, exc.message))
            return False
        return True

    # -- internals -------------------------------------------------------
    def _load_base(self):
        try:
            self.base.load_from_string(FALLBACK_CSS)
        except GLib.Error as exc:  # pragma: no cover
            _warn("built-in stylesheet rejected: %s" % exc.message)

    def _on_parse_error(self, provider, section, error):
        _warn("CSS parse error: %s" % error.message)

    def _on_file_changed(self, monitor, gfile, other, event):
        wanted = (
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.CHANGED,
            Gio.FileMonitorEvent.MOVED_IN,
            Gio.FileMonitorEvent.RENAMED,
        )
        if event not in wanted:
            return
        if self._pending:
            GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(80, self._do_reload)

    def _do_reload(self):
        self._pending = 0
        self.reload()
        for cb in list(self._listeners):
            try:
                cb()
            except Exception as exc:  # pragma: no cover
                _warn("theme listener failed: %r" % (exc,))
        return GLib.SOURCE_REMOVE


def _warn(msg):
    print("[hyde-ai] %s" % msg, file=sys.stderr, flush=True)


# ==========================================================================
# Small helpers
# ==========================================================================

def copy_to_clipboard(widget, text):
    display = None
    if widget is not None:
        display = widget.get_display()
    if display is None:
        display = Gdk.Display.get_default()
    if display is None:
        return False
    try:
        display.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(text))
        return True
    except Exception as exc:  # pragma: no cover
        _warn("clipboard failed: %r" % (exc,))
        return False


def _icon_button(icon_name, tooltip, css=("icon-btn",)):
    btn = Gtk.Button()
    btn.set_icon_name(icon_name)
    btn.set_tooltip_text(tooltip)
    btn.set_has_frame(False)
    btn.set_valign(Gtk.Align.CENTER)
    for c in css:
        btn.add_css_class(c)
    btn.add_css_class("flat")
    return btn


def _spacer():
    box = Gtk.Box()
    box.set_hexpand(True)
    return box


def _flash_icon(button, temp_icon, restore_icon, ms=1400):
    button.set_icon_name(temp_icon)

    def restore():
        try:
            button.set_icon_name(restore_icon)
        except Exception:
            pass
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(ms, restore)


# -- cancellation ----------------------------------------------------------
# The provider layer may hand us a rich cancel token (``providers.CancelToken``,
# whose ``cancelled`` is a *property* and which closes the live socket) or we may
# fall back to a plain ``threading.Event``.  These two helpers speak both.

def cancel_request(token):
    if token is None:
        return
    fn = getattr(token, "cancel", None)
    if callable(fn):
        fn()
        return
    fn = getattr(token, "set", None)
    if callable(fn):
        fn()


def cancel_is_set(token):
    if token is None:
        return False
    value = getattr(token, "cancelled", None)
    if value is not None:
        return bool(value() if callable(value) else value)
    fn = getattr(token, "is_set", None)
    if callable(fn):
        return bool(fn())
    return False


# ==========================================================================
# Markdown -> blocks
# ==========================================================================

# Closed fenced code blocks and closed <think> sections.
_BLOCK_RE = re.compile(r"```([^\n`]*)\n?(.*?)```|<think>(.*?)</think>", re.S)


def parse_blocks(md):
    """Split markdown into ``(kind, lang, content, closed)`` tuples.

    ``kind`` is ``"text"``, ``"code"`` or ``"think"``.  An unterminated fence or
    ``<think>`` at the tail yields ``closed=False`` so that streaming renders
    progressively instead of flickering.
    """
    out = []
    if not md:
        return out

    pos = 0
    for m in _BLOCK_RE.finditer(md):
        pre = md[pos:m.start()]
        if pre.strip():
            out.append(("text", "", pre.strip("\n"), True))
        if m.group(3) is not None:
            out.append(("think", "", m.group(3).strip("\n"), True))
        else:
            out.append(("code", (m.group(1) or "").strip(), m.group(2), True))
        pos = m.end()

    rest = md[pos:]
    if rest:
        f = rest.find("```")
        t = rest.find("<think>")
        candidates = [x for x in (f, t) if x != -1]
        if not candidates:
            if rest.strip():
                out.append(("text", "", rest.strip("\n"), True))
        else:
            idx = min(candidates)
            pre = rest[:idx]
            if pre.strip():
                out.append(("text", "", pre.strip("\n"), True))
            if idx == f:
                body = rest[idx + 3:]
                nl = body.find("\n")
                if nl == -1:
                    lang, code = body.strip(), ""
                else:
                    lang, code = body[:nl].strip(), body[nl + 1:]
                out.append(("code", lang, code, False))
            else:
                out.append(("think", "", rest[idx + 7:].strip("\n"), False))
    return out



# ---- matematica: LaTeX -> Unicode / Pango ---------------------------------
# Os modelos emitem TeX com frequencia ($x^2$, \alpha, \frac{a}{b}, \times).
# Sem isto o usuario ve o codigo cru no meio da frase.

_TEX_SIMBOLOS = {
    # gregas minusculas
    "alpha": "\u03b1", "beta": "\u03b2", "gamma": "\u03b3", "delta": "\u03b4",
    "epsilon": "\u03b5", "varepsilon": "\u03b5", "zeta": "\u03b6", "eta": "\u03b7",
    "theta": "\u03b8", "vartheta": "\u03d1", "iota": "\u03b9", "kappa": "\u03ba",
    "lambda": "\u03bb", "mu": "\u03bc", "nu": "\u03bd", "xi": "\u03be",
    "pi": "\u03c0", "rho": "\u03c1", "sigma": "\u03c3", "tau": "\u03c4",
    "upsilon": "\u03c5", "phi": "\u03c6", "varphi": "\u03d5", "chi": "\u03c7",
    "psi": "\u03c8", "omega": "\u03c9",
    # gregas maiusculas
    "Gamma": "\u0393", "Delta": "\u0394", "Theta": "\u0398", "Lambda": "\u039b",
    "Xi": "\u039e", "Pi": "\u03a0", "Sigma": "\u03a3", "Upsilon": "\u03a5",
    "Phi": "\u03a6", "Psi": "\u03a8", "Omega": "\u03a9",
    # operadores e relacoes
    "times": "\u00d7", "div": "\u00f7", "cdot": "\u00b7", "pm": "\u00b1",
    "mp": "\u2213", "leq": "\u2264", "le": "\u2264", "geq": "\u2265",
    "ge": "\u2265", "neq": "\u2260", "ne": "\u2260", "approx": "\u2248",
    "equiv": "\u2261", "sim": "\u223c", "propto": "\u221d", "ll": "\u226a",
    "gg": "\u226b", "infty": "\u221e", "partial": "\u2202", "nabla": "\u2207",
    "sum": "\u2211", "prod": "\u220f", "int": "\u222b", "oint": "\u222e",
    "sqrt": "\u221a", "angle": "\u2220", "perp": "\u22a5", "parallel": "\u2225",
    "degree": "\u00b0", "circ": "\u2218", "ast": "\u2217", "star": "\u22c6",
    # conjuntos e logica
    "in": "\u2208", "notin": "\u2209", "ni": "\u220b", "subset": "\u2282",
    "subseteq": "\u2286", "supset": "\u2283", "supseteq": "\u2287",
    "cup": "\u222a", "cap": "\u2229", "emptyset": "\u2205", "varnothing": "\u2205",
    "forall": "\u2200", "exists": "\u2203", "nexists": "\u2204",
    "land": "\u2227", "wedge": "\u2227", "lor": "\u2228", "vee": "\u2228",
    "neg": "\u00ac", "therefore": "\u2234", "because": "\u2235",
    "mathbb{R}": "\u211d", "mathbb{N}": "\u2115", "mathbb{Z}": "\u2124",
    "mathbb{Q}": "\u211a", "mathbb{C}": "\u2102",
    # setas
    "to": "\u2192", "rightarrow": "\u2192", "Rightarrow": "\u21d2",
    "leftarrow": "\u2190", "Leftarrow": "\u21d0", "leftrightarrow": "\u2194",
    "Leftrightarrow": "\u21d4", "mapsto": "\u21a6", "uparrow": "\u2191",
    "downarrow": "\u2193", "implies": "\u21d2", "iff": "\u21d4",
    # reticencias e espacos
    "ldots": "\u2026", "cdots": "\u22ef", "dots": "\u2026", "vdots": "\u22ee",
    "quad": "  ", "qquad": "    ", ",": " ", ";": " ", "!": "",
}

_TEX_CMD = re.compile(r"\\(mathbb\{[A-Z]\}|[A-Za-z]+|[,;!])")
_CHAVE = r"((?:[^{}]|\{[^{}]*\})*)"
_TEX_FRAC = re.compile(r"\\(?:d|t)?frac\s*\{" + _CHAVE + r"\}\s*\{" + _CHAVE + r"\}")
_TEX_SQRT = re.compile(r"\\sqrt\s*\{" + _CHAVE + r"\}")
_TEX_TEXT = re.compile(r"\\(?:text|mathrm|mathbf|operatorname)\s*\{([^{}]*)\}")
_SUP = re.compile(r"\^\{([^{}]+)\}|\^(\w)")
_SUB = re.compile(r"_\{([^{}]+)\}|_(\w)")

# $$...$$ e \[...\] (bloco) antes de $...$ e \(...\) (inline)


# pylatexenc traz a tabela completa de simbolos LaTeX; a tabela manual abaixo
# fica como reserva para quando ele nao estiver instalado.
try:
    from pylatexenc.latex2text import LatexNodes2Text as _L2T
    _l2t = _L2T(math_mode="text", keep_comments=False, strict_latex_spaces=False)
except Exception:
    _l2t = None


def _tex_para_unicode(t):
    """Converte um trecho de TeX em texto legivel com marcacao Pango."""
    if _l2t is not None:
        try:
            # expoentes/indices primeiro: o pylatexenc os achataria
            # o texto chega ja HTML-escapado; desfaz antes do parser TeX
            cru = html.unescape(t)
            marcado = _SUP.sub(lambda m: "\x01%s\x02" % (m.group(1) or m.group(2)), cru)
            marcado = _SUB.sub(lambda m: "\x03%s\x04" % (m.group(1) or m.group(2)), marcado)
            saida = _l2t.latex_to_text(marcado)
            saida = (html.escape(saida, quote=True)
                     .replace("\x01", "<sup>").replace("\x02", "</sup>")
                     .replace("\x03", "<sub>").replace("\x04", "</sub>"))
            if saida.strip():
                return saida.strip()
        except Exception:
            pass
    t = _TEX_TEXT.sub(lambda m: m.group(1), t)
    t = _TEX_FRAC.sub(lambda m: "%s/%s" % (_env(m.group(1)), _env(m.group(2))), t)
    t = _TEX_SQRT.sub(lambda m: "\u221a(%s)" % m.group(1), t)
    # \alpha\beta em TeX nao tem espaco, mas em Unicode os simbolos colam;
    # um espaco fino mantem a legibilidade sem alargar demais
    t = _TEX_CMD.sub(
        lambda m: _TEX_SIMBOLOS.get(m.group(1), "\\" + m.group(1)) + "\u2009", t)
    t = re.sub(r"\u2009+", "\u2009", t)
    t = re.sub(r"\u2009([)\],.;:])", r"\1", t)
    t = _SUP.sub(lambda m: "<sup>%s</sup>" % (m.group(1) or m.group(2)), t)
    t = _SUB.sub(lambda m: "<sub>%s</sub>" % (m.group(1) or m.group(2)), t)
    t = t.replace("\\left", "").replace("\\right", "")
    t = re.sub(r"\s*&amp;\s*", " ", t)          # alinhamento de ambientes
    t = t.replace("\\\\", "  ")
    return t.strip()


def _env(x):
    """Parenteses so quando o termo tem mais de um elemento."""
    x = x.strip()
    return x if len(x) <= 1 or x.isalnum() else "(%s)" % x


def _wallbash_is_dark():
    try:
        with open(WALL_DCOL, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("dcol_mode"):
                    return "light" not in line.lower()
    except OSError:
        pass
    return True


# Estado do esquema de cores dos blocos de codigo. Vivia no meio do parser
# antigo e foi removido junto por engano ao deletar aquele intervalo.
_SCHEME = {"id": None, "override": None}
_CODE_BLOCKS = weakref.WeakSet()


def resolve_code_scheme():
    """Pick a GtkSourceView *style scheme name* (not a colour) for code blocks."""
    if _SCHEME["override"]:
        return _SCHEME["override"]
    return "Adwaita-dark" if _wallbash_is_dark() else "Adwaita"


def set_code_scheme_override(name):
    _SCHEME["override"] = name or None


def refresh_code_scheme():
    """Re-resolve the scheme and restyle every live code block."""
    new = resolve_code_scheme()
    if new == _SCHEME["id"]:
        return
    _SCHEME["id"] = new
    for block in list(_CODE_BLOCKS):
        try:
            block.apply_scheme(new)
        except Exception:
            pass


def _lookup_scheme(name):
    if not HAVE_SOURCEVIEW:
        return None
    mgr = GtkSource.StyleSchemeManager.get_default()
    for candidate in (name, "Adwaita-dark", "classic-dark", "Adwaita", "classic"):
        if not candidate:
            continue
        scheme = mgr.get_scheme(candidate)
        if scheme is not None:
            return scheme
    return None


_LANG_ALIASES = {
    # GtkSourceView's "python" id is Python *2*; "python3" is the modern one.
    "python": "python3", "py": "python3", "python3": "python3", "python2": "python",
    "bash": "sh", "shell": "sh", "zsh": "sh", "console": "sh", "shell-session": "sh",
    "sh": "sh", "command": "sh", "bash-session": "sh",
    "javascript": "js", "node": "js", "mjs": "js", "cjs": "js",
    "ts": "typescript", "tsx": "typescript",
    "yml": "yaml", "c++": "cpp", "cxx": "cpp", "cc": "cpp", "hpp": "cpp", "h": "c",
    "rs": "rust", "golang": "go", "md": "markdown", "rb": "ruby",
    "cs": "c-sharp", "csharp": "c-sharp", "c#": "c-sharp",
    "ps1": "powershell", "pwsh": "powershell",
    "dockerfile": "docker", "objective-c": "objc",
    "htm": "html", "conf": "ini", "cfg": "ini", "toml": "toml",
    "make": "makefile", "patch": "diff",
    "text": None, "txt": None, "plain": None, "plaintext": None, "none": None, "": None,
}

_LANG_NAME_INDEX = None


def _language_by_name(key):
    """Fall back to matching a fence tag against a language's display name."""
    global _LANG_NAME_INDEX
    if not HAVE_SOURCEVIEW:
        return None
    if _LANG_NAME_INDEX is None:
        index = {}
        manager = GtkSource.LanguageManager.get_default()
        for language_id in (manager.get_language_ids() or ()):
            language = manager.get_language(language_id)
            if language is None:
                continue
            index.setdefault(language.get_name().lower(), language_id)
        _LANG_NAME_INDEX = index
    return _LANG_NAME_INDEX.get(key)


def _resolve_language(lang):
    if not HAVE_SOURCEVIEW or not lang:
        return None
    raw = lang.strip().lower()
    if not raw:
        return None
    manager = GtkSource.LanguageManager.get_default()
    if raw in _LANG_ALIASES:
        mapped = _LANG_ALIASES[raw]
        if mapped is None:
            return None
        language = manager.get_language(mapped)
        if language is not None:
            return language
    language = manager.get_language(raw)
    if language is not None:
        return language
    by_name = _language_by_name(raw)
    return manager.get_language(by_name) if by_name else None


# ==========================================================================
# Block widgets
# ==========================================================================

class CodeBlock(Gtk.Box):
    """A fenced code block: language header, copy button, highlighted body."""

    def __init__(self, lang, code, closed=True, show_line_numbers=True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("code-block")
        self._lang = ""
        self._code = ""
        self._show_lines = show_line_numbers

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        header.add_css_class("code-header")
        self._lang_label = Gtk.Label(xalign=0.0)
        self._lang_label.add_css_class("code-lang")
        self._lang_label.set_ellipsize(Pango.EllipsizeMode.END)
        header.append(self._lang_label)
        header.append(_spacer())

        self._copy_btn = _icon_button("edit-copy-symbolic", "Copy code", ("code-copy",))
        self._copy_btn.connect("clicked", self._on_copy)
        header.append(self._copy_btn)
        self.append(header)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        scroller.set_propagate_natural_height(True)
        scroller.set_propagate_natural_width(False)
        scroller.add_css_class("code-scroll")

        if HAVE_SOURCEVIEW:
            self._buffer = GtkSource.Buffer()
            self._buffer.set_highlight_matching_brackets(False)
            self._view = GtkSource.View(buffer=self._buffer)
            self._view.set_show_line_numbers(self._show_lines)
            self._view.set_highlight_current_line(False)
            self._view.set_monospace(True)
        else:  # pragma: no cover - gtksourceview is installed on this machine
            self._buffer = Gtk.TextBuffer()
            self._view = Gtk.TextView(buffer=self._buffer)
            self._view.set_monospace(True)

        self._view.set_editable(False)
        self._view.set_cursor_visible(False)
        self._view.set_wrap_mode(Gtk.WrapMode.NONE)
        self._view.set_left_margin(6)
        self._view.set_right_margin(6)
        self._view.set_top_margin(4)
        self._view.set_bottom_margin(4)
        self._view.add_css_class("code-view")
        scroller.set_child(self._view)
        self.append(scroller)

        _CODE_BLOCKS.add(self)
        self.apply_scheme(_SCHEME["id"] or resolve_code_scheme())
        self.update(lang, code, closed)

    # -- api -------------------------------------------------------------
    def apply_scheme(self, name):
        if HAVE_SOURCEVIEW:
            scheme = _lookup_scheme(name)
            if scheme is not None:
                self._buffer.set_style_scheme(scheme)

    def update(self, lang, code, closed=True):
        if lang != self._lang:
            self._lang = lang
            label = lang.strip() or "plain"
            if HAVE_SOURCEVIEW:
                language = _resolve_language(lang)
                self._buffer.set_language(language)
                if language is not None:
                    label = language.get_name()
            self._lang_label.set_text(label)
        if code != self._code:
            self._code = code
            body = code[:-1] if code.endswith("\n") else code
            self._buffer.set_text(body, -1)

    def get_code(self):
        return self._code

    # -- callbacks -------------------------------------------------------
    def _on_copy(self, _button):
        if copy_to_clipboard(self, self._code):
            _flash_icon(self._copy_btn, "object-select-symbolic", "edit-copy-symbolic")


class ThinkBlock(Gtk.Box):
    """Collapsible reasoning section: 'Thinking...' while open, 'Thought' when done."""

    def __init__(self, content, closed=True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("think")
        self._content = None
        self._closed = None

        self._expander = Gtk.Expander()
        self._expander.set_expanded(False)
        self._expander.add_css_class("think-header")

        self._label = Gtk.Label(xalign=0.0)
        self._label.set_wrap(True)
        self._label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._label.set_selectable(True)
        self._label.add_css_class("think-body")
        self._label.add_css_class("dim")
        self._expander.set_child(self._label)
        self.append(self._expander)
        self.update(content, closed)

    def update(self, content, closed=True):
        if closed != self._closed:
            self._closed = closed
            self._expander.set_label("Thought" if closed else "Thinking…")
        if content != self._content:
            self._content = content
            self._label.set_text(content)


# Matematica em bloco vira imagem tipografada (mathtext do matplotlib).

try:
    import mathrender as _mathrender
except Exception:  # pragma: no cover
    _mathrender = None

try:
    import speedstats as _vel
except Exception:  # pragma: no cover
    _vel = None

try:
    import mdrender as _md
except Exception:  # pragma: no cover
    _md = None


def _cor_texto():
    """Cor do texto vinda do wallbash, para a imagem casar com o tema."""
    try:
        caminho = os.path.expanduser("~/.cache/hyde/wallbash/eww.scss")
        for line in open(caminho):
            if line.startswith("$fg:"):
                return "#" + line.split("#", 1)[1].split(";")[0].strip()
    except Exception:
        pass
    return "#E8EBF7"


# Tabelas GFM. Antes eram despejadas cruas em <tt>, com o separador ":---",
# os "**" e o "$x$" visiveis. Os modelos usam tabela o tempo todo.





class TableBlock(Gtk.Grid):
    """Tabela vinda do parser: celulas ja em markup, alinhamento do GFM."""

    def __init__(self, cabec, alin, linhas):
        super().__init__()
        self.add_css_class("md-table")
        self.set_halign(Gtk.Align.START)
        self.set_valign(Gtk.Align.START)
        self.set_vexpand(False)

        ncols = max([len(cabec)] + [len(r) for r in linhas] + [1])
        while len(alin) < ncols:
            alin.append(0.0)

        def celula(markup, col, idx, cabecalho=False):
            lbl = Gtk.Label(xalign=alin[col])
            lbl.set_wrap(True)
            lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            lbl.set_max_width_chars(28)
            lbl.set_selectable(True)
            lbl.add_css_class("td-head" if cabecalho else "td")
            if idx % 2 == 1 and not cabecalho:
                lbl.add_css_class("td-alt")
            try:
                lbl.set_markup(markup or "")
            except GLib.Error:
                lbl.set_text(re.sub(r"<[^>]+>", "", markup or ""))
            return lbl

        for c in range(ncols):
            self.attach(celula(cabec[c] if c < len(cabec) else "", c, 0, True), c, 0, 1, 1)
        for r, linha in enumerate(linhas, start=1):
            for c in range(ncols):
                self.attach(celula(linha[c] if c < len(linha) else "", c, r), c, r, 1, 1)


# Fator sobre o tamanho nominal do SVG: o mathtext ja compoe na proporcao
# certa, isto so amplia para leitura confortavel no painel.
_MATH_ESCALA = 1.25       # display um tico maior que a prosa, como no LaTeX
# Corpo da formula, em pontos. Proximo do texto (15px) com leve destaque.
_MATH_CORPO = 13


def _math_inline_markup(tex):
    """TeX inline -> markup Pango. Chamado pelo parser via gancho."""
    return '<span size="larger"><i>%s</i></span>' % _tex_para_unicode(
        html.escape(tex, quote=True))


# Comando LaTeX que sobreviveu a todas as conversoes.
_TEX_SOBRA = re.compile(r"\\[A-Za-z]+(?:\{[^{}]*\})*")


def _math_solto_markup(markup):
    """Converte comandos TeX soltos DENTRO de markup ja escapado.

    Os modelos escrevem TeX na prosa sem delimitador ("a constante
    \\frac{1}{\\sqrt{2\\pi}} existe..."). Sem isto o comando aparece cru.
    Percorremos so os trechos fora de tag, para nao corromper a marcacao.
    """
    partes = re.split(r"(<[^>]+>)", markup)
    for i in range(0, len(partes), 2):
        t = partes[i]
        if "\\" not in t and "^" not in t and "_{" not in t:
            continue
        # passadas ate estabilizar: \\frac{1}{\\sqrt{2\\pi}} precisa que o
        # \\sqrt interno resolva antes de a fracao fazer sentido
        for _ in range(4):
            antes = t
            t = _TEX_TEXT.sub(lambda m: m.group(1), t)
            t = _TEX_SQRT.sub(lambda m: "\u221a(%s)" % m.group(1), t)
            t = _TEX_FRAC.sub(
                lambda m: "%s/%s" % (_env(m.group(1)), _env(m.group(2))), t)
            t = _TEX_CMD.sub(
                lambda m: _TEX_SIMBOLOS.get(m.group(1), m.group(0)), t)
            t = _SUP.sub(lambda m: "<sup>%s</sup>" % (m.group(1) or m.group(2)), t)
            t = _SUB.sub(lambda m: "<sub>%s</sub>" % (m.group(1) or m.group(2)), t)
            if t == antes:
                break
        partes[i] = t

    # Rede final. Se algum comando escapou de todas as conversoes, ele vira
    # monoespacado: fica claro que e codigo LaTeX que nao coube, em vez de
    # aparecer como prosa quebrada no meio da frase.
    saida = "".join(partes)
    if _TEX_SOBRA.search(re.sub(r"<[^>]+>", "", saida)):
        pedacos = re.split(r"(<[^>]+>)", saida)
        for i in range(0, len(pedacos), 2):
            if _TEX_SOBRA.search(pedacos[i]):
                pedacos[i] = _TEX_SOBRA.sub(
                    lambda m: "<tt>%s</tt>" % m.group(0), pedacos[i])
        saida = "".join(pedacos)
    return saida


if _md is not None:
    _md.registrar_math_inline(_math_inline_markup)
    _md.registrar_math_solto(_math_solto_markup)


class MathBlock(Gtk.Box):
    """Formula vetorial, tamanho de glifo constante e com quebra de linha.

    Imagem nao quebra sozinha, entao equacao longa virava uma faixa larga com
    rolagem horizontal. Aqui a expressao e partida nos sinais de relacao (como
    o ambiente align do LaTeX) e cada trecho vira uma linha propria. So sobra
    rolagem quando um unico trecho, ja indivisivel, ainda nao cabe.

    O LaTeX de origem fica disponivel: clique copia, clique duplo revela.
    """

    def __init__(self, caminho, tex=""):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self._tex = tex or ""
        self._escala = 0
        self._figuras = []
        self.add_css_class("math-block")
        self.set_halign(Gtk.Align.FILL)

        linhas = [self._tex]
        if _mathrender is not None and self._tex:
            try:
                linhas = _mathrender.quebrar(self._tex)
            except Exception:
                linhas = [self._tex]

        cor = _cor_texto()
        self._caminhos = []
        for parte in linhas:
            p = (_mathrender.render(parte, cor, _MATH_CORPO, display=True)
                 if _mathrender else None)
            if p is None:
                p = caminho if len(linhas) == 1 else None
            if p is None:
                continue
            self._caminhos.append(p)
            fig = Gtk.Picture()
            # can_shrink=False + FILL fazia o Picture exigir a largura toda da
            # coluna e esticar a formula junto: "E = mc^2" saia com 631px de
            # largura para 49px de imagem.
            # CONTAIN e height-for-width: dada a largura da coluna ele pede a
            # altura que preencheria tudo, e o container cresce junto.
            # SCALE_DOWN so encolhe -- formula curta fica no tamanho, formula
            # larga demais cabe sozinha.
            fig.set_can_shrink(True)
            fig.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
            fig.set_halign(Gtk.Align.START)
            fig.set_valign(Gtk.Align.START)
            fig.set_hexpand(False)
            fig.set_vexpand(False)
            self._figuras.append(fig)
            self.append(fig)

        if not self._figuras:                      # nada renderizou
            fig = Gtk.Picture.new_for_filename(caminho)
            fig.set_halign(Gtk.Align.START)
            self._figuras.append(fig)
            self._caminhos.append(caminho)
            self.append(fig)

        self._fonte = Gtk.Label(xalign=0.0)
        self._fonte.add_css_class("math-fonte")
        self._fonte.set_selectable(True)
        self._fonte.set_wrap(True)
        self._fonte.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._fonte.set_visible(False)
        self._fonte.set_text(self._tex)
        self.append(self._fonte)

        if self._tex:
            for fig in self._figuras:
                fig.set_tooltip_text(
                    "Clique: copia o LaTeX  ·  Clique duplo: mostra o codigo")
                fig.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
                g = Gtk.GestureClick()
                g.connect("released", self._on_clique)
                fig.add_controller(g)

        self.connect("realize", lambda *_a: self._render())
        self.connect("notify::scale-factor", lambda *_a: self._render())
        self._render()

    def _on_clique(self, _g, n, _x, _y):
        if n >= 2:
            self._fonte.set_visible(not self._fonte.get_visible())
            return
        if not self._tex:
            return
        try:
            self.get_clipboard().set(self._tex)
        except Exception as exc:
            _warn("nao consegui copiar a formula: %r" % (exc,))
            return
        self.add_css_class("math-copiada")
        GLib.timeout_add(600, lambda: (self.remove_css_class("math-copiada"),
                                       GLib.SOURCE_REMOVE)[1])
        janela = self.get_root()
        mostrar = getattr(janela, "show_banner", None)
        if callable(mostrar):
            mostrar("LaTeX copiado.", timeout_ms=1800)

    def _render(self):
        escala = self.get_scale_factor() or 1
        if escala == self._escala:
            return
        self._escala = escala
        try:
            import gi
            gi.require_version("Rsvg", "2.0")
            from gi.repository import Rsvg
        except Exception:
            return
        for fig, caminho in zip(self._figuras, self._caminhos):
            try:
                ok, larg, alt = (Rsvg.Handle.new_from_file(caminho)
                                 .get_intrinsic_size_in_pixels())
                if not ok or alt <= 0:
                    continue
                w = max(1, int(round(larg * _MATH_ESCALA)))
                h = max(1, int(round(alt * _MATH_ESCALA)))
                pintura = _mathrender.paintable(caminho, w, h, escala)
                if pintura is not None:
                    fig.set_paintable(pintura)
                    fig.set_size_request(w, h)
            except Exception as exc:
                _warn("nao consegui rasterizar a formula: %r" % (exc,))


class ProseBlock(Gtk.Box):
    """Prosa montada a partir dos segmentos do markdown-it.

    Atualiza de forma INCREMENTAL. Durante o streaming esta funcao roda a cada
    token: reconstruir a arvore toda a cada delta fazia a mensagem sumir e
    reaparecer, reparseando markdown e re-renderizando matematica sem parar.
    Agora so o que mudou de fato e tocado; a estrutura so e refeita quando a
    sequencia de segmentos muda de forma.
    """

    def __init__(self, content):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_halign(Gtk.Align.FILL)
        self._content = None
        self._segs = []
        self._widgets = []
        self.update(content)

    # -- ciclo ----------------------------------------------------------
    def update(self, content):
        if content == self._content:
            return
        self._content = content

        if _md is None:
            self._reconstruir([("texto", content)])
            return
        try:
            segs = _md.segmentos(content)
        except Exception as exc:
            _warn("parser de markdown falhou: %r" % (exc,))
            self._reconstruir([("texto", content)])
            return

        # No streaming o texto so CRESCE: os segmentos ja fechados nao mudam
        # e novos aparecem no fim. Preservando o prefixo em comum, nenhum
        # widget estavel e destruido -- e o que tirava a mensagem da tela.
        comum = 0
        while (comum < len(segs) and comum < len(self._segs)
               and segs[comum][0] == self._segs[comum][0]):
            comum += 1

        if comum == len(self._segs) or comum > 0:
            # atualiza o que mudou dentro do prefixo
            for i in range(comum):
                if segs[i] != self._segs[i]:
                    self._atualizar(i, segs[i])
            # descarta o excedente antigo
            for w in self._widgets[comum:]:
                self.remove(w)
            del self._widgets[comum:]
            # acrescenta o que veio depois
            for seg in segs[comum:]:
                w = self._criar(seg)
                if w is not None:
                    self.append(w)
                    self._widgets.append(w)
            self._segs = segs
            if not self._widgets:
                self._reconstruir(segs)
            return

        self._reconstruir(segs)

    def _reconstruir(self, segs):
        child = self.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.remove(child)
            child = nxt
        self._widgets = []
        for seg in segs:
            w = self._criar(seg)
            if w is not None:
                self.append(w)
                self._widgets.append(w)
        self._segs = segs
        if not self._widgets:
            vazio = TextBlock(self._content or "")
            self.append(vazio)
            self._widgets.append(vazio)

    # -- por segmento ---------------------------------------------------
    def _criar(self, seg):
        if seg[0] == "texto":
            return TextBlock(seg[1], pronto=True)
        if seg[0] == "tabela":
            return TableBlock(seg[1], list(seg[2]), seg[3])
        if seg[0] == "math":
            caminho = (_mathrender.render(seg[1], _cor_texto(), _MATH_CORPO, display=True)
                       if _mathrender else None)
            if caminho:
                return MathBlock(caminho, tex=seg[1])
            return TextBlock("<i>%s</i>" % html.escape(seg[1]), pronto=True)
        return None

    def _atualizar(self, i, seg):
        if i >= len(self._widgets):
            return
        w = self._widgets[i]
        if seg[0] == "texto" and isinstance(w, TextBlock):
            w.update(seg[1])          # so troca o markup: barato
            return
        # tabela e matematica mudam pouco; trocar o widget e mais simples
        novo = self._criar(seg)
        if novo is None:
            return
        anterior = self._widgets[i - 1] if i > 0 else None
        self.remove(w)
        self.insert_child_after(novo, anterior)
        self._widgets[i] = novo


class TextBlock(Gtk.Label):
    """A prose block rendered with Pango markup."""

    def __init__(self, content, pronto=False):
        super().__init__(xalign=0.0)
        self.set_wrap(True)
        self.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.set_selectable(True)
        self.set_halign(Gtk.Align.FILL)
        self.add_css_class("md-text")
        self._content = None
        self._pronto = pronto
        self.connect("activate-link", self._on_link)
        self.update(content)

    def update(self, content):
        if content == self._content:
            return
        self._content = content
        try:
            # sem markup pronto, escapa e mostra literal: o parsing de
            # markdown agora e do mdrender, nao mais daqui
            self.set_markup(content if self._pronto
                            else html.escape(content, quote=True))
        except GLib.Error:
            self.set_text(content)

    def _on_link(self, _label, uri):
        try:
            Gtk.UriLauncher.new(uri).launch(None, None, None, None)
        except Exception:
            try:
                Gio.AppInfo.launch_default_for_uri(uri, None)
            except GLib.Error as exc:  # pragma: no cover
                _warn("could not open %s: %s" % (uri, exc.message))
        return True


class MarkdownView(Gtk.Box):
    """Incrementally-diffed markdown renderer.

    ``set_markdown`` only rebuilds the blocks that actually changed, which is
    what keeps streaming smooth instead of flickering the whole message.
    """

    def __init__(self, show_line_numbers=True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_css_class("md")
        self._blocks = []
        self._widgets = []
        self._show_lines = show_line_numbers

    def set_markdown(self, md):
        new = parse_blocks(md)
        old = self._blocks

        overlap = min(len(new), len(old))
        for i in range(overlap):
            if old[i] == new[i]:
                continue
            if old[i][0] == new[i][0] and old[i][1] == new[i][1]:
                self._update_widget(self._widgets[i], new[i])
            else:
                self._replace_widget(i, new[i])

        for i in range(overlap, len(new)):
            widget = self._make_widget(new[i])
            self.append(widget)
            self._widgets.append(widget)

        while len(self._widgets) > len(new):
            widget = self._widgets.pop()
            self.remove(widget)

        self._blocks = new

    def clear(self):
        for widget in self._widgets:
            self.remove(widget)
        self._widgets = []
        self._blocks = []

    # -- internals -------------------------------------------------------
    def _make_widget(self, block):
        kind, lang, content, closed = block
        if kind == "code":
            return CodeBlock(lang, content, closed, self._show_lines)
        if kind == "think":
            return ThinkBlock(content, closed)
        return ProseBlock(content)

    def _update_widget(self, widget, block):
        kind, lang, content, closed = block
        if kind == "code":
            widget.update(lang, content, closed)
        elif kind == "think":
            widget.update(content, closed)
        else:
            widget.update(content)

    def _replace_widget(self, index, block):
        widget = self._make_widget(block)
        sibling = self._widgets[index - 1] if index > 0 else None
        self.remove(self._widgets[index])
        self.insert_child_after(widget, sibling)
        self._widgets[index] = widget


# ==========================================================================
# Message row
# ==========================================================================

_ROLE_META = {
    ROLE_USER: ("avatar-default-symbolic", "You", "msg-user"),
    ROLE_ASSISTANT: ("starred-symbolic", "Assistant", "msg-assistant"),
    ROLE_INTERFACE: ("dialog-information-symbolic", "hyde-ai", "msg-interface"),
}


class _DeltaTexto(object):
    """Evento de texto, para registries que nao rotulam os deltas."""

    __slots__ = ("kind", "text", "data")

    def __init__(self, text):
        self.kind = "text"
        self.text = text
        self.data = {}


def _somente_texto(deltas):
    for delta in deltas:
        if delta:
            yield _DeltaTexto(delta)


# Delimitadores de matematica reconhecidos, do mais longo para o mais curto
# (senao "$$" seria lido como dois "$").
_PARES_MATH = (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$"))


def _corta_math_incompleta(texto):
    """Remove a formula ainda pela metade no fim do texto em streaming.

    Enquanto os tokens chegam, "$b_n = \\frac{2(-1)^{" fica como LaTeX cru na
    tela ate o delimitador fechar. Cortar esse rabo faz a formula aparecer
    inteira, de uma vez, em vez de se montar aos pedacos como codigo.

    Uma unica varredura da esquerda para a direita: fora de matematica procura
    uma abertura, dentro procura o fechamento correspondente. O que sobrar
    aberto no fim e o corte.
    """
    if not texto or ("$" not in texto and "\\" not in texto):
        return texto

    i = 0
    n = len(texto)
    abertura = None          # indice do delimitador aberto, se houver
    fecha_esperado = None
    while i < n:
        if abertura is None:
            for abre, fecha in _PARES_MATH:
                if texto.startswith(abre, i):
                    abertura, fecha_esperado = i, fecha
                    i += len(abre)
                    break
            else:
                i += 1
        else:
            if texto.startswith(fecha_esperado, i):
                i += len(fecha_esperado)
                abertura, fecha_esperado = None, None
            else:
                i += 1

    if abertura is None:
        return texto
    return texto[:abertura].rstrip()


class ThinkingBlock(Gtk.Box):
    """Indicador de raciocinio: pulsa enquanto o modelo pensa, e depois vira
    um resumo clicavel com o rascunho inteiro dentro.

    Enquanto nao ha resposta, este e o unico sinal de que algo esta
    acontecendo -- por isso ele se anuncia; quando a resposta chega, encolhe
    para uma linha e sai da frente.
    """

    _PASSOS = ("", ".", "..", "...")

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("think")
        self._texto = ""
        self._t0 = None
        self._decorrido = 0.0
        self._tick = 0
        self._tick_id = 0
        self._aberto = False

        self._chevron = Gtk.Image.new_from_icon_name("pan-end-symbolic")
        self._chevron.add_css_class("think-chevron")

        self._rotulo = Gtk.Label(xalign=0.0, label="Pensando")
        self._rotulo.add_css_class("think-header")

        linha = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        linha.append(self._chevron)
        linha.append(self._rotulo)

        self._botao = Gtk.Button()
        self._botao.set_child(linha)
        self._botao.add_css_class("flat")
        self._botao.add_css_class("think-toggle")
        self._botao.set_halign(Gtk.Align.START)
        self._botao.connect("clicked", self._alternar)
        self.append(self._botao)

        self._corpo = Gtk.Label(xalign=0.0, label="")
        self._corpo.set_wrap(True)
        self._corpo.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._corpo.set_selectable(True)
        self._corpo.add_css_class("think-body")

        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroll.set_propagate_natural_height(True)
        self._scroll.set_max_content_height(THINK_MAX_H)
        self._scroll.set_child(self._corpo)

        self._revealer = Gtk.Revealer()
        self._revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._revealer.set_transition_duration(180)
        self._revealer.set_child(self._scroll)
        self._revealer.set_reveal_child(False)
        self.append(self._revealer)

    # -- api -------------------------------------------------------------
    @property
    def texto(self):
        return self._texto

    def acrescentar(self, texto):
        if not texto:
            return
        if self._t0 is None:
            self._t0 = time.monotonic()
            self._iniciar_pulso()
        self._texto += texto
        self._corpo.set_text(self._texto)
        adj = self._scroll.get_vadjustment()
        if adj is not None:
            GLib.idle_add(self._colar_no_fim, adj)

    def _colar_no_fim(self, adj):
        adj.set_value(max(0.0, adj.get_upper() - adj.get_page_size()))
        return GLib.SOURCE_REMOVE

    def abrir(self, aberto):
        self._aberto = bool(aberto)
        self._revealer.set_reveal_child(self._aberto)
        self._chevron.set_from_icon_name(
            "pan-down-symbolic" if self._aberto else "pan-end-symbolic")

    def concluir(self):
        """Parou de pensar: congela o tempo e troca o texto pelo resumo."""
        if self._t0 is not None and not self._decorrido:
            self._decorrido = time.monotonic() - self._t0
        self._parar_pulso()
        if self._decorrido >= 0.1:
            self._rotulo.set_text("Pensou por %s" % _duracao(self._decorrido))
        else:
            self._rotulo.set_text("Raciocinio")

    # -- animacao --------------------------------------------------------
    def _iniciar_pulso(self):
        if self._tick_id:
            return
        self._rotulo.add_css_class("think-live")
        self._tick_id = GLib.timeout_add(400, self._pulsar)

    def _pulsar(self):
        self._tick = (self._tick + 1) % len(self._PASSOS)
        self._rotulo.set_text("Pensando" + self._PASSOS[self._tick])
        return GLib.SOURCE_CONTINUE

    def _parar_pulso(self):
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0
        self._rotulo.remove_css_class("think-live")

    def _alternar(self, _btn):
        self.abrir(not self._aberto)


def _duracao(segundos):
    if segundos < 60:
        return "%ds" % round(segundos)
    minutos, resto = divmod(int(round(segundos)), 60)
    return "%dmin %02ds" % (minutos, resto)


class MessageRow(Gtk.Box):
    """One conversation entry: tinted header strip + full-width body."""

    def __init__(self, role, content, name=None, msg_id=None,
                 on_delete=None, on_regenerate=None, show_line_numbers=True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        icon, default_name, css = _ROLE_META.get(role, _ROLE_META[ROLE_INTERFACE])
        self.add_css_class("msg")
        self.add_css_class(css)
        self.set_hexpand(True)
        if role == ROLE_USER:
            # User turns are short; letting them run the full panel width makes
            # the conversation hard to skim. Cap them and push them to the side
            # opposite the model's replies.
            self.set_halign(Gtk.Align.END)
            self.set_hexpand(False)

        self.role = role
        self.msg_id = msg_id
        self.content = content or ""
        self._error = False
        self._streaming = False

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.add_css_class("msg-header")

        image = Gtk.Image.new_from_icon_name(icon)
        image.add_css_class("msg-icon")
        header.append(image)

        self._name_label = Gtk.Label(xalign=0.0, label=name or default_name)
        self._name_label.add_css_class("msg-name")
        self._name_label.set_ellipsize(Pango.EllipsizeMode.END)
        header.append(self._name_label)

        # tempo e velocidade DESTA resposta, discretos ao lado do nome
        self._metricas_label = Gtk.Label(xalign=0.0)
        self._metricas_label.add_css_class("msg-metricas")
        self._metricas_label.set_visible(False)
        header.append(self._metricas_label)

        if role == ROLE_INTERFACE:
            hidden = Gtk.Image.new_from_icon_name("view-conceal-symbolic")
            hidden.set_tooltip_text("Not visible to the model")
            hidden.add_css_class("dim")
            header.append(hidden)

        header.append(_spacer())

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        actions.add_css_class("msg-actions")

        if role == ROLE_ASSISTANT and on_regenerate is not None:
            regen = _icon_button("view-refresh-symbolic", "Regenerate from here")
            regen.connect("clicked", lambda _b: on_regenerate(self))
            actions.append(regen)

        self._copy_btn = _icon_button("edit-copy-symbolic", "Copy message")
        self._copy_btn.connect("clicked", self._on_copy)
        actions.append(self._copy_btn)

        if on_delete is not None:
            delete = _icon_button("edit-delete-symbolic", "Delete message")
            delete.connect("clicked", lambda _b: on_delete(self))
            actions.append(delete)

        header.append(actions)
        self.append(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        body.add_css_class("msg-body")
        body.set_hexpand(True)

        if role == ROLE_USER:
            self._markdown = None
            self._plain = Gtk.Label(xalign=0.0, label=self.content)
            self._plain.set_wrap(True)
            self._plain.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            self._plain.set_selectable(True)
            self._plain.add_css_class("msg-text")
            body.append(self._plain)
        else:
            self._plain = None
            self._markdown = MarkdownView(show_line_numbers=show_line_numbers)
            body.append(self._markdown)

        # Modelos de raciocinio (qwen3, o-series, Claude com thinking) emitem
        # o rascunho antes da resposta. Sem isso a bolha fica vazia enquanto
        # eles pensam -- e vazia de vez se o raciocinio consumir todo o
        # orcamento de tokens.
        self._think_exp = None
        if role == ROLE_ASSISTANT:
            self._think_exp = ThinkingBlock()
            self._think_exp.set_visible(False)
            body.append(self._think_exp)

        self._spinner = Gtk.Spinner()
        self._spinner.set_halign(Gtk.Align.START)
        self._spinner.set_visible(False)
        body.append(self._spinner)

        self.append(body)
        self._render()

    # -- api -------------------------------------------------------------
    def set_content(self, text):
        self.content = text or ""
        self._render()

    def append_content(self, text):
        if not text:
            return
        # A resposta comecou: o rascunho para de pulsar, vira "Pensou por Xs"
        # e se recolhe -- continua a um clique de distancia.
        if self._think_exp is not None and not self.content and self._think_exp.texto:
            self._think_exp.concluir()
            self._think_exp.abrir(False)
        self.content += text
        self._render()

    def append_thinking(self, text):
        """Acumula o rascunho do modelo no bloco de raciocinio."""
        if not text or self._think_exp is None:
            return
        primeiro = not self._think_exp.get_visible()
        self._think_exp.acrescentar(text)
        if primeiro:
            self._think_exp.set_visible(True)
            # so abre sozinho enquanto nao ha resposta: e o unico sinal de
            # que o modelo esta trabalhando
            self._think_exp.abrir(not self.content)
        if not self.content:
            self._spinner.set_visible(False)
            self._spinner.stop()

    def finish_thinking(self):
        if self._think_exp is not None and self._think_exp.texto:
            self._think_exp.concluir()

    @property
    def thinking(self):
        return self._think_exp.texto if self._think_exp is not None else ""

    def set_metricas(self, espera, geracao, vel):
        """Mostra o custo desta resposta: espera + geracao + tok/s dela."""
        partes = []
        if espera >= 0.15:
            partes.append("%.1fs pensando" % espera)
        partes.append("%.1fs" % geracao)
        if vel:
            partes.append("%.0f tok/s" % vel if vel >= 10 else "%.1f tok/s" % vel)
        self._metricas_label.set_text("  ·  " + "  ·  ".join(partes))
        self._metricas_label.set_visible(True)

    def set_streaming(self, streaming):
        antes = self._streaming
        self._streaming = bool(streaming)
        if antes and not self._streaming:
            self._render()          # a fórmula final entra inteira
        if streaming:
            self.add_css_class("streaming")
            if not self.content.strip():
                self._spinner.set_visible(True)
                self._spinner.start()
        else:
            self.remove_css_class("streaming")
            self._spinner.stop()
            self._spinner.set_visible(False)

    def set_error(self, is_error):
        self._error = is_error
        if is_error:
            self.add_css_class("msg-error")
        else:
            self.remove_css_class("msg-error")

    def set_name(self, name):
        self._name_label.set_text(name)

    # -- internals -------------------------------------------------------
    def _render(self):
        if self._markdown is not None:
            texto = self.content
            if self._streaming:
                texto = _corta_math_incompleta(texto)
            self._markdown.set_markdown(texto)
            if self.content.strip():
                self._spinner.stop()
                self._spinner.set_visible(False)
        elif self._plain is not None:
            self._plain.set_text(self.content)

    def _on_copy(self, _button):
        if copy_to_clipboard(self, self.content):
            _flash_icon(self._copy_btn, "object-select-symbolic", "edit-copy-symbolic")


# ==========================================================================
# Selector button (provider / model)
# ==========================================================================

class SelectorButton(Gtk.MenuButton):
    """A dropdown whose unavailable entries stay visible but insensitive."""

    def __init__(self, on_select, empty_label="none"):
        super().__init__()
        self.set_has_frame(False)
        self.add_css_class("selector")
        self.add_css_class("flat")
        self._on_select = on_select
        self._empty_label = empty_label
        self._items = []
        self._active = None
        self._sufixo = ""

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._label = Gtk.Label(label=empty_label, xalign=0.0)
        self._label.add_css_class("selector-label")
        self._label.set_ellipsize(Pango.EllipsizeMode.END)
        self._label.set_max_width_chars(16)
        content.append(self._label)
        content.append(Gtk.Image.new_from_icon_name("pan-down-symbolic"))
        self.set_child(content)

        self._popover = Gtk.Popover()
        self._popover.add_css_class("selector-popover")
        self._popover.set_size_request(320, -1)
        # Re-probe whenever the list is opened, so a model pulled since the
        # panel started shows up without a restart. Set by the Sidebar.
        self.on_open = None
        self._popover.connect("show", self._on_popover_show)
        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._listbox.add_css_class("selector-list")
        self._listbox.connect("row-activated", self._on_row_activated)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(380)
        scroller.set_min_content_width(320)
        # Without this the ScrolledWindow hands the listbox its MINIMUM width,
        # which for a wrapping label is a single character -> vertical letters.
        scroller.set_propagate_natural_width(True)
        scroller.set_child(self._listbox)
        self._popover.set_child(scroller)
        self.set_popover(self._popover)

    def _on_popover_show(self, _popover):
        if callable(self.on_open):
            try:
                self.on_open()
            except Exception as exc:
                _warn("selector refresh failed: %r" % (exc,))

    def set_items(self, items, active_id=None):
        """``items``: list of dicts with id/name/subtitle/sensitive keys."""
        self._items = list(items)
        child = self._listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._listbox.remove(child)
            child = nxt

        for item in self._items:
            row = Gtk.ListBoxRow()
            row.add_css_class("selector-row")
            row._item_id = item["id"]
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            box.set_margin_top(5)
            box.set_margin_bottom(5)
            box.set_margin_start(10)
            box.set_margin_end(10)
            title = Gtk.Label(xalign=0.0, label=item.get("name") or item["id"])
            title.add_css_class("selector-row-title")
            title.set_ellipsize(Pango.EllipsizeMode.END)
            title.set_width_chars(18)
            box.append(title)
            subtitle = item.get("subtitle")
            if subtitle:
                sub = Gtk.Label(xalign=0.0, label=subtitle)
                sub.add_css_class("dim")
                sub.add_css_class("selector-row-sub")
                # Ellipsize rather than wrap: WORD_CHAR wrapping is what let
                # the row collapse to one character per line.
                sub.set_wrap(False)
                sub.set_ellipsize(Pango.EllipsizeMode.END)
                sub.set_max_width_chars(38)
                box.append(sub)
            row.set_child(box)
            if not item.get("sensitive", True):
                row.set_sensitive(False)
                row.set_activatable(False)
            self._listbox.append(row)

        self.set_active(active_id)

    def set_sufixo(self, texto):
        """Texto extra depois do nome -- usado para a media de tok/s."""
        self._sufixo = texto or ""
        self.set_active(self._active)

    def set_active(self, item_id):
        self._active = item_id
        suf = getattr(self, "_sufixo", "")
        for item in self._items:
            if item["id"] == item_id:
                self._label.set_text((item.get("name") or item_id) + suf)
                self.set_tooltip_text(item.get("subtitle") or item.get("name") or item_id)
                return
        self._label.set_text((item_id or self._empty_label) + suf)
        self.set_tooltip_text(item_id or self._empty_label)

    def get_active(self):
        return self._active

    def _on_row_activated(self, _listbox, row):
        self._popover.popdown()
        item_id = getattr(row, "_item_id", None)
        if item_id is None:
            return
        self.set_active(item_id)
        if self._on_select is not None:
            self._on_select(item_id)


# ==========================================================================
# The sidebar window
# ==========================================================================

# Text inset inside the composer. The TextView applies it as margins and the
# overlaid placeholder mirrors it, so the hint sits exactly on the caret.
_INPUT_INSET = 8

HELP_TEXT = """## hyde-ai

**Slash commands**

- `/help` — this message
- `/clear` — start a fresh conversation
- `/provider` — list providers, `/provider <id>` to switch
- `/model` — list models, `/model <id>` to switch
- `/key <provider> <value>` — store an API key
- `/keys` — show which providers have a key
- `/historico` — lista as conversas salvas
- `/refresh` — rescan providers and models now
- `/restart` — restart hyde-ai (rarely needed; models auto-detect)
- `/side left|right` — which edge the panel opens on
- `/width 35` — panel width, as a percent or `700px`

**Keys**

- `Enter` send · `Shift+Enter` newline
- `Escape` hide the sidebar
- `Ctrl+L` clear · `Ctrl+Shift+C` copy last reply
"""


# Catalogo de comandos: usado pela paleta ("/" no campo) e pelo /help, para
# nao existirem duas listas que divergem com o tempo.
COMANDOS = [
    ("/help",      "",                    "mostra a ajuda"),
    ("/clear",     "",                    "comeca uma conversa nova"),
    ("/historico", "",                    "lista as conversas salvas"),
    ("/provider",  "[id]",                "lista ou troca de provedor"),
    ("/model",     "[id]",                "lista ou troca de modelo"),
    ("/key",       "<provedor> <valor>",  "guarda uma chave de API"),
    ("/keys",      "",                    "mostra quais provedores tem chave"),
    ("/refresh",   "",                    "reprocura provedores e modelos"),
    ("/side",      "left|right",          "borda em que o painel abre"),
    ("/width",     "35 | 700px",           "largura do painel"),
    ("/velocidade","",                    "tokens/s medio por modelo"),
    ("/think",     "auto|on|off|low|medium|high", "rascunho dos modelos de raciocinio"),
    ("/restart",   "",                    "reinicia o hyde-ai"),
]


class Sidebar(Gtk.ApplicationWindow):
    """Right-edge, full-height, overlay layer-shell panel."""

    def __init__(self, app, config, registry, history, theme):
        super().__init__(application=app)
        self.config = config
        self.registry = registry
        self.history = history
        self.theme = theme

        self.set_decorated(False)
        self.set_title("hyde-ai")
        self.add_css_class("hyde-ai")

        self._shown = False
        self._anim_id = None
        self._anim_from = 0
        self._anim_to = 0
        self._anim_t0 = None
        self._width = self._compute_width()

        self._stream_seq = 0
        self._cancel = None
        self._paleta = None
        self._paleta_lista = None
        self._rescanning = False
        # Poll for newly-pulled Ollama models while the panel is open, the way
        # other Ollama frontends do, so `ollama pull` needs no restart here.
        self._poll_id = GLib.timeout_add_seconds(12, self._poll_providers)
        self._worker = None
        self._active_row = None
        self._pending_deltas = []
        self._done_reason = None
        self._pending_lock = threading.Lock()
        self._flush_id = 0
        self._follow = True
        self._banner_id = 0
        self._rows = []

        self._follow_stream = bool(self.config.get("ui.follow_stream", True))
        self._show_line_numbers = bool(self.config.get("ui.code_line_numbers", True))
        scheme_override = self.config.get("ui.code_style_scheme", "auto")
        if scheme_override and scheme_override != "auto":
            set_code_scheme_override(scheme_override)
        refresh_code_scheme()

        self._build_layer_shell()
        self._build_ui()
        self._install_controllers()

        if self.theme is not None:
            self.theme.connect_reload(self._on_theme_reload)

        self.present()
        if bool(self.config.get("ui.restore_last_session", True)):
            self._restore_history()
        self._sync_selectors()

    # ------------------------------------------------------------------
    # Layer shell
    # ------------------------------------------------------------------
    def _compute_width(self):
        try:
            fraction = float(self.config.get("sidebar.width_fraction", DEFAULT_WIDTH_FRACTION))
        except (TypeError, ValueError):
            fraction = DEFAULT_WIDTH_FRACTION
        try:
            wmin = int(self.config.get("sidebar.width_min", DEFAULT_WIDTH_MIN))
            wmax = int(self.config.get("sidebar.width_max", DEFAULT_WIDTH_MAX))
        except (TypeError, ValueError):
            wmin, wmax = DEFAULT_WIDTH_MIN, DEFAULT_WIDTH_MAX

        monitor_width = 1920
        try:
            surface = self.get_surface()
            display = self.get_display() or Gdk.Display.get_default()
            monitor = None
            if surface is not None and display is not None:
                monitor = display.get_monitor_at_surface(surface)
            if monitor is None and display is not None:
                monitors = display.get_monitors()
                if monitors is not None and monitors.get_n_items() > 0:
                    monitor = monitors.get_item(0)
            if monitor is not None:
                monitor_width = monitor.get_geometry().width
        except Exception:
            pass

        return max(wmin, min(wmax, int(monitor_width * fraction)))

    def _build_layer_shell(self):
        if LS is None or not LAYER_SHELL_OK:
            _warn("gtk4-layer-shell unavailable; falling back to a normal window")
            self.set_default_size(self._width, 900)
            return

        LS.init_for_window(self)
        LS.set_namespace(self, self.config.get("sidebar.namespace", "hyde-ai") or "hyde-ai")
        edge = self.config.get("sidebar.edge", "right")
        self._edge = LS.Edge.LEFT if str(edge).lower() == "left" else LS.Edge.RIGHT
        LS.set_anchor(self, self._edge, True)
        LS.set_anchor(self, LS.Edge.TOP, True)
        LS.set_anchor(self, LS.Edge.BOTTOM, True)
        LS.set_layer(self, LS.Layer.OVERLAY)
        # -1 == reserve nothing: the panel overlays, it never pushes tiled windows.
        LS.set_exclusive_zone(self, -1)
        LS.set_keyboard_mode(self, LS.KeyboardMode.NONE)
        LS.set_margin(self, self._edge, -self._width)
        self.set_default_size(self._width, -1)

    def _apply_width(self, width):
        if width == self._width:
            return
        self._width = width
        self.set_default_size(width, -1)
        if LS is not None and LAYER_SHELL_OK and not self._shown:
            LS.set_margin(self, self._edge, -width)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.add_css_class("panel")
        root.add_css_class("hyde-ai-root")
        self.set_child(root)

        root.append(self._build_header())

        # O banner flutua SOBRE a conversa. Como irmao numa caixa vertical ele
        # empurrava tudo para baixo ao aparecer e puxava de volta ao sumir --
        # a conversa dava um salto a cada aviso.
        corpo = Gtk.Overlay()
        corpo.set_vexpand(True)
        corpo.set_child(self._build_chat())
        banner = self._build_banner()
        banner.set_halign(Gtk.Align.FILL)
        banner.set_valign(Gtk.Align.START)
        corpo.add_overlay(banner)
        root.append(corpo)

        root.append(self._build_input())

    def _build_header(self):
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        header.add_css_class("header")

        title = Gtk.Label(label="hyde-ai", xalign=0.0)
        title.add_css_class("header-title")
        header.append(title)

        self._provider_sel = SelectorButton(self._on_provider_selected, "provider")
        self._provider_sel.set_tooltip_text("Provider")
        self._provider_sel.on_open = self.rescan_providers
        header.append(self._provider_sel)

        self._model_sel = SelectorButton(self._on_model_selected, "model")
        self._model_sel.set_tooltip_text("Model")
        self._model_sel.on_open = self.rescan_providers
        header.append(self._model_sel)

        header.append(_spacer())

        hist_btn = _icon_button("document-open-recent-symbolic",
                                "Conversas salvas (/historico)")
        hist_btn.connect("clicked", lambda _b: self._abrir_historico(hist_btn))
        header.append(hist_btn)

        rescan_btn = _icon_button("view-refresh-symbolic",
                                  "Rescan providers and models (/refresh)")
        rescan_btn.connect("clicked", lambda _b: self.rescan_providers(announce=True))
        header.append(rescan_btn)

        new_btn = _icon_button("document-new-symbolic", "New chat")
        new_btn.connect("clicked", lambda _b: self.new_conversation())
        header.append(new_btn)

        help_btn = _icon_button("help-about-symbolic", "Help (/help)")
        help_btn.connect("clicked", lambda _b: self._add_interface(HELP_TEXT))
        header.append(help_btn)

        close_btn = _icon_button("window-close-symbolic", "Close (Escape)")
        close_btn.connect("clicked", lambda _b: self.hide_panel())
        header.append(close_btn)
        return header

    def _build_banner(self):
        self._banner_revealer = Gtk.Revealer()
        self._banner_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._banner_revealer.set_transition_duration(160)
        self._banner_revealer.set_reveal_child(False)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.add_css_class("banner")
        icon = Gtk.Image.new_from_icon_name("dialog-information-symbolic")
        box.append(icon)
        self._banner_label = Gtk.Label(xalign=0.0)
        self._banner_label.add_css_class("banner-label")
        self._banner_label.set_wrap(True)
        self._banner_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._banner_label.set_hexpand(True)
        self._banner_label.set_selectable(True)
        box.append(self._banner_label)
        dismiss = _icon_button("window-close-symbolic", "Dismiss")
        dismiss.connect("clicked", lambda _b: self._banner_revealer.set_reveal_child(False))
        box.append(dismiss)
        self._banner_revealer.set_child(box)
        return self._banner_revealer

    def _build_chat(self):
        self._chat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._chat_box.add_css_class("chat")
        self._chat_box.set_valign(Gtk.Align.START)

        self._placeholder = self._build_placeholder()
        self._chat_box.append(self._placeholder)

        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_vexpand(True)
        self._scroller.set_child(self._chat_box)

        self._vadj = self._scroller.get_vadjustment()
        self._vadj.connect("changed", self._on_adj_changed)
        self._vadj.connect("value-changed", self._on_adj_value_changed)

        overlay = Gtk.Overlay()
        overlay.set_child(self._scroller)

        self._to_bottom = Gtk.Button()
        self._to_bottom.add_css_class("scroll-bottom")
        self._to_bottom.add_css_class("flat")
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        inner.append(Gtk.Image.new_from_icon_name("go-down-symbolic"))
        inner.append(Gtk.Label(label="Scroll to bottom"))
        self._to_bottom.set_child(inner)
        self._to_bottom.set_halign(Gtk.Align.CENTER)
        self._to_bottom.set_valign(Gtk.Align.END)
        self._to_bottom.set_visible(False)
        self._to_bottom.connect("clicked", lambda _b: self._scroll_to_bottom(force=True))
        overlay.add_overlay(self._to_bottom)
        return overlay

    def _build_placeholder(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("placeholder")
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)

        icon = Gtk.Image.new_from_icon_name("starred-symbolic")
        icon.set_pixel_size(48)
        icon.add_css_class("placeholder-icon")
        box.append(icon)

        title = Gtk.Label(label="Ask anything")
        title.add_css_class("placeholder-title")
        box.append(title)

        body = Gtk.Label(
            label="Enter sends · Shift+Enter for a newline\n"
                  "Type / for commands · Escape hides the panel"
        )
        body.set_justify(Gtk.Justification.CENTER)
        body.add_css_class("placeholder-body")
        body.add_css_class("dim")
        box.append(body)
        return box

    def _build_input(self):
        area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        area.add_css_class("input-area")

        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        frame.add_css_class("input-frame")
        self._input_frame = frame

        self._input_buffer = Gtk.TextBuffer()
        self._input_buffer.connect("changed", self._on_input_changed)
        self._input_buffer.connect("changed",
                                   lambda *_a: self._atualizar_paleta())
        self._input_view = Gtk.TextView(buffer=self._input_buffer)
        self._input_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._input_view.set_accepts_tab(False)
        self._input_view.set_left_margin(_INPUT_INSET)
        self._input_view.set_right_margin(_INPUT_INSET)
        self._input_view.set_top_margin(_INPUT_INSET)
        self._input_view.set_bottom_margin(_INPUT_INSET)
        self._input_view.add_css_class("input")

        input_scroll = Gtk.ScrolledWindow()
        input_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        input_scroll.set_propagate_natural_height(True)
        input_scroll.set_max_content_height(220)
        input_scroll.set_min_content_height(44)
        input_scroll.set_child(self._input_view)

        overlay = Gtk.Overlay()
        overlay.set_child(input_scroll)
        self._input_placeholder = Gtk.Label(
            label="Message the model…  “/” for commands", xalign=0.0
        )
        self._input_placeholder.add_css_class("input-placeholder")
        self._input_placeholder.set_halign(Gtk.Align.START)
        self._input_placeholder.set_valign(Gtk.Align.START)
        self._input_placeholder.set_can_target(False)
        # The placeholder is overlaid on the ScrolledWindow, so it starts at the
        # overlay's 0,0 -- but the TextView insets its text by its own margins.
        # Mirror them exactly or the hint sits above and left of the caret.
        self._input_placeholder.set_margin_start(_INPUT_INSET)
        self._input_placeholder.set_margin_top(_INPUT_INSET)
        self._input_placeholder.set_margin_end(_INPUT_INSET)
        overlay.add_overlay(self._input_placeholder)
        frame.append(overlay)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        controls.add_css_class("input-controls")

        # Ligar/desligar o rascunho e uma decisao por pergunta, nao de
        # configuracao: "quanto vale esperar" muda a cada uma. Por isso mora
        # aqui, ao lado do envio, e nao enterrado num arquivo.
        self._think_sync = False
        self._think_btn = Gtk.ToggleButton()
        self._think_btn.set_child(Gtk.Image.new_from_icon_name("weather-clear-symbolic"))
        self._think_btn.add_css_class("flat")
        self._think_btn.add_css_class("think-btn")
        self._think_btn.connect("toggled", self._on_think_toggled)
        controls.append(self._think_btn)
        self._sync_think_btn()

        controls.append(_spacer())

        self._status_label = Gtk.Label(xalign=1.0)
        self._status_label.add_css_class("dim")
        self._status_label.add_css_class("input-status")
        self._status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._status_label.set_max_width_chars(28)
        controls.append(self._status_label)

        self._send_btn = _icon_button("go-up-symbolic", "Send (Enter)", ("send-btn",))
        self._send_btn.connect("clicked", self._on_send_clicked)
        self._send_btn.set_sensitive(False)
        controls.append(self._send_btn)

        frame.append(controls)
        area.append(frame)
        return area

    def _install_controllers(self):
        win_keys = Gtk.EventControllerKey()
        win_keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        win_keys.connect("key-pressed", self._on_window_key)
        self.add_controller(win_keys)

        entry_keys = Gtk.EventControllerKey()
        entry_keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        entry_keys.connect("key-pressed", self._on_entry_key)
        self._input_view.add_controller(entry_keys)

        self.connect("map", self._on_mapped)
        self.connect("close-request", self._on_close_request)

    # ------------------------------------------------------------------
    # Visibility / animation
    # ------------------------------------------------------------------
    def _on_mapped(self, _widget):
        GLib.idle_add(self._post_map)
        return False

    def _post_map(self):
        self._apply_width(self._compute_width())
        return GLib.SOURCE_REMOVE

    def _on_close_request(self, _window):
        # Never destroy: the panel lives for the whole session and just slides away.
        self.hide_panel()
        return True

    def is_shown(self):
        return self._shown

    def toggle(self):
        if self._shown:
            self.hide_panel()
        else:
            self.show_panel()

    def show_panel(self):
        if LS is not None and LAYER_SHELL_OK:
            # Two-stage keyboard mode.
            #
            # ON_DEMAND alone never takes focus on map, so typing went to the
            # window underneath. EXCLUSIVE takes focus but grabs the keyboard
            # for as long as the panel is open, which locks every other window
            # out. Neither is what we want on its own.
            #
            # So: grab EXCLUSIVE just long enough for the compositor to hand
            # this surface keyboard focus, then immediately relax to ON_DEMAND.
            # Focus stays here (nothing else has asked for it), but the grab is
            # gone, so clicking another window works normally.
            LS.set_keyboard_mode(self, LS.KeyboardMode.EXCLUSIVE)
        self._shown = True
        self.set_visible(True)
        self._animate_to(0)
        GLib.timeout_add(60, self._focus_input)
        GLib.timeout_add(220, self._relax_keyboard_grab)

    def _relax_keyboard_grab(self):
        """Drop EXCLUSIVE -> ON_DEMAND once focus has landed here.

        Runs after show_panel()'s grab. If the panel was closed again in the
        meantime, leave the mode alone -- hide_panel() already set NONE.
        """
        if self._shown and LS is not None and LAYER_SHELL_OK:
            try:
                LS.set_keyboard_mode(self, LS.KeyboardMode.ON_DEMAND)
            except Exception as exc:
                _warn("could not relax keyboard grab: %r" % (exc,))
        return GLib.SOURCE_REMOVE

    def hide_panel(self):
        if not self._shown and self._anim_id is None:
            return
        self._shown = False
        self._animate_to(-self._width)
        if LS is not None and LAYER_SHELL_OK:
            LS.set_keyboard_mode(self, LS.KeyboardMode.NONE)
        try:
            self.history.save()
        except Exception as exc:
            _warn("history save failed: %r" % (exc,))

    def _focus_input(self):
        self._input_view.grab_focus()
        return GLib.SOURCE_REMOVE

    def _animate_to(self, target):
        if LS is None or not LAYER_SHELL_OK:
            self.set_visible(self._shown)
            return
        self._anim_from = LS.get_margin(self, self._edge)
        self._anim_to = target
        if self._anim_from == target:
            return
        self._anim_t0 = None
        if self._anim_id is not None:
            self.remove_tick_callback(self._anim_id)
        self._anim_id = self.add_tick_callback(self._tick)

    def _tick(self, _widget, clock):
        now = clock.get_frame_time()
        if self._anim_t0 is None:
            self._anim_t0 = now
        progress = min(1.0, (now - self._anim_t0) / float(ANIM_US))
        eased = 1.0 - pow(1.0 - progress, 3)
        value = int(self._anim_from + (self._anim_to - self._anim_from) * eased)
        LS.set_margin(self, self._edge, value)
        if progress >= 1.0:
            LS.set_margin(self, self._edge, self._anim_to)
            self._anim_id = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------
    def _popover_open(self):
        for sel in (self._provider_sel, self._model_sel):
            popover = sel.get_popover()
            if popover is not None and popover.get_visible():
                return True
        return False

    def _on_window_key(self, _ctrl, keyval, _keycode, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if keyval == Gdk.KEY_Escape:
            if self._popover_open():
                return False
            if self._cancel is not None and not cancel_is_set(self._cancel):
                self._stop_stream()
                return True
            self.hide_panel()
            return True
        if ctrl and not shift and keyval in (Gdk.KEY_l, Gdk.KEY_L):
            self.new_conversation()
            return True
        if ctrl and shift and keyval in (Gdk.KEY_c, Gdk.KEY_C):
            self._copy_last_reply()
            return True
        return False

    def _on_entry_key(self, _ctrl, keyval, _keycode, state):
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            if shift or ctrl:
                return False           # newline
            self._submit()
            return True
        return False

    # ------------------------------------------------------------------
    # Input helpers
    # ------------------------------------------------------------------
    def _input_text(self):
        start, end = self._input_buffer.get_bounds()
        return self._input_buffer.get_text(start, end, False)

    def _set_input_text(self, text):
        self._input_buffer.set_text(text, -1)
        end = self._input_buffer.get_end_iter()
        self._input_buffer.place_cursor(end)

    def _think_ligado(self):
        modo = str(self.config.get("ollama.think", "off") or "off").lower()
        return modo not in ("off", "false", "no", "0")

    def _sync_think_btn(self):
        """Reflete a config no botao sem disparar o handler de volta."""
        btn = getattr(self, "_think_btn", None)
        if btn is None:
            return
        ligado = self._think_ligado()
        # set_active() emite "toggled"; a trava evita que refletir a config
        # no botao acabe reescrevendo a propria config.
        self._think_sync = True
        try:
            btn.set_active(ligado)
        finally:
            self._think_sync = False
        modo = str(self.config.get("ollama.think", "off") or "off").lower()
        if ligado:
            btn.set_tooltip_text(
                "Raciocinio: %s - o modelo rascunha antes de responder "
                "(clique para desligar)" % modo)
            btn.remove_css_class("think-off")
        else:
            btn.set_tooltip_text(
                "Raciocinio desligado - respostas mais rapidas "
                "(clique para religar)")
            btn.add_css_class("think-off")
        # So o Ollama expoe esse controle no protocolo.
        btn.set_visible(self.history.provider == "ollama")

    def _on_think_toggled(self, btn):
        if getattr(self, "_think_sync", False):
            return
        self.config.set("ollama.think", "on" if btn.get_active() else "off")
        try:
            self.config.save()
        except Exception as exc:
            _warn("config.save failed: %r" % (exc,))
        self._sync_think_btn()
        self.show_banner(
            "Raciocinio ligado" if btn.get_active() else "Raciocinio desligado",
            2500)

    def _on_input_changed(self, _buffer):
        # May fire before the rest of the input area exists; stay defensive.
        placeholder = getattr(self, "_input_placeholder", None)
        send = getattr(self, "_send_btn", None)
        text = self._input_text()
        if placeholder is not None:
            placeholder.set_visible(not text)
        if send is not None and self._cancel is None:
            send.set_sensitive(bool(text.strip()))

    def _chip_slash(self, _button):
        text = self._input_text()
        if not text.startswith("/"):
            self._set_input_text("/" + text)
        self._input_view.grab_focus()

    def _chip_clear(self, _button):
        self.new_conversation()

    def _on_send_clicked(self, _button):
        if self._cancel is not None:
            self._stop_stream()
        else:
            self._submit()

    # ------------------------------------------------------------------
    # Banner
    # ------------------------------------------------------------------
    def show_banner(self, text, timeout_ms=7000):
        self._banner_label.set_text(text)
        self._banner_revealer.set_reveal_child(True)
        if self._banner_id:
            GLib.source_remove(self._banner_id)
            self._banner_id = 0
        if timeout_ms:
            self._banner_id = GLib.timeout_add(timeout_ms, self._hide_banner)

    def _hide_banner(self):
        self._banner_id = 0
        self._banner_revealer.set_reveal_child(False)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Provider / model plumbing
    # ------------------------------------------------------------------
    def _providers(self):
        try:
            return list(self.registry.list_providers())
        except Exception as exc:
            _warn("registry.list_providers failed: %r" % (exc,))
            return []

    def _provider_by_id(self, pid):
        for provider in self._providers():
            if provider.id == pid:
                return provider
        return None

    def _sync_selectors(self):
        providers = self._providers()
        items = []
        for provider in providers:
            items.append({
                "id": provider.id,
                "name": provider.name,
                "subtitle": None if provider.available else (provider.hint or "unavailable"),
                "sensitive": provider.available,
            })
        active_pid = self.history.provider
        if not items:
            self._provider_sel.set_items([], None)
            self._model_sel.set_items([], None)
            self._status_label.set_text("no providers")
            self.show_banner(
                "No providers are configured. Check ~/.config/hyde-ai/config.json.",
                timeout_ms=0,
            )
            self._update_send_state()
            return

        known = {p.id for p in providers}
        if active_pid not in known:
            available = [p for p in providers if p.available]
            active_pid = (available[0].id if available else providers[0].id)
            self.history.set_model(active_pid, None)
        self._provider_sel.set_items(items, active_pid)

        models = []
        try:
            models = list(self.registry.models(active_pid))
        except Exception as exc:
            _warn("registry.models failed: %r" % (exc,))

        def _com_media(m):
            """Anexa a media historica do modelo -- diferente da velocidade
            de uma resposta isolada, que aparece no cabecalho da mensagem."""
            sub = getattr(m, "description", "") or ""
            if _vel is not None:
                try:
                    md = _vel.media(m.id)
                except Exception:
                    md = None
                if md:
                    marca = _vel.formatar(md) + " media"
                    sub = (sub + "  ·  " + marca) if sub else marca
            return sub or None

        model_items = [{
            "id": m.id,
            "name": m.name,
            "subtitle": _com_media(m),
            "sensitive": True,
        } for m in models]

        active_model = self.history.model
        model_ids = {m.id for m in models}
        if active_model not in model_ids:
            active_model = None
            chooser = getattr(self.registry, "default_model", None)
            if callable(chooser):
                try:
                    candidate = chooser(active_pid)
                    if candidate in model_ids:
                        active_model = candidate
                except Exception:
                    active_model = None
            if active_model is None:
                active_model = models[0].id if models else None
            self.history.set_model(active_pid, active_model)
        self._model_sel.set_items(model_items, active_model)
        if _vel is not None and active_model:
            try:
                md = _vel.media(active_model)
            except Exception:
                md = None
            self._model_sel.set_sufixo(("  " + _vel.formatar(md)) if md else "")

        provider = self._provider_by_id(active_pid)
        if provider is not None and not provider.available:
            self._status_label.set_text("no API key")
            self.show_banner(
                "%s is unavailable: %s  —  set a key with:  /key %s <value>"
                % (provider.name, provider.hint or "no API key", provider.id),
                timeout_ms=0,
            )
        else:
            self._status_label.set_text(active_model or "")
        self._update_send_state()

        self._sync_think_btn()

    def set_edge(self, side):
        """Move the panel to the given screen edge and persist the choice."""
        side = "left" if str(side).lower() == "left" else "right"
        self.config.set("sidebar.edge", side)
        try:
            self.config.save()
        except Exception as exc:
            _warn("could not persist sidebar.edge: %r" % (exc,))
        self._add_interface(
            "Sidebar will open on the **%s**. Restarting to apply..." % side)
        GLib.timeout_add(400, self._do_restart)
        return True

    def set_width(self, spec):
        """Accept '35', '35%' or '700px'; clamp, persist, and apply live."""
        raw = str(spec).strip().lower().rstrip("%")
        try:
            if raw.endswith("px"):
                pixels = int(float(raw[:-2]))
                monitor_w = self._monitor_width()
                fraction = pixels / float(monitor_w or 1920)
            else:
                value = float(raw)
                # bare numbers >1 are percentages, <=1 are fractions
                fraction = value / 100.0 if value > 1 else value
        except (TypeError, ValueError):
            self._add_interface("Usage: `/width 35`, `/width 35%` or `/width 700px`")
            return True

        fraction = max(0.15, min(0.90, fraction))
        self.config.set("sidebar.width_fraction", round(fraction, 4))
        try:
            self.config.save()
        except Exception as exc:
            _warn("could not persist sidebar.width_fraction: %r" % (exc,))

        self._width = self._compute_width()
        if LS is not None and LAYER_SHELL_OK:
            try:
                self.set_size_request(self._width, -1)
                LS.set_margin(self, self._edge, 0 if self._shown else -self._width)
            except Exception as exc:
                _warn("could not resize live: %r" % (exc,))
        else:
            self.set_default_size(self._width, 900)
        self._add_interface("Sidebar width set to **%d%%** (%dpx). Saved."
                            % (round(fraction * 100), self._width))
        return True

    def _monitor_width(self):
        try:
            display = self.get_display() or Gdk.Display.get_default()
            surface = self.get_surface()
            monitor = None
            if surface is not None and display is not None:
                monitor = display.get_monitor_at_surface(surface)
            if monitor is None and display is not None:
                mons = display.get_monitors()
                if mons is not None and mons.get_n_items() > 0:
                    monitor = mons.get_item(0)
            if monitor is not None:
                return monitor.get_geometry().width
        except Exception:
            pass
        return 1920

    def _do_restart(self):
        """Re-exec the app, reopening the panel so the restart is seamless."""
        import os
        import shutil
        import subprocess
        try:
            self.history.save()
        except Exception:
            pass
        launcher = os.path.expanduser("~/.local/bin/hyde-ai")
        try:
            subprocess.Popen(
                ["setsid", launcher, "--show"] if shutil.which("setsid")
                else [launcher, "--show"],
                start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            _warn("restart failed: %r" % (exc,))
            return GLib.SOURCE_REMOVE
        app = self.get_application()
        if app is not None:
            GLib.timeout_add(150, lambda: (app.quit(), GLib.SOURCE_REMOVE)[1])
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Paleta de comandos
    # ------------------------------------------------------------------
    def _atualizar_paleta(self):
        """Abre/filtra a paleta conforme o que foi digitado apos a barra.

        So aparece quando a linha COMECA com "/" e ainda nao tem espaco --
        depois do primeiro argumento o usuario ja escolheu o comando.
        """
        texto = self._input_text()
        if not texto.startswith("/") or "\n" in texto:
            self._fechar_paleta()
            return
        termo = texto[1:]
        if " " in termo:
            self._fechar_paleta()
            return

        achados = [c for c in COMANDOS if c[0][1:].startswith(termo.lower())]
        if not achados:
            self._fechar_paleta()
            return

        if self._paleta is None:
            self._paleta = Gtk.Popover()
            self._paleta.add_css_class("selector-popover")
            self._paleta.set_position(Gtk.PositionType.TOP)
            self._paleta.set_autohide(False)
            self._paleta.set_has_arrow(False)
            self._paleta.set_halign(Gtk.Align.START)
            # Ancorado no proprio TextView: assim o retangulo de referencia
            # pode ser a posicao da barra, e nao o centro do campo.
            self._paleta.set_parent(self._input_view)
            self._paleta_lista = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                         spacing=1)
            rol = Gtk.ScrolledWindow()
            rol.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            rol.set_max_content_height(300)
            rol.set_propagate_natural_height(True)
            rol.set_min_content_width(340)
            rol.set_child(self._paleta_lista)
            self._paleta.set_child(rol)

        filho = self._paleta_lista.get_first_child()
        while filho is not None:
            prox = filho.get_next_sibling()
            self._paleta_lista.remove(filho)
            filho = prox

        for nome, args, desc in achados:
            botao = Gtk.Button()
            botao.add_css_class("selector-row")
            botao.set_has_frame(False)
            linha = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            linha.set_margin_top(4); linha.set_margin_bottom(4)
            linha.set_margin_start(10); linha.set_margin_end(10)

            lbl = Gtk.Label(xalign=0.0)
            lbl.set_markup("<b>%s</b>%s" % (
                html.escape(nome),
                ("  <span alpha='55%%'>%s</span>" % html.escape(args)) if args else ""))
            lbl.add_css_class("selector-row-title")
            linha.append(lbl)

            d = Gtk.Label(xalign=1.0, label=desc, hexpand=True)
            d.add_css_class("selector-row-sub")
            d.set_ellipsize(Pango.EllipsizeMode.END)
            linha.append(d)

            botao.set_child(linha)
            botao.connect("clicked", lambda _b, n=nome, a=args: self._usar_comando(n, a))
            self._paleta_lista.append(botao)

        self._apontar_paleta()
        if not self._paleta.get_visible():
            self._paleta.popup()

    def _apontar_paleta(self):
        """Alinha o popover com a barra digitada, nao com o centro do campo."""
        try:
            buf = self._input_buffer
            inicio = buf.get_start_iter()
            ret = self._input_view.get_iter_location(inicio)
            # coordenadas do buffer -> coordenadas da janela do widget
            x, y = self._input_view.buffer_to_window_coords(
                Gtk.TextWindowType.WIDGET, ret.x, ret.y)
            alvo = Gdk.Rectangle()
            alvo.x = max(0, x)
            alvo.y = max(0, y)
            alvo.width = 1
            alvo.height = max(1, ret.height)
            self._paleta.set_pointing_to(alvo)
        except Exception as exc:
            _warn("nao consegui alinhar a paleta: %r" % (exc,))

    def _usar_comando(self, nome, args):
        self._set_input_text(nome + (" " if args else ""))
        self._fechar_paleta()
        self._input_view.grab_focus()

    def _fechar_paleta(self):
        if self._paleta is not None and self._paleta.get_visible():
            self._paleta.popdown()

    def _abrir_historico(self, ancora=None):
        """Lista as conversas salvas, agrupadas por provedor.

        Sem ancora, reabre no botao usado da ultima vez -- e assim que a lista
        volta sozinha depois de excluir uma conversa.
        """
        ancora = ancora or getattr(self, "_hist_ancora", None)
        if ancora is None:
            return
        self._hist_ancora = ancora
        pop = Gtk.Popover()
        pop.add_css_class("selector-popover")
        pop.set_size_request(360, -1)
        pop.set_parent(ancora)

        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        rolagem = Gtk.ScrolledWindow()
        rolagem.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        rolagem.set_max_content_height(420)
        rolagem.set_propagate_natural_height(True)
        rolagem.set_propagate_natural_width(True)
        rolagem.set_min_content_width(360)
        rolagem.set_child(caixa)
        pop.set_child(rolagem)

        try:
            itens = self.history.conversas()
        except Exception as exc:
            _warn("nao consegui listar o historico: %r" % (exc,))
            itens = []

        if not itens:
            vazio = Gtk.Label(label="nenhuma conversa salva ainda")
            vazio.add_css_class("dim")
            vazio.set_margin_top(12)
            vazio.set_margin_bottom(12)
            caixa.append(vazio)

        atual_prov = None
        for it in itens:
            if it["provider"] != atual_prov:
                atual_prov = it["provider"]
                cab = Gtk.Label(xalign=0.0, label=atual_prov or "sem provedor")
                cab.add_css_class("selector-row-sub")
                cab.set_margin_start(10)
                cab.set_margin_top(8)
                caixa.append(cab)

            linha = Gtk.Button()
            linha.add_css_class("selector-row")
            linha.set_has_frame(False)
            corpo = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            corpo.set_margin_top(5)
            corpo.set_margin_bottom(5)
            corpo.set_margin_start(10)
            corpo.set_margin_end(10)

            titulo = Gtk.Label(xalign=0.0, label=it["titulo"])
            titulo.add_css_class("selector-row-title")
            titulo.set_ellipsize(Pango.EllipsizeMode.END)
            titulo.set_max_width_chars(38)
            corpo.append(titulo)

            import datetime
            quando = ""
            if it["quando"]:
                try:
                    quando = datetime.datetime.fromtimestamp(
                        it["quando"]).strftime("%d/%m %H:%M")
                except Exception:
                    quando = ""
            marca = "  ·  em uso" if it["atual"] else ""
            sub = Gtk.Label(
                xalign=0.0,
                label="%s  ·  %d mensagens%s" % (quando or it["model"], it["n"], marca))
            sub.add_css_class("selector-row-sub")
            corpo.append(sub)

            linha.set_child(corpo)
            linha.set_hexpand(True)
            cid = it["id"]
            linha.connect("clicked", lambda _b, i=cid, p=pop: self._restaurar(i, p))

            # Excluir fica na propria linha, nao num menu: e a acao que se
            # procura olhando para a conversa que se quer tirar dali.
            apagar = _icon_button("edit-delete-symbolic", "Excluir esta conversa")
            apagar.add_css_class("historico-apagar")
            apagar.set_valign(Gtk.Align.CENTER)
            apagar.connect("clicked",
                           lambda _b, i=cid, p=pop: self._apagar_conversa(i, p))

            par = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
            par.append(linha)
            if not it["atual"]:          # a conversa aberta nao se apaga
                par.append(apagar)
            caixa.append(par)

        pop.popup()

    def _apagar_conversa(self, conv_id, pop):
        try:
            apagou = self.history.apagar(conv_id)
        except Exception as exc:
            _warn("nao consegui apagar a conversa: %r" % (exc,))
            return
        if not apagou:
            return
        try:
            self.history.save()
        except Exception as exc:
            _warn("history.save falhou: %r" % (exc,))
        pop.popdown()
        self.show_banner("Conversa excluida.", timeout_ms=2500)
        # reabre a lista ja sem ela, para apagar varias de uma vez
        GLib.timeout_add(180, lambda: (self._abrir_historico(), GLib.SOURCE_REMOVE)[1])

    def _restaurar(self, conv_id, pop):
        try:
            if self.history.abrir(conv_id):
                self.history.save()
                self.reload_conversation()
                self.show_banner("Conversa restaurada.", timeout_ms=2500)
        except Exception as exc:
            _warn("nao consegui restaurar a conversa: %r" % (exc,))
        pop.popdown()

    def rescan_providers(self, announce=False):
        """Re-probe every provider off the main thread, then repaint.

        Called when a selector dropdown opens, by the header refresh button,
        by /refresh, and by a periodic poll while the panel is visible. This
        is what makes an `ollama pull` show up without restarting the app.
        """
        if getattr(self, "_rescanning", False):
            return
        self._rescanning = True

        def done():
            GLib.idle_add(self._rescan_finished, announce)

        try:
            runner = getattr(self.registry, "refresh_async", None)
            if callable(runner):
                runner(done)
            else:
                self._rescanning = False
        except Exception as exc:
            self._rescanning = False
            _warn("rescan failed: %r" % (exc,))

    def _rescan_finished(self, announce=False):
        self._rescanning = False
        before = tuple(self._known_model_ids())
        try:
            self._sync_selectors()
        except Exception as exc:
            _warn("sync after rescan failed: %r" % (exc,))
        after = tuple(self._known_model_ids())
        if announce:
            added = [m for m in after if m not in before]
            if added:
                self.show_banner("Found %d new model%s: %s"
                                 % (len(added), "" if len(added) == 1 else "s",
                                    ", ".join(added[:4])), timeout_ms=4000)
            else:
                self.show_banner("Providers rescanned - nothing new.",
                                 timeout_ms=2500)
        return GLib.SOURCE_REMOVE

    def _known_model_ids(self):
        """Flat list of every model id currently on offer, for change detection."""
        out = []
        try:
            for provider in self._providers():
                try:
                    for m in self.registry.models(provider.id):
                        out.append(getattr(m, "id", None) or getattr(m, "name", None) or str(m))
                except Exception:
                    continue
        except Exception:
            pass
        return out

    def _poll_providers(self):
        """Periodic background rescan while the panel is on screen.

        Cheap: Ollama's /api/tags is a local request, and the cloud providers
        only re-check whether a key is present. Skipped while hidden and while
        a generation is in flight so it never competes with a stream.
        """
        if self._shown and self._cancel is None:
            self.rescan_providers(announce=False)
        return GLib.SOURCE_CONTINUE

    def refresh_providers(self):
        """Re-read provider availability (called after an async probe)."""
        self._sync_selectors()

    def _on_provider_selected(self, pid):
        self.history.set_model(pid, None)
        self.history.save()
        self._sync_selectors()

    def _on_model_selected(self, model_id):
        self.history.set_model(self.history.provider, model_id)
        self.history.save()
        self._sync_selectors()

    def _update_send_state(self):
        if self._cancel is not None:
            self._send_btn.set_sensitive(True)
            return
        self._send_btn.set_sensitive(bool(self._input_text().strip()))

    # ------------------------------------------------------------------
    # Conversation rendering
    # ------------------------------------------------------------------
    def _restore_history(self):
        for message in list(self.history.messages):
            self._append_row(
                message.get("role", ROLE_INTERFACE),
                message.get("content", ""),
                msg_id=message.get("id"),
                name=message.get("name") or message.get("model"),
                persist=False,
                metricas=message.get("metricas"),
            )
        self._scroll_to_bottom(force=True)

    def _update_placeholder(self):
        self._placeholder.set_visible(not self._rows)

    def _append_row(self, role, content, msg_id=None, name=None, persist=True,
                    metricas=None):
        if persist:
            record = self.history.add(role, content, name=name)
            msg_id = record.get("id")

        display_name = name
        if display_name is None and role == ROLE_ASSISTANT:
            display_name = self.history.model or "Assistant"

        row = MessageRow(
            role, content,
            name=display_name,
            msg_id=msg_id,
            on_delete=self._on_delete_row,
            on_regenerate=self._on_regenerate_row if role == ROLE_ASSISTANT else None,
            show_line_numbers=self._show_line_numbers,
        )
        if metricas:
            try:
                row.set_metricas(metricas.get("espera", 0.0),
                                 metricas.get("geracao", 0.0),
                                 metricas.get("tps"))
            except Exception:
                pass
        self._chat_box.append(row)
        self._rows.append(row)
        self._update_placeholder()
        self._scroll_to_bottom()
        return row

    def _on_delete_row(self, row):
        if self._cancel is not None and row is self._active_row:
            self._stop_stream()
        try:
            self.history.delete(row.msg_id)
        except Exception as exc:
            _warn("history.delete failed: %r" % (exc,))
        if row in self._rows:
            self._rows.remove(row)
        self._chat_box.remove(row)
        self.history.save()
        self._update_placeholder()

    def _on_regenerate_row(self, row):
        if self._cancel is not None:
            self.show_banner("Still streaming — stop it first.")
            return
        index = self._rows.index(row) if row in self._rows else -1
        if index < 0:
            return
        try:
            self.history.truncate_from(row.msg_id)
        except Exception as exc:
            _warn("history.truncate_from failed: %r" % (exc,))
            return
        for stale in self._rows[index:]:
            self._chat_box.remove(stale)
        del self._rows[index:]
        self._update_placeholder()
        self.history.save()
        self._start_stream()

    def _add_interface(self, text):
        self._append_row(ROLE_INTERFACE, text)
        self.history.save()

    def _copy_last_reply(self):
        for row in reversed(self._rows):
            if row.role == ROLE_ASSISTANT:
                if copy_to_clipboard(self, row.content):
                    self.show_banner("Copied the last reply.", 2500)
                return
        self.show_banner("Nothing to copy yet.", 2500)

    def reload_conversation(self):
        """Redesenha a conversa atual do historico na tela.

        Usado ao restaurar uma conversa salva: limpa as linhas e reconstroi a
        partir do que esta gravado, sem regravar nada (persist=False).
        """
        if self._cancel is not None:
            self._stop_stream()
        for row in self._rows:
            self._chat_box.remove(row)
        self._rows = []
        try:
            mensagens = list(self.history.messages)
        except Exception as exc:
            _warn("nao consegui ler as mensagens: %r" % (exc,))
            mensagens = []
        for m in mensagens:
            self._append_row(
                m.get("role", ROLE_ASSISTANT),
                m.get("content", ""),
                msg_id=m.get("id"),
                name=m.get("model") or None,
                persist=False,
                metricas=m.get("metricas"),
            )
        self._sync_selectors()
        self._update_placeholder()
        self._follow = True
        self._scroll_to_bottom()

    def new_conversation(self):
        if self._cancel is not None:
            self._stop_stream()
        for row in self._rows:
            self._chat_box.remove(row)
        self._rows = []
        try:
            self.history.new_conversation(self.history.provider, self.history.model)
            self.history.save()
        except Exception as exc:
            _warn("history.new_conversation failed: %r" % (exc,))
        self._update_placeholder()
        self._follow = True
        self._to_bottom.set_visible(False)

    # ------------------------------------------------------------------
    # Scrolling
    # ------------------------------------------------------------------
    def _on_adj_changed(self, adj):
        if self._follow and self._follow_stream:
            adj.set_value(max(0.0, adj.get_upper() - adj.get_page_size()))
        self._update_to_bottom(adj)

    def _on_adj_value_changed(self, adj):
        at_bottom = adj.get_value() >= adj.get_upper() - adj.get_page_size() - 4.0
        self._follow = at_bottom
        self._update_to_bottom(adj)

    def _update_to_bottom(self, adj):
        overflow = adj.get_upper() - adj.get_page_size()
        visible = overflow > 8.0 and adj.get_value() < overflow - 8.0
        self._to_bottom.set_visible(visible)

    def _scroll_to_bottom(self, force=False):
        """Fixa no fim ate a altura estabilizar.

        Rolar uma vez so nao basta: labels com quebra de linha e as figuras de
        formula so tem altura final depois de um ou dois ciclos de layout, e a
        rolagem antecipada parava no meio da ultima mensagem.
        """
        if force:
            self._follow = True

        estado = {"ultimo": -1.0, "tentativas": 0}

        def fixar():
            adj = self._vadj
            topo = adj.get_upper()
            adj.set_value(max(0.0, topo - adj.get_page_size()))
            estado["tentativas"] += 1
            if abs(topo - estado["ultimo"]) < 0.5 or estado["tentativas"] > 24:
                return GLib.SOURCE_REMOVE
            estado["ultimo"] = topo
            return GLib.SOURCE_CONTINUE

        GLib.idle_add(fixar, priority=GLib.PRIORITY_LOW)
        GLib.timeout_add(30, fixar)

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------
    def _handle_command(self, text):
        parts = text[1:].split()
        if not parts:
            return True
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("help", "h", "?"):
            self._add_interface(HELP_TEXT)
            return True

        if cmd in ("clear", "new", "reset"):
            self.new_conversation()
            return True

        if cmd in ("historico", "history", "conversas"):
            itens = self.history.conversas()
            if not itens:
                self._add_interface("Nenhuma conversa salva ainda.")
                return True
            import datetime
            linhas = ["## Conversas salvas", ""]
            for it in itens:
                q = ""
                if it["quando"]:
                    try:
                        q = datetime.datetime.fromtimestamp(
                            it["quando"]).strftime("%d/%m %H:%M")
                    except Exception:
                        q = ""
                linhas.append("- **%s** — %s · %s · %d msgs%s"
                              % (it["titulo"], it["provider"] or "?", q,
                                 it["n"], "  (em uso)" if it["atual"] else ""))
            linhas += ["", "Use o botao de conversas no cabecalho para abrir uma."]
            self._add_interface("\n".join(linhas))
            return True

        if cmd in ("velocidade", "speed", "tps"):
            if _vel is None:
                self._add_interface("Modulo de velocidade indisponivel.")
                return True
            linhas = ["## Velocidade por modelo", ""]
            for nome, prov, m, n in _vel.resumo():
                linhas.append("- **%s** — %s · %s · %d execucoes"
                              % (nome, prov or "?", _vel.formatar(m), n))
            if len(linhas) == 2:
                linhas.append("_ainda sem medicoes_")
            self._add_interface("\n".join(linhas))
            return True

        if cmd in ("refresh", "rescan", "reload"):
            self.rescan_providers(announce=True)
            return True

        if cmd in ("side", "edge"):
            if not args:
                current = self.config.get("sidebar.edge", "right")
                self._add_interface(
                    "Sidebar is on the **%s**. Use `/side left` or `/side right`."
                    % current)
                return True
            want = args[0].lower()
            if want not in ("left", "right"):
                self._add_interface("Usage: `/side left` or `/side right`")
                return True
            self.set_edge(want)
            return True

        if cmd in ("width", "size"):
            if not args:
                frac = self.config.get("sidebar.width_fraction",
                                       DEFAULT_WIDTH_FRACTION)
                self._add_interface(
                    "Sidebar width is **%d%%** of the screen (%dpx).\n\n"
                    "Set it with `/width 35` (percent) or `/width 700px`."
                    % (round(float(frac) * 100), self._width))
                return True
            self.set_width(args[0])
            return True

        if cmd == "restart":
            # Full process restart. Auto-discovery covers new Ollama models,
            # so this is only for changes it cannot pick up: a rewritten
            # config.json, an upgraded ollama daemon, or edited app code.
            self._add_interface("Restarting hyde-ai...")
            GLib.timeout_add(300, self._do_restart)
            return True

        if cmd == "provider":
            providers = self._providers()
            if not args:
                lines = ["## Providers", ""]
                for provider in providers:
                    mark = "✓" if provider.available else "✗"
                    note = "" if provider.available else "  — %s" % (provider.hint or "unavailable")
                    lines.append("- %s `%s` — %s%s" % (mark, provider.id, provider.name, note))
                lines.append("")
                lines.append("Switch with `/provider <id>`.")
                self._add_interface("\n".join(lines))
                return True
            pid = args[0]
            provider = self._provider_by_id(pid)
            if provider is None:
                self._add_interface("Unknown provider: `%s`. Try `/provider` for the list." % pid)
                return True
            self.history.set_model(pid, None)
            self.history.save()
            self._sync_selectors()
            self._add_interface("Provider set to **%s**." % provider.name)
            return True

        if cmd == "think":
            validos = ("auto", "on", "off", "low", "medium", "high")
            atual = str(self.config.get("ollama.think", "off") or "off").lower()
            if not args:
                self._add_interface(
                    "## Raciocinio\n\n"
                    "Agora: `%s`\n\n"
                    "- `auto` - o modelo decide\n"
                    "- `off` - sem rascunho, respostas bem mais rapidas\n"
                    "- `on` - forca o rascunho\n"
                    "- `low` / `medium` / `high` - esforco, nos modelos que aceitam\n\n"
                    "Troque com `/think off`. Vale para o Ollama; "
                    "Claude e Gemini tem os proprios ajustes." % atual)
                return True
            escolha = args[0].lower()
            if escolha not in validos:
                self._add_interface("`%s` nao serve. Use: %s."
                                    % (escolha, ", ".join("`%s`" % v for v in validos)))
                return True
            self.config.set("ollama.think", escolha)
            try:
                self.config.save()
            except Exception as exc:
                _warn("config.save failed: %r" % (exc,))
            self._sync_think_btn()
            self.show_banner("Raciocinio: %s" % escolha, 3000)
            return True

        if cmd == "model":
            pid = self.history.provider
            try:
                models = list(self.registry.models(pid))
            except Exception as exc:
                self._add_interface("Could not list models: `%s`" % exc)
                return True
            if not args:
                lines = ["## Models — %s" % (pid or "?"), ""]
                for model in models:
                    desc = getattr(model, "description", "") or ""
                    lines.append("- `%s` — %s%s" % (model.id, model.name, ("  — " + desc) if desc else ""))
                lines.append("")
                lines.append("Switch with `/model <id>`.")
                self._add_interface("\n".join(lines))
                return True
            wanted = args[0].lower()
            match = None
            for model in models:
                if model.id.lower() == wanted:
                    match = model
                    break
            if match is None:
                for model in models:
                    if wanted in model.id.lower():
                        match = model
                        break
            if match is None:
                self._add_interface("Unknown model: `%s`. Try `/model` for the list." % args[0])
                return True
            self.history.set_model(pid, match.id)
            self.history.save()
            self._sync_selectors()
            self._add_interface("Model set to **%s**." % match.name)
            return True

        if cmd == "key":
            if len(args) < 2:
                self._add_interface("Usage: `/key <provider> <value>`\n\nRun `/provider` to see the ids.")
                return True
            pid, value = args[0], " ".join(args[1:]).strip()
            if self._provider_by_id(pid) is None:
                self._add_interface("Unknown provider: `%s`." % pid)
                return True
            try:
                self.registry.set_api_key(pid, value)
            except Exception as exc:
                self._add_interface("Could not store the key: `%s`" % exc)
                return True
            self._sync_selectors()
            self._add_interface("API key stored for **%s**." % pid)
            return True

        if cmd == "keys":
            lines = ["## API keys", ""]
            for provider in self._providers():
                state = "set" if provider.available else (provider.hint or "missing")
                lines.append("- `%s` — %s" % (provider.id, state))
            self._add_interface("\n".join(lines))
            return True

        self._add_interface("Unknown command: `/%s`. Try `/help`." % cmd)
        return True

    # ------------------------------------------------------------------
    # Sending / streaming
    # ------------------------------------------------------------------
    def ask(self, text):
        """Fill the input with `text` and send it, as if the user had typed it.

        Used by `hyde-ai --ask`, so a keybind or script can hand the panel a
        question directly.
        """
        text = (text or "").strip()
        if not text:
            return
        self._set_input_text(text)
        GLib.idle_add(self._submit)

    def _submit(self):
        if self._cancel is not None:
            return
        text = self._input_text().strip()
        if not text:
            return

        if text.startswith("/"):
            self._set_input_text("")
            self._handle_command(text)
            return

        # A mensagem do usuario aparece primeiro, sempre. Qualquer checagem
        # antes disso atrasa o retorno visual do Enter.
        self._set_input_text("")
        self._append_row(ROLE_USER, text)
        self.history.save()

        provider = self._provider_by_id(self.history.provider)
        if provider is None:
            self.show_banner("No provider selected.", 0)
            return
        if not provider.available:
            self.show_banner(
                "%s is unavailable: %s  —  store a key with  /key %s <value>"
                % (provider.name, provider.hint or "no API key", provider.id),
                timeout_ms=0,
            )
            return
        if not self.history.model:
            self.show_banner("No model selected — use /model to pick one.", 0)
            return

        # o stream comeca no proximo ciclo do loop, para o GTK desenhar a
        # mensagem e o indicador antes de qualquer trabalho de rede
        GLib.idle_add(self._start_stream)

    def _system_prompt(self):
        resolver = getattr(self.config, "system_prompt", None)
        if callable(resolver):
            try:
                value = resolver()
                if value:
                    return value
            except Exception as exc:
                _warn("config.system_prompt() failed: %r" % (exc,))
        return self.config.get("chat.system_prompt", "") or ""

    def _build_request_messages(self):
        try:
            limit = int(self.config.get("chat.max_history_messages", 40))
        except (TypeError, ValueError):
            limit = 40
        messages = [
            {"role": m["role"], "content": m.get("content", "")}
            for m in self.history.messages
            if m.get("role") in (ROLE_USER, ROLE_ASSISTANT) and (m.get("content") or "").strip()
        ]
        if limit > 0:
            messages = messages[-limit:]
        return messages

    def _make_cancel(self):
        """Prefer the provider layer's own token so the socket is torn down."""
        factory = getattr(self.registry, "new_cancel", None)
        if callable(factory):
            try:
                return factory()
            except Exception as exc:
                _warn("registry.new_cancel failed: %r" % (exc,))
        return threading.Event()

    def _start_stream(self):
        messages = self._build_request_messages()
        if not messages:
            self.show_banner("Nothing to send.", 3000)
            return

        provider_id = self.history.provider
        model_id = self.history.model
        system = self._system_prompt()

        row = self._append_row(ROLE_ASSISTANT, "", name=model_id or "Assistant")
        row.set_streaming(True)
        self._active_row = row
        # medicao desta resposta: primeiro token marca o fim do "pensando"
        self._t_envio = time.monotonic()
        self._t_primeiro = None
        self._n_tokens = 0

        self._stream_seq += 1
        seq = self._stream_seq
        cancel = self._make_cancel()
        self._cancel = cancel

        self._send_btn.set_icon_name("media-playback-stop-symbolic")
        self._send_btn.set_tooltip_text("Stop generating (Escape)")
        self._send_btn.add_css_class("stop-btn")
        self._send_btn.set_sensitive(True)
        self._status_label.set_text("streaming…")

        self._worker = threading.Thread(
            target=self._stream_worker,
            args=(seq, provider_id, model_id, messages, system, cancel),
            name="hyde-ai-stream",
            daemon=True,
        )
        self._worker.start()

    def _stream_worker(self, seq, provider_id, model_id, messages, system, cancel):
        """Runs OFF the main loop.  Never touches a widget directly."""
        error = None
        self._done_reason = None
        try:
            # Nem todo registry expoe stream_events; cair para o stream de
            # texto puro e melhor do que quebrar o envio.
            eventos_fn = getattr(self.registry, "stream_events", None)
            if callable(eventos_fn):
                eventos = eventos_fn(
                    provider_id, model_id, messages, system, cancel)
            else:
                eventos = _somente_texto(self.registry.stream(
                    provider_id, model_id, messages, system, cancel))
            for ev in eventos:
                if cancel_is_set(cancel):
                    break
                kind = getattr(ev, "kind", "text")
                if kind == "usage":
                    self._done_reason = (ev.data or {}).get("done_reason")
                    continue
                if kind not in ("text", "thinking"):
                    continue
                delta = ev.text
                if not delta:
                    continue
                # Tokens de raciocinio sao gerados como qualquer outro, entao
                # contam para a velocidade; o que muda e onde eles aparecem.
                if self._t_primeiro is None:
                    self._t_primeiro = time.monotonic()
                self._n_tokens += 1
                with self._pending_lock:
                    self._pending_deltas.append((kind, delta))
                    schedule = self._flush_id == 0
                    if schedule:
                        self._flush_id = -1
                if schedule:
                    GLib.timeout_add(DELTA_FLUSH_MS, self._flush_deltas, seq)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            error = str(exc) or exc.__class__.__name__
        GLib.idle_add(self._on_stream_done, seq, error, cancel_is_set(cancel))

    def _drenar_pendentes(self):
        """Junta os deltas acumulados, preservando a ordem entre os tipos."""
        with self._pending_lock:
            itens = self._pending_deltas
            self._pending_deltas = []
            self._flush_id = 0
        blocos = []
        for kind, texto in itens:
            if blocos and blocos[-1][0] == kind:
                blocos[-1][1].append(texto)
            else:
                blocos.append((kind, [texto]))
        return [(k, "".join(partes)) for k, partes in blocos]

    def _aplicar_blocos(self, row, blocos):
        for kind, texto in blocos:
            if kind == "thinking":
                row.append_thinking(texto)
            else:
                row.append_content(texto)

    def _flush_deltas(self, seq):
        blocos = self._drenar_pendentes()
        if not blocos:
            return GLib.SOURCE_REMOVE
        if seq != self._stream_seq or self._active_row is None:
            return GLib.SOURCE_REMOVE
        self._aplicar_blocos(self._active_row, blocos)
        try:
            self.history.replace_last_content(self._active_row.content)
        except Exception as exc:
            _warn("history.replace_last_content failed: %r" % (exc,))
        return GLib.SOURCE_REMOVE

    def _on_stream_done(self, seq, error, cancelled):
        if seq != self._stream_seq:
            return GLib.SOURCE_REMOVE

        # Drain anything still buffered.
        blocos = self._drenar_pendentes()
        row = self._active_row
        if row is not None and blocos:
            self._aplicar_blocos(row, blocos)

        if row is not None:
            row.finish_thinking()

        # Um modelo de raciocinio que gasta todo o orcamento pensando termina
        # sem resposta nenhuma. Sem este aviso a bolha fica vazia e sem
        # explicacao.
        if (row is not None and not error and not cancelled
                and not row.content.strip() and getattr(row, "thinking", "")):
            if self._done_reason == "length":
                self.show_banner(
                    "The model used the whole token budget on reasoning and never "
                    "reached an answer — raise max_tokens in the config.",
                    timeout_ms=0,
                )
            else:
                self.show_banner("The model returned only reasoning, no answer.", 6000)

        # metricas desta resposta: tempo ate o 1o token (o "pensando") e a
        # velocidade de geracao dela propria
        if row is not None and not error and self._n_tokens:
            fim = time.monotonic()
            espera = ((self._t_primeiro or fim) - self._t_envio)
            geracao = max(fim - (self._t_primeiro or fim), 1e-6)
            vel = None
            if _vel is not None:
                try:
                    vel = _vel.registrar(self.history.model, self._n_tokens,
                                         geracao, self.history.provider)
                except Exception as exc:
                    _warn("nao consegui registrar a velocidade: %r" % (exc,))
            row.set_metricas(espera, geracao, vel)
            try:
                self.history.set_metricas(row.msg_id, espera, geracao, vel)
                self.history.save()
            except Exception as exc:
                _warn("nao consegui gravar as metricas: %r" % (exc,))
            self._sync_selectors()

        if row is not None:
            if cancelled and not error:
                row.append_content("\n\n*[stopped]*")
            if error:
                row.set_error(True)
                if row.content.strip():
                    row.append_content("\n\n**Error:** %s" % error)
                else:
                    row.set_content("**Error:** %s" % error)
                self.show_banner("Request failed: %s" % error, 0)
            row.set_streaming(False)
            try:
                self.history.replace_last_content(row.content)
            except Exception:
                pass

        self._cancel = None
        self._worker = None
        self._active_row = None
        self._send_btn.set_icon_name("go-up-symbolic")
        self._send_btn.set_tooltip_text("Send (Enter)")
        self._send_btn.remove_css_class("stop-btn")
        self._status_label.set_text(self.history.model or "")
        self._update_send_state()
        try:
            self.history.save()
        except Exception as exc:
            _warn("history save failed: %r" % (exc,))
        return GLib.SOURCE_REMOVE

    def _stop_stream(self):
        if self._cancel is not None:
            cancel_request(self._cancel)
            self._status_label.set_text("stopping…")

    # ------------------------------------------------------------------
    # Theme reload hook
    # ------------------------------------------------------------------
    def _on_theme_reload(self):
        refresh_code_scheme()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def shutdown(self):
        if self._cancel is not None:
            cancel_request(self._cancel)
        try:
            self.history.save()
        except Exception:
            pass


if __name__ == "__main__":  # pragma: no cover
    print("hyde-ai: run `hyde-ai` (main.py), not sidebar.py directly.", file=sys.stderr)
    sys.exit(2)
