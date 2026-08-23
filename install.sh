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
hyde-shell reload >/dev/null 2>&1 || true

echo "==> Verificacao"
"$BIN/hyde-ai" --doctor 2>&1 | grep -E '\[ *(ok|warn|--) *\]' | head -20 || true
echo "==> Pronto.  hyde-ai --setup   para as chaves de API"
