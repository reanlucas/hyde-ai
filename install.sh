#!/usr/bin/env bash
# Instala o hyde-ai. Idempotente.
set -uo pipefail
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}"
LIB="$HOME/.local/lib/hyde-ai"
BIN="$HOME/.local/bin"

cat <<'AVISO'
+---------------------------------------------------------------+
|  hyde-ai esta em BETA -- uso nao recomendado.                  |
|  Instavel, com caminhos nao testados e API sujeita a mudar.    |
|                                                               |
|  O backend e o Hermes Agent: todo turno e agentico e pode      |
|  rodar comandos na sua maquina. O que altera o sistema pede    |
|  a sua permissao, inline na conversa.                          |
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
         python-pylatexenc ttf-cascadia-code-nerd; do
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
# modulos aposentados pela fusao com o Hermes (upgrade de versoes antigas)
rm -f "$LIB/agent.py" "$LIB/providers.py"
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

echo "==> Hermes (backend)"
# O painel nao fala mais com APIs de modelo: ele spawna o gateway do Hermes
# Agent num venv proprio (o Python do sistema nao serve -- o Hermes exige
# >=3.11,<3.14) e conversa por JSON-RPC via stdio.
HERMES_DIR="${HYDE_AI_HERMES_DIR:-$HOME/Projetos/hermes-agent}"
if [ ! -f "$HERMES_DIR/pyproject.toml" ]; then
    echo "    clonando o hermes-agent em $HERMES_DIR"
    git clone --depth 1 https://github.com/NousResearch/hermes-agent "$HERMES_DIR" \
        || echo "    AVISO: clone falhou -- clone manualmente (ou exporte" \
                "HYDE_AI_HERMES_DIR) e rode o install.sh de novo"
fi
if [ ! -f "$HERMES_DIR/pyproject.toml" ]; then
    echo "    AVISO: sem hermes-agent o painel nao tem backend"
else
    if ! command -v uv &>/dev/null; then
        echo "    instalando uv (gerencia o venv e o Python 3.11 do Hermes)"
        sudo pacman -S --needed --noconfirm uv \
            || echo "    AVISO: instale o uv manualmente e rode de novo"
    fi
    if command -v uv &>/dev/null; then
        echo "    uv sync em $HERMES_DIR (a primeira vez demora)"
        (cd "$HERMES_DIR" && uv sync) \
            || echo "    AVISO: uv sync falhou; rode-o manualmente"
    fi
    # hermes.path entra sempre que o checkout existe; hermes.python so com o
    # venv pronto (sem ele, from_config ainda acha .venv/bin/python depois).
    HPY="$HERMES_DIR/.venv/bin/python"
    [ -x "$HPY" ] || HPY=""
    if python3 - "$CFG/hyde-ai/config.json" "$HPY" "$HERMES_DIR" <<'PYEOF'
import json, os, sys
caminho, py, repo = sys.argv[1], sys.argv[2], sys.argv[3]
dados = {}
if os.path.exists(caminho):
    try:
        dados = json.load(open(caminho))
    except Exception:
        dados = {}
hermes = dados.setdefault("hermes", {})
hermes["path"] = repo
if py:
    hermes["python"] = py
tmp = caminho + ".tmp"
with open(tmp, "w") as fh:
    json.dump(dados, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
os.replace(tmp, caminho)
os.chmod(caminho, 0o600)
PYEOF
    then
        if [ -n "$HPY" ]; then
            echo "    hermes.python e hermes.path gravados no config"
        else
            echo "    hermes.path gravado; venv ausente -- rode 'uv sync' em" \
                 "$HERMES_DIR e o install.sh de novo"
        fi
    else
        echo "    AVISO: falhou gravar o config -- ajuste hermes.python e" \
             "hermes.path em $CFG/hyde-ai/config.json" >&2
    fi
fi

echo "==> Cores do tema"
# O painel le ~/.cache/hyde/wallbash/hyde-ai.css. Sem esse arquivo ele nasce
# sem estilo nenhum -- fonte do sistema, sem cores -- e so se conserta quando
# o usuario troca de tema por conta propria. "hyde-shell reload" nem sempre
# passa pelos templates, entao reaplicar o tema atual e o caminho garantido.
hyde-shell reload >/dev/null 2>&1 || true
CSS="${XDG_CACHE_HOME:-$HOME/.cache}/hyde/wallbash/hyde-ai.css"
if [ ! -s "$CSS" ] || ! grep -q "tool-cmd" "$CSS" 2>/dev/null; then
    TEMA="$(sed -n 's/^HYDE_THEME="\(.*\)"$/\1/p' \
            "${XDG_STATE_HOME:-$HOME/.local/state}/hyde/staterc" 2>/dev/null)"
    SWITCH="$HOME/.local/lib/hyde/theme.switch.sh"
    if [ -n "$TEMA" ] && [ -x "$SWITCH" ]; then
        "$SWITCH" -s "$TEMA" >/dev/null 2>&1 || true
    fi
fi
[ -s "$CSS" ] && echo "    stylesheet em $CSS" \
              || echo "    AVISO: stylesheet nao gerada -- troque de tema uma vez"

echo "==> Verificacao"
saida="$("$BIN/hyde-ai" --doctor 2>&1)"; codigo=$?
echo "$saida" | grep -E '\[ *(ok|warn|FAIL|--) *\]|problem' || true
if [ "$codigo" -ne 0 ]; then
    echo "    AVISO: o doctor achou problemas -- rode: hyde-ai --doctor"
fi

if [ "$RODAVA" -eq 1 ]; then
    echo "==> Reiniciando o painel"
    "$BIN/hyde-ai" --daemon >/dev/null 2>&1 || true
fi
echo "==> Pronto.  hyde-ai --setup   para conferir o Hermes e as chaves"
