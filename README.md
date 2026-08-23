<div align="center">

# hyde-ai

**Chat com LLM numa barra lateral do Hyprland.**

GTK4 + layer-shell, com as cores do wallbash do
[HyDE](https://github.com/HyDE-Project/HyDE).

Claude · Gemini · ChatGPT · Ollama local — todos com streaming.

</div>

> [!WARNING]
> **Projeto em beta. Uso não recomendado.**
>
> Escrito para um setup específico e ainda instável: a interface tem arestas,
> a API muda sem aviso e há caminhos não testados. Se for experimentar, espere
> quebrar — e não conte com ele para nada importante.

---

<div align="center">
  <img src="docs/sidebar.gif" width="860" alt="The sidebar slides in from the right edge over the desktop, slides out, reopens docked to the left edge, then returns to the right">
  <p><em>Uma barra lateral de verdade: layer-shell, desliza sobre o desktop sem
  redimensionar nada. <code>/side left</code> troca a borda.</em></p>
</div>

<div align="center">
  <img src="docs/ssh.gif" width="400" alt="Asking how to configure a GitHub SSH connection on Arch Linux; the answer streams in with numbered steps and syntax-highlighted shell blocks">
  &nbsp;&nbsp;
  <img src="docs/painel.png" width="400" alt="The finished answer: numbered steps with ssh-keygen, chmod and ssh -T commands in highlighted code blocks, with the reasoning collapsed at the bottom">
  <p><em>Uma pergunta prática, respondida pelo Qwen3.5 9B rodando local.
  Blocos de código com realce de sintaxe e numeração. Acelerado 3,5&times;.</em></p>
</div>

<div align="center">
  <img src="docs/conversa.gif" width="400" alt="A maths question answered live: five display formulas typeset one after another as the answer streams in">
  <p><em>E uma de cálculo, com as fórmulas tipografadas conforme chegam.</em></p>
</div>

---

## Matemática tipografada

Fórmulas passam pelo `mathtext` do matplotlib, um parser TeX completo, e saem
em **SVG rasterizado na densidade real da tela** — nítidas em qualquer escala.

<div align="center">
  <img src="docs/matematica.png" width="400" alt="Rendered formulas: a Gaussian integral with limits, a stacked fraction, a nested square root and a boxed result, all typeset in the STIX font">
  <p><em>Stacked fractions, integrals with limits, radicands under the bar.
  STIX &mdash; the font used in scientific publishing.</em></p>
</div>

Equações longas são quebradas nos sinais de relação, como o ambiente `align`
do LaTeX — sem isso viravam uma faixa larga com rolagem horizontal.

Clicar numa fórmula copia o LaTeX; clique duplo revela o código como texto
selecionável.

---

## Instalação

### 1. Pré-requisitos

Arch Linux com Hyprland e [HyDE](https://github.com/HyDE-Project/HyDE).

### 2. Clonar e instalar

```bash
git clone https://github.com/reanlucas/hyde-ai.git
cd hyde-ai
./install.sh
```

O instalador resolve as dependências, copia os arquivos, gera as cores do tema
e roda um diagnóstico ao final. É idempotente.

### 3. Configurar os provedores

```bash
hyde-ai --setup
```

Pede as chaves de API e o modelo padrão. A configuração é gravada em
`~/.config/hyde-ai/config.json` com permissão `0600`.

**O Ollama não precisa de chave.** Se você já tem o serviço rodando, os modelos
aparecem sozinhos:

```bash
sudo pacman -S ollama-rocm      # AMD; use "ollama" para CPU/NVIDIA
sudo systemctl enable --now ollama
ollama pull qwen3.5:9b
```

### 4. Abrir

```bash
hyde-ai --toggle
```

### 5. Atalho e autostart (opcional)

Em `~/.config/hypr/hyprland.lua`:

```lua
hl.bind("SUPER + I", hl.dsp.exec_cmd("$HOME/.local/bin/hyde-ai --toggle"), {
    description = "[Launcher|Apps] AI sidebar",
})

hl.on("hyprland.start", function()
    hl.exec_cmd("$HOME/.local/bin/hyde-ai --daemon")
end)
```

---

## Uso

`Enter` envia · `Shift+Enter` quebra linha · `Esc` fecha

Digite **`/`** no campo para abrir a paleta de comandos, que filtra conforme
você escreve.

| Comando | O que faz |
|---|---|
| `/clear` | conversa nova |
| `/historico` | lista as conversas salvas |
| `/provider` `/model` | troca de provedor ou modelo |
| `/key <provedor> <valor>` | guarda uma chave de API |
| `/velocidade` | tokens/s médio por modelo |
| `/refresh` | reprocura provedores e modelos |
| `/agente on\|off` | deixa o modelo rodar comandos aqui |
| `/side left\|right` · `/width 35` | geometria do painel |

Da linha de comando, `hyde-ai --ask "sua pergunta"` abre o painel e envia a
pergunta direto — dá para pendurar num atalho ou chamar de um script.

---

## Velocidade

Duas medidas, deliberadamente distintas:

- **Ao lado do modelo** — média histórica das últimas 60 execuções: *como ele
  vem rodando*
- **No cabeçalho de cada resposta** — tempo até o primeiro token, tempo de
  geração e a velocidade **daquela** resposta

```
qwen3.5:9b  ·  0.8s pensando  ·  14.7s  ·  61 tok/s
```

O tempo é separado em dois de propósito: *pensando* é da submissão ao primeiro
token (carregamento, prompt eval). A velocidade é calculada só sobre a geração
— incluir a espera falsearia o número para baixo.

As métricas ficam gravadas junto da mensagem, então acompanham a conversa.

---

## Modelos de raciocínio

Qwen3, o-series e Claude com *thinking* geram um rascunho antes da resposta.

**Desligado por padrão** — o rascunho multiplica o tempo de resposta por cinco
ou mais, e quase nunca compensa numa barra lateral. O botão ao lado do envio
liga por pergunta; `/think` aceita `auto`, `on`, `off`, `low`, `medium` e
`high`.

Com ele ligado, o cabeçalho pulsa **Pensando…** enquanto o modelo trabalha e
vira **Pensou por 36s** quando termina — clicável, com o rascunho inteiro
dentro.

Sem isso a bolha ficaria vazia durante todo o raciocínio, e vazia para sempre
se o rascunho consumisse todo o `max_tokens`. Quando isso acontece, o painel
diz o que houve em vez de deixar a mensagem em branco.

---

## Modo agente

Ligado, o modelo pode **rodar comandos nesta máquina** para responder. Ele
encadeia chamadas: olha o estado, decide o próximo passo, e só então responde.

```
Você: por que o coletor de métricas parou?

  ▸ shell   systemctl --user status hyde-widgets-collector      ok
  ▸ shell   journalctl --user -u hyde-widgets-collector -n 30   ok

O caminho da GPU mudou de card1 para card2 depois do último
boot, e o coletor não encontra mais o sensor.
```

**Desligado por padrão.** O botão de terminal ao lado do envio liga, ou
`/agente on`. Só o Ollama, com modelos que anunciam `tools` — o `ollama show`
mostra quais.

### O que ele pode rodar sozinho

Comando de leitura roda direto. Qualquer coisa que altere o sistema mostra o
comando exato e espera **Permitir** ou **Negar**:

```
  ▸ shell   sed -i 's/card1/card2/' collector
            [ Negar ]  [ Permitir ]
```

A classificação é uma lista fechada de comandos de leitura, aplicada a **cada
segmento** da linha — um `ls | sh` não passa porque `sh` não está na lista.
Redirecionamento (`>`), substituição de comando (`` ` ``, `$(...)`), `sudo`,
`python3 -c` e subcomandos que escrevem (`git push`, `systemctl restart`,
`pacman -S`) caem todos na confirmação. Comando desconhecido também: a regra
erra de propósito para o lado de perguntar.

Negar não é o fim — o modelo recebe a recusa como resultado e explica o que
faria, em vez de tentar de novo.

```bash
python3 tests/test_agente.py     # 66 comandos contra o portão de segurança
```

| Config | |
|---|---|
| `agent.enabled` | ligado ou não (padrão: não) |
| `agent.max_steps` | teto de idas e voltas por pergunta (padrão: 8) |
| `agent.timeout` | segundos por comando (padrão: 45) |

---

## Histórico

Conversas salvas e agrupadas por provedor, com título, data e contagem de
mensagens. O botão no cabeçalho lista e restaura; guarda 40 conversas.

---

## Renderização

Markdown por [markdown-it-py](https://github.com/executablebooks/markdown-it-py)
— o mesmo motor (porte Python) usado pelos frontends web: tabelas com
alinhamento, listas, ênfase, código com realce via GtkSourceView.

Matemática detectada em quatro formatos, porque os modelos alternam entre eles:
`$$...$$`, `\[...\]`, ` ```math ` e parágrafos que são só LaTeX.

O `$` de shell não é confundido com matemática: `$HOME`, `$1`, `${VAR}` e `$5`
ficam como texto. Durante o streaming, uma fórmula ainda pela metade fica
escondida até o delimitador fechar — ela aparece inteira, de uma vez.

### Testes

```bash
python3 tests/test_bateria.py     # 43 casos: matemática, Python, JS, shell
python3 tests/test_parser.py      # 21 casos: regressões e streaming
```

Cada caso veio de uma resposta que apareceu errada na tela.

---

## Decisões de projeto

<details>
<summary><b>GTK4 nativo, não KaTeX no WebKit</b></summary>

O painel transmite token a token, e cada delta significaria re-renderizar um
documento HTML — pisca e custa caro num fluxo ao vivo. Widgets GTK atualizam de
forma incremental.

O custo dessa escolha é que a fórmula é imagem e não tem texto selecionável,
daí o clique-para-copiar.

</details>

<details>
<summary><b>Atualização incremental durante o streaming</b></summary>

Reconstruir a árvore de widgets a cada token fazia a mensagem sumir e
reaparecer. Preservando o prefixo de segmentos já fechados, nenhum widget
estável é destruído: **0 reconstruções** numa resposta com fórmula e tabela,
contra uma por token.

</details>

<details>
<summary><b>Disponibilidade do provedor vem do cache</b></summary>

Consultar `available()` no envio disparava uma sondagem de rede **síncrona na
thread da interface**, segurando a mensagem do usuário por até 2 segundos.

</details>

<details>
<summary><b>Delimitadores normalizados antes do parser</b></summary>

O markdown trata `\[` como escape e come a barra, o que fazia a fórmula
desaparecer no meio do parágrafo. A normalização roda antes, protegendo o que
estiver dentro de crase.

</details>

---

## Dependências

`gtk4-layer-shell` `python-gobject` `libadwaita` `gtksourceview5`
`python-matplotlib` `librsvg` `python-markdown-it` `python-mdit_py_plugins`
`python-pylatexenc` — todas nos repositórios oficiais do Arch.

---

## Licença

MIT
