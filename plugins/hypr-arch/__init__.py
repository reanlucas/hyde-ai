"""Plugin hypr-arch — ferramentas Hyprland + Arch Linux para o hypr-ia.

Instalado pelo install.sh do hyde-ai em ``~/.hypr-ia/plugins/hypr-arch`` e
ativado em ``plugins.enabled`` no config.yaml. Registra dois toolsets:

``hyprland``
    ``hypr_info`` (consultas -j do hyprctl) e ``hypr_ctl`` (dispatch,
    keyword, reload e notificacao de desktop).

``archlinux``
    ``pacman_info`` (consultas read-only ao pacman) e ``arch_system``
    (visao geral, units com falha, erros do boot).

Tudo roda por subprocess com timeout; nada aqui instala, remove ou
atualiza pacotes — mudancas de sistema continuam passando pelo toolset
``terminal`` do proprio agente, com aprovacao inline.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

from tools.registry import tool_error, tool_result

_TIMEOUT = 15


def _run(cmd, timeout=_TIMEOUT):
    """Roda um comando e devolve (rc, stdout, stderr) sem nunca levantar."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", "%s nao encontrado" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", "%s excedeu %ss" % (cmd[0], timeout)
    except Exception as exc:  # noqa: BLE001 - fronteira de tool
        return 1, "", str(exc)


def _check_hyprland() -> bool:
    return bool(shutil.which("hyprctl")
                and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))


def _check_arch() -> bool:
    return bool(shutil.which("pacman"))


# ── hyprland ─────────────────────────────────────────────────────────────

_HYPR_INFO_QUERIES = (
    "workspaces", "clients", "monitors", "activewindow", "activeworkspace",
    "devices", "binds", "layers", "version",
)

HYPR_INFO_SCHEMA = {
    "name": "hypr_info",
    "description": (
        "Read-only Hyprland (window manager) state via hyprctl: open "
        "windows (clients), workspaces, monitors, the focused window, "
        "keybinds, input devices, layer-shell surfaces, version, or a "
        "single config option."),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "enum": list(_HYPR_INFO_QUERIES) + ["getoption"],
                "description": "What to read from the compositor.",
            },
            "option": {
                "type": "string",
                "description": ("Config option for query=getoption, e.g. "
                                "'general:gaps_in'."),
            },
        },
        "required": ["query"],
    },
}


def _handle_hypr_info(args: dict, **_kw) -> str:
    query = str(args.get("query") or "").strip().lower()
    if query == "getoption":
        option = str(args.get("option") or "").strip()
        if not option:
            return tool_error("query=getoption precisa de 'option'")
        cmd = ["hyprctl", "-j", "getoption", option]
    elif query in _HYPR_INFO_QUERIES:
        cmd = ["hyprctl", "-j", query]
    else:
        return tool_error("query desconhecida: %r" % query)
    rc, out, err = _run(cmd)
    if rc != 0:
        return tool_error(err.strip() or out.strip() or "hyprctl falhou")
    try:
        return tool_result(json.loads(out))
    except ValueError:
        return tool_result({"raw": out.strip()})


HYPR_CTL_SCHEMA = {
    "name": "hypr_ctl",
    "description": (
        "Act on Hyprland: 'dispatch' runs a dispatcher (workspace 3, "
        "togglefloating, fullscreen, movetoworkspace 2, focuswindow "
        "class:foo, exec <app>...), 'keyword' sets a config option at "
        "runtime (general:gaps_in 8), 'reload' re-reads the Hyprland "
        "config, 'notify' shows a desktop notification to the user."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["dispatch", "keyword", "reload", "notify"],
            },
            "command": {
                "type": "string",
                "description": ("For dispatch: '<dispatcher> [args]'. For "
                                "keyword: '<option> <value>'. For notify: "
                                "the message text."),
            },
        },
        "required": ["action"],
    },
}


def _handle_hypr_ctl(args: dict, **_kw) -> str:
    action = str(args.get("action") or "").strip().lower()
    command = str(args.get("command") or "").strip()
    if action == "reload":
        cmd = ["hyprctl", "reload"]
    elif action == "notify":
        if not command:
            return tool_error("notify precisa de 'command' com o texto")
        exe = shutil.which("notify-send")
        cmd = ([exe, "hypr-ia", command] if exe
               else ["hyprctl", "notify", "-1", "6000", "0", command])
    elif action in ("dispatch", "keyword"):
        if not command:
            return tool_error("%s precisa de 'command'" % action)
        cmd = ["hyprctl", action] + command.split()
    else:
        return tool_error("action desconhecida: %r" % action)
    rc, out, err = _run(cmd)
    texto = (out or err).strip()
    if rc != 0:
        return tool_error(texto or "hyprctl falhou")
    return tool_result({"ok": True, "output": texto})


# ── archlinux ────────────────────────────────────────────────────────────

PACMAN_INFO_SCHEMA = {
    "name": "pacman_info",
    "description": (
        "Read-only Arch Linux package queries via pacman: search the "
        "repos, show a package's details or file list, find which "
        "package owns a file, list explicitly installed packages, list "
        "orphans, or list pending updates. Never installs or removes "
        "anything — use the terminal for that (it asks for approval)."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "info", "files", "owns",
                         "list_explicit", "orphans", "updates"],
            },
            "term": {
                "type": "string",
                "description": ("Search term, package name, or file path "
                                "(for owns)."),
            },
        },
        "required": ["action"],
    },
}


def _handle_pacman_info(args: dict, **_kw) -> str:
    action = str(args.get("action") or "").strip().lower()
    term = str(args.get("term") or "").strip()
    precisa_termo = {"search", "info", "files", "owns"}
    if action in precisa_termo and not term:
        return tool_error("action=%s precisa de 'term'" % action)
    if action == "search":
        cmd = ["pacman", "-Ss", term]
    elif action == "info":
        # -Qi para instalado; cai para -Si (repo) se nao estiver
        rc, out, err = _run(["pacman", "-Qi", term])
        if rc != 0:
            rc, out, err = _run(["pacman", "-Si", term])
        if rc != 0:
            return tool_error(err.strip() or "pacote nao encontrado: %s" % term)
        return tool_result({"info": out.strip()})
    elif action == "files":
        cmd = ["pacman", "-Ql", term]
    elif action == "owns":
        cmd = ["pacman", "-Qo", term]
    elif action == "list_explicit":
        cmd = ["pacman", "-Qe"]
    elif action == "orphans":
        cmd = ["pacman", "-Qdtq"]
    elif action == "updates":
        exe = shutil.which("checkupdates")
        cmd = [exe] if exe else ["pacman", "-Qu"]
    else:
        return tool_error("action desconhecida: %r" % action)
    rc, out, err = _run(cmd, timeout=30)
    # pacman -Qdtq e checkupdates devolvem rc!=0 quando a lista e vazia
    if rc != 0 and not out.strip():
        if action in ("orphans", "updates"):
            return tool_result({"lines": [], "note": "nada encontrado"})
        return tool_error(err.strip() or "pacman falhou (rc=%s)" % rc)
    linhas = [l for l in out.strip().splitlines() if l]
    if len(linhas) > 400:
        linhas = linhas[:400] + ["... (%d linhas no total)" % len(linhas)]
    return tool_result({"lines": linhas})


ARCH_SYSTEM_SCHEMA = {
    "name": "arch_system",
    "description": (
        "Arch Linux system health, read-only: 'overview' (kernel, "
        "uptime, memory, disk), 'failed_units' (systemd units in a "
        "failed state), 'boot_errors' (error-level journal entries from "
        "the current boot)."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["overview", "failed_units", "boot_errors"],
            },
        },
        "required": ["action"],
    },
}


def _handle_arch_system(args: dict, **_kw) -> str:
    action = str(args.get("action") or "").strip().lower()
    if action == "overview":
        dados = {}
        for chave, cmd in (
            ("kernel", ["uname", "-sr"]),
            ("uptime", ["uptime", "-p"]),
            ("memoria", ["free", "-h"]),
            ("disco", ["df", "-h", "/", os.path.expanduser("~")]),
        ):
            rc, out, err = _run(cmd)
            dados[chave] = out.strip() if rc == 0 else (err.strip() or "?")
        return tool_result(dados)
    if action == "failed_units":
        rc, out, err = _run(["systemctl", "--failed", "--no-pager",
                             "--no-legend"])
        linhas = [l for l in out.strip().splitlines() if l]
        return tool_result({"failed": linhas or [],
                            "note": "" if linhas else "nenhuma unit falhou"})
    if action == "boot_errors":
        rc, out, err = _run(["journalctl", "-p", "err", "-b",
                             "--no-pager", "-n", "80"], timeout=20)
        if rc != 0:
            return tool_error(err.strip() or "journalctl falhou")
        return tool_result({"lines": out.strip().splitlines()[-80:]})
    return tool_error("action desconhecida: %r" % action)


# ── registro ─────────────────────────────────────────────────────────────

_TOOLS = (
    ("hypr_info",   "hyprland",  HYPR_INFO_SCHEMA,   _handle_hypr_info,
     _check_hyprland, "🪟"),
    ("hypr_ctl",    "hyprland",  HYPR_CTL_SCHEMA,    _handle_hypr_ctl,
     _check_hyprland, "🎛️"),
    ("pacman_info", "archlinux", PACMAN_INFO_SCHEMA, _handle_pacman_info,
     _check_arch, "📦"),
    ("arch_system", "archlinux", ARCH_SYSTEM_SCHEMA, _handle_arch_system,
     _check_arch, "🐧"),
)


def register(ctx) -> None:
    """Chamado uma vez pelo loader de plugins do hypr-ia."""
    for name, toolset, schema, handler, check_fn, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            emoji=emoji,
        )
