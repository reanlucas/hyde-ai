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
+---------------------------------------------------------------+
AVISO
sleep 2

echo "==> Dependencias"
faltando=()
# gtk4-layer-shell: painel ancorado na borda
# python-matplotlib: tipografia das formulas (parser TeX completo)
# librsvg: rasteriza o SVG da formula na densidade da tela
# python-markdown-it + mdit_py_plugins: markdown e matematica
for p in gtk4-layer-shell python-gobject libadwaita gtksourceview5 \
         python-matplotlib librsvg python-markdown-it python-mdit_py_plugins \
         python-pylatexenc ttf-cascadia-code-nerd; do
    pacman -Qq "$p" &>/dev/null || faltando+=("$p")
done
if [ ${#faltando[@]} -gt 0 ]; then
    echo "    instalando: ${faltando[*]}"
    sudo pacman -S --needed --noconfirm "${faltando[@]}" || {
        echo "    ERRO: instale manualmente e rode de novo" >&2; exit 1; }
fi

echo "==> Arquivos"
mkdir -p "$LIB" "$BIN" "$CFG/hyde/wallbash/always" "$CFG/hyde/wallbash/scripts" \
         "$HOME/.local/state/hyde-ai" "$HOME/.cache/hyde-ai/math"
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
"$BIN/hyde-ai" --doctor 2>&1 | grep -E '\[ *(ok|warn|--) *\]' | head -20 || true
echo "==> Pronto.  hyde-ai --setup   para as chaves de API"
