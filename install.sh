#!/usr/bin/env bash
# Instala o hyde-ai. Idempotente.
set -uo pipefail
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}"
LIB="$HOME/.local/lib/hyde-ai"
BIN="$HOME/.local/bin"
# Uma instalacao concluida precisa ter backend, tema e diagnostico funcionais.
# HYDE_AI_STRICT=0 mantem o comportamento tolerante antigo para manutencao.
HYDE_AI_STRICT="${HYDE_AI_STRICT:-1}"

exigir() {
    local mensagem="$1"
    if [ "$HYDE_AI_STRICT" = "1" ]; then
        echo "    ERRO: $mensagem" >&2
        exit 1
    fi
    echo "    AVISO: $mensagem" >&2
}

cat <<'AVISO'
+---------------------------------------------------------------+
|  hyde-ai esta em BETA -- uso nao recomendado.                  |
|  Instavel, com caminhos nao testados e API sujeita a mudar.    |
|                                                               |
|  O backend e o Hypr-IA: todo turno e agentico e pode rodar     |
|  comandos na sua maquina. O que altera o sistema pede a sua    |
|  permissao, inline na conversa.                                |
+---------------------------------------------------------------+
AVISO
sleep 2

echo "==> Dependencias"
faltando=()
# gtk4-layer-shell: painel ancorado na borda
# python-matplotlib: tipografia das formulas (parser TeX completo)
# librsvg: rasteriza o SVG da formula na densidade da tela
# python-markdown-it-py + mdit_py_plugins: markdown e matematica
for p in gtk4-layer-shell python-gobject libadwaita gtksourceview5 \
         python-matplotlib librsvg python-markdown-it-py python-mdit_py_plugins \
         python-pylatexenc; do
    pacman -Qq "$p" &>/dev/null || faltando+=("$p")
done
if [ ${#faltando[@]} -gt 0 ]; then
    echo "    instalando: ${faltando[*]}"
    sudo pacman -S --needed --noconfirm "${faltando[@]}" || {
        echo "    ERRO: instale manualmente e rode de novo" >&2; exit 1; }
fi

echo "==> Arquivos"
# Painel rodando: para antes de trocar os .py e mexer no config. Um painel
# em execucao regrava o config da memoria ao encerrar, apagando o que o
# install gravar por baixo dele.
# Padrao ancorado no interpretador + caminho instalado: nao casa com editores
# nem shells que tenham "hyde-ai/main.py" na linha de comando.
PADRAO='python[0-9.]* .*/lib/hyde-ai/main\.py'
espera_painel() {  # ate ~10s para o processo sumir
    for _ in $(seq 1 20); do
        pgrep -u "$USER" -f "$PADRAO" >/dev/null 2>&1 || return 0
        sleep 0.5
    done
    return 1
}
RODAVA=0
if pgrep -u "$USER" -f "$PADRAO" >/dev/null 2>&1; then
    RODAVA=1
    echo "    parando o painel para atualizar"
    "$BIN/hyde-ai" --quit >/dev/null 2>&1 || true
    if ! espera_painel; then
        pkill -u "$USER" -f "$PADRAO"
        espera_painel || echo "    AVISO: painel ainda rodando; config pode ser sobrescrita" >&2
    fi
fi
mkdir -p "$LIB" "$BIN" "$CFG/hyde/wallbash/always" "$CFG/hyde/wallbash/scripts" \
         "$HOME/.local/state/hyde-ai" "$HOME/.cache/hyde-ai/math"
# modulos aposentados pela fusao com o backend (upgrade de versoes antigas)
rm -f "$LIB/agent.py" "$LIB/providers.py" \
      "$LIB/hermes_client.py" "$LIB/hermes_registry.py"
rm -rf "$LIB/__pycache__"
cp -f "$BASE"/lib/*.py "$LIB/"
cp -f "$BASE"/bin/hyde-ai "$BIN/" && chmod +x "$BIN/hyde-ai"
cp -f "$BASE"/wallbash/always/*.dcol "$CFG/hyde/wallbash/always/"
cp -f "$BASE"/wallbash/scripts/*.sh "$CFG/hyde/wallbash/scripts/" \
    && chmod +x "$CFG/hyde/wallbash/scripts"/*.sh

if [ ! -f "$CFG/hyde-ai/config.json" ]; then
    mkdir -p "$CFG/hyde-ai"
    cp "$BASE/config.example.json" "$CFG/hyde-ai/config.json"
    chmod 600 "$CFG/hyde-ai/config.json"
    echo "    config criada em $CFG/hyde-ai/config.json"
else
    echo "    config existente preservada"
fi

echo "==> Hypr-IA (backend)"
# O painel nao fala com APIs de modelo: ele spawna o gateway do Hypr-IA num
# venv proprio (o Python do sistema nao serve -- o backend exige
# >=3.11,<3.14) e conversa por JSON-RPC via stdio. A base do Hypr-IA e um
# fork do hermes-agent (Nous Research); o clone abaixo e so o bootstrap.
HYPRIA_DIR="${HYDE_AI_HYPRIA_DIR:-${HYDE_AI_HERMES_DIR:-$HOME/Projetos/hypr-ia}}"
HYPRIA_HOME="${HERMES_HOME:-$HOME/.hypr-ia}"

# Migracao da era "hermes": pasta do checkout e pasta de estado.
if [ ! -e "$HYPRIA_DIR" ] && [ -f "$HOME/Projetos/hermes-agent/pyproject.toml" ]; then
    echo "    migrando ~/Projetos/hermes-agent -> $HYPRIA_DIR"
    mv "$HOME/Projetos/hermes-agent" "$HYPRIA_DIR"
fi
if [ ! -e "$HYPRIA_HOME" ] && [ -d "$HOME/.hermes" ]; then
    echo "    migrando ~/.hermes -> $HYPRIA_HOME"
    mv "$HOME/.hermes" "$HYPRIA_HOME"
fi

if [ ! -f "$HYPRIA_DIR/pyproject.toml" ]; then
    echo "    clonando a base do Hypr-IA em $HYPRIA_DIR"
    git clone --depth 1 https://github.com/NousResearch/hermes-agent "$HYPRIA_DIR" \
        || exigir "clone falhou -- defina HYDE_AI_HYPRIA_DIR ou rode novamente"
fi
if [ ! -f "$HYPRIA_DIR/pyproject.toml" ]; then
    exigir "sem o checkout do Hypr-IA o painel nao tem backend"
else
    if ! command -v uv &>/dev/null; then
        echo "    instalando uv (gerencia o venv e o Python 3.11 do backend)"
        sudo pacman -S --needed --noconfirm uv \
            || exigir "nao foi possivel instalar uv"
    fi
    if command -v uv &>/dev/null; then
        echo "    uv sync em $HYPRIA_DIR (a primeira vez demora)"
        (cd "$HYPRIA_DIR" && uv sync) \
            || exigir "uv sync falhou"
    else
        exigir "uv nao esta disponivel"
    fi
    # hypria.path entra sempre que o checkout existe; hypria.python so com o
    # venv pronto (sem ele, from_config ainda acha .venv/bin/python depois).
    HPY="$HYPRIA_DIR/.venv/bin/python"
    [ -x "$HPY" ] || HPY=""
    [ -n "$HPY" ] || exigir "venv do Hypr-IA nao foi criado por uv sync"
    if python3 - "$CFG/hyde-ai/config.json" "$HPY" "$HYPRIA_DIR" <<'PYEOF'
import json, os, sys
caminho, py, repo = sys.argv[1], sys.argv[2], sys.argv[3]
dados = {}
if os.path.exists(caminho):
    try:
        dados = json.load(open(caminho))
    except Exception:
        dados = {}
if "hermes" in dados and "hypria" not in dados:
    dados["hypria"] = dados.pop("hermes")
hypria = dados.setdefault("hypria", {})
hypria["path"] = repo
if py:
    hypria["python"] = py
tmp = caminho + ".tmp"
with open(tmp, "w") as fh:
    json.dump(dados, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
os.replace(tmp, caminho)
os.chmod(caminho, 0o600)
PYEOF
    then
        if [ -n "$HPY" ]; then
            echo "    hypria.python e hypria.path gravados no config"
        else
            echo "    hypria.path gravado; venv ausente -- rode 'uv sync' em" \
                 "$HYPRIA_DIR e o install.sh de novo"
        fi
    else
        exigir "falhou gravar hypria.python e hypria.path no config"
    fi

    echo "==> Plugin hypr-arch (tools Hyprland + Arch)"
    mkdir -p "$HYPRIA_HOME/plugins"
    rm -rf "$HYPRIA_HOME/plugins/hypr-arch"
    cp -r "$BASE/plugins/hypr-arch" "$HYPRIA_HOME/plugins/hypr-arch"
    if [ -n "$HPY" ]; then
        # Plugins de usuario sao opt-in: entra em plugins.enabled no
        # config.yaml do backend (o venv tem PyYAML; o python3 do sistema
        # pode nao ter).
        if HERMES_HOME="$HYPRIA_HOME" "$HPY" - "$HYPRIA_HOME/config.yaml" <<'PYEOF'
import sys, yaml, os
caminho = sys.argv[1]
dados = {}
if os.path.exists(caminho):
    with open(caminho) as fh:
        dados = yaml.safe_load(fh) or {}
plugins = dados.setdefault("plugins", {})
ativos = plugins.get("enabled") or []
if "hypr-arch" not in ativos:
    ativos.append("hypr-arch")
    plugins["enabled"] = ativos
    tmp = caminho + ".tmp"
    with open(tmp, "w") as fh:
        yaml.safe_dump(dados, fh, sort_keys=False, allow_unicode=True)
    os.replace(tmp, caminho)
PYEOF
        then
            echo "    plugin instalado e ativado (toolsets hyprland + archlinux)"
        else
            exigir "nao foi possivel ativar o plugin hypr-arch"
        fi
    else
        exigir "plugin hypr-arch exige o venv do Hypr-IA"
    fi
fi

echo "==> Cores do tema"
# O painel le ~/.cache/hyde/wallbash/hyde-ai.css. Sem esse arquivo ele nasce
# sem estilo nenhum -- fonte do sistema, sem cores -- e so se conserta quando
# o usuario troca de tema por conta propria. "hyde-shell reload" nem sempre
# passa pelos templates, entao reaplicar o tema atual e o caminho garantido.
hyde-shell reload >/dev/null 2>&1 || true
CSS="${XDG_CACHE_HOME:-$HOME/.cache}/hyde/wallbash/hyde-ai.css"
if [ ! -s "$CSS" ] || ! grep -q "effort-btn" "$CSS" 2>/dev/null; then
    TEMA="$(sed -n 's/^HYDE_THEME="\(.*\)"$/\1/p' \
            "${XDG_STATE_HOME:-$HOME/.local/state}/hyde/staterc" 2>/dev/null)"
    SWITCH="$HOME/.local/lib/hyde/theme.switch.sh"
    if [ -n "$TEMA" ] && [ -x "$SWITCH" ]; then
        "$SWITCH" -s "$TEMA" >/dev/null 2>&1 || true
    fi
fi
if [ -s "$CSS" ]; then
    echo "    stylesheet em $CSS"
else
    exigir "stylesheet nao gerada; aplique um tema HyDE e rode novamente"
fi

echo "==> Verificacao"
saida="$("$BIN/hyde-ai" --doctor 2>&1)"; codigo=$?
echo "$saida" | grep -E '\[ *(ok|warn|FAIL|--) *\]|problem' || true
if [ "$codigo" -ne 0 ]; then
    exigir "o doctor encontrou problemas"
fi

if [ "$RODAVA" -eq 1 ]; then
    echo "==> Reiniciando o painel"
    "$BIN/hyde-ai" --daemon >/dev/null 2>&1 || exigir "nao foi possivel reiniciar o painel"
fi
echo "==> Pronto.  hyde-ai --setup   para conferir o Hypr-IA e as chaves"
