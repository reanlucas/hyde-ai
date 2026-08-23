"""Ferramentas do modo agente e a regra que decide o que pode rodar sozinho.

O desenho tem uma unica ideia: **so roda sem perguntar o que comprovadamente
nao muda nada**. A lista de comandos de leitura e fechada e curta; qualquer
coisa fora dela -- comando desconhecido, redirecionamento, substituicao de
comando -- cai na confirmacao. Errar para o lado de perguntar e barulhento;
errar para o outro lado apaga arquivo.
"""

import json
import os
import shlex
import subprocess

# Comandos que leem e nao escrevem. Nomes exatos, sem caminho.
LEITURA = frozenset("""
    ls dir pwd cd echo printf cat bat head tail less more wc nl
    find fd grep rg egrep fgrep awk sed cut sort uniq tr column jq yq
    stat file du df realpath readlink basename dirname which type command
    date cal uptime uname hostname whoami id groups env printenv locale
    ps top htop free lscpu lsblk lsusb lspci lsmod sensors
    systemctl journalctl loginctl
    pacman paru yay flatpak
    git hyprctl playerctl wpctl pactl amixer
    python3 node curl ping dig host ip ss
    ollama nvidia-smi rocm-smi
    diff cmp md5sum sha256sum tree xxd strings
""".split())

# Subcomandos que tornam um comando de leitura em escrita.
SUBCOMANDO_ESCREVE = {
    "systemctl": {"start", "stop", "restart", "reload", "enable", "disable",
                  "mask", "unmask", "set-property", "kill", "isolate",
                  "daemon-reload", "edit"},
    "pacman":    {"-S", "-R", "-U", "-Sy", "-Syu", "-Rns", "-Rs", "--sync",
                  "--remove", "--upgrade"},
    "paru":      {"-S", "-R", "-U", "-Sy", "-Syu", "-Rns"},
    "yay":       {"-S", "-R", "-U", "-Sy", "-Syu", "-Rns"},
    "flatpak":   {"install", "uninstall", "update", "remove", "override"},
    "git":       {"push", "commit", "merge", "rebase", "reset", "checkout",
                  "clean", "rm", "mv", "apply", "cherry-pick", "revert",
                  "tag", "fetch", "pull", "stash", "init", "add", "restore"},
    "hyprctl":   {"dispatch", "keyword", "reload", "setprop", "seterror"},
    "playerctl": {"play", "pause", "play-pause", "next", "previous", "stop",
                  "volume", "position"},
    "wpctl":     {"set-volume", "set-mute", "set-default"},
    "pactl":     {"set-sink-volume", "set-sink-mute", "set-source-volume",
                  "load-module", "unload-module"},
    "ip":        {"add", "del", "set", "link", "route"},
    "ollama":    {"pull", "rm", "create", "cp", "push", "run", "serve"},
    "curl":      {"-o", "-O", "--output", "--upload-file", "-T"},
    # sed -i e awk com redirecao ja caem na regra de redirecionamento; -i e
    # a forma que passa despercebida, entao ela e tratada aqui.
    "sed":       {"-i", "--in-place"},
}

# Operadores que separam comandos numa linha de shell.
_SEPARADORES = (";", "&&", "||", "|", "&")
# Coisas que escrevem ou executam texto arbitrario.
_PERIGOSOS = (">", ">>", "<(", "$(", "`", "sudo", "doas", "pkexec")


def _segmentos(cmd):
    """Quebra a linha nos operadores de shell, preservando aspas."""
    try:
        pedacos = shlex.split(cmd, posix=True)
    except ValueError:
        return None                       # aspas nao fecham: nao da para julgar
    fora = []
    atual = []
    for token in pedacos:
        if token in _SEPARADORES:
            if atual:
                fora.append(atual)
            atual = []
        else:
            atual.append(token)
    if atual:
        fora.append(atual)
    return fora


def so_leitura(cmd):
    """True quando cada segmento do comando comprovadamente so le.

    Na duvida devolve False, que significa "pergunte ao usuario".
    """
    if not cmd or not cmd.strip():
        return False
    bruto = cmd.strip()

    # Redirecionamento, substituicao de comando e escalada aparecem como texto
    # bruto; o shlex os engoliria como tokens comuns.
    for marca in _PERIGOSOS:
        if marca in bruto:
            return False

    segs = _segmentos(bruto)
    if not segs:
        return False

    for seg in segs:
        if not seg:
            return False
        prog = os.path.basename(seg[0])
        if prog not in LEITURA:
            return False
        escreve = SUBCOMANDO_ESCREVE.get(prog)
        if escreve:
            for arg in seg[1:]:
                if arg in escreve:
                    return False
        # python3 -c e node -e executam codigo arbitrario
        if prog in ("python3", "node") and any(
                a in ("-c", "-e", "--eval") for a in seg[1:]):
            return False
    return True


FERRAMENTAS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Roda um comando de shell nesta maquina Arch Linux com Hyprland "
                "e devolve a saida. Comandos de leitura rodam direto; qualquer "
                "coisa que altere o sistema so roda depois que o usuario "
                "confirmar, entao explique o que vai fazer antes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {
                        "type": "string",
                        "description": "o comando completo, como se digitado no terminal",
                    },
                },
                "required": ["cmd"],
            },
        },
    },
]

LIMITE_SAIDA = 8000          # caracteres devolvidos ao modelo


def executar(cmd, timeout=45):
    """Roda o comando e devolve (texto, codigo). Nunca levanta excecao."""
    try:
        r = subprocess.run(["bash", "-lc", cmd], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ("(o comando passou de %ds e foi interrompido)" % timeout, 124)
    except Exception as exc:                                  # noqa: BLE001
        return ("(nao consegui rodar: %s)" % exc, 1)

    saida = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
    saida = saida.strip()
    if len(saida) > LIMITE_SAIDA:
        cortado = len(saida) - LIMITE_SAIDA
        saida = saida[:LIMITE_SAIDA] + "\n(... %d caracteres cortados)" % cortado
    if not saida:
        saida = "(sem saida)"
    return (saida, r.returncode)


def resultado_para_modelo(cmd, saida, codigo):
    """Empacota o resultado no formato que o modelo espera de volta."""
    return json.dumps({"cmd": cmd, "exit_code": codigo, "output": saida},
                      ensure_ascii=False)
