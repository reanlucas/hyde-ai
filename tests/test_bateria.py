#!/usr/bin/env python3
"""Bateria larga do parser: matematica pesada, codigo e shell.

Cobre as duas camadas na ordem em que a interface as usa:

    parse_blocks()   separa cercas ``` e <think> do texto corrido
    segmentos()      quebra o texto em prosa, formulas e tabelas

O que cada caso verifica:

  * matematica  -- a formula vira segmento "math", nao texto cru
  * codigo      -- a cerca vira bloco de codigo com a linguagem certa,
                   e nada dentro dela e reinterpretado
  * shell       -- $VAR, $1, ${VAR} e $5 NAO viram matematica

Rodar:  python3 tests/test_bateria.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import sidebar          # noqa: E402  - registra os ganchos de matematica
import mdrender         # noqa: E402
import mathrender       # noqa: E402

TAG = re.compile(r"<[^>]+>")
# LaTeX que chegou visivel na prosa = falha de renderizacao
CRU = re.compile(r"\\[A-Za-z]+|\$\$|\\\[|\\\(")

falhas = []


def _prosa(seg):
    return TAG.sub("", str(seg[1]))


def checa_math(rotulo, texto, minimo=1):
    """A prosa nao pode conter LaTeX cru, e deve haver formulas renderizadas."""
    segs = mdrender.segmentos(texto)
    blocos = sum(1 for s in segs if s[0] == "math")
    inline = sum(1 for s in segs
                 if s[0] == "texto" and 'size="larger"' in str(s[1]))
    if blocos + inline < minimo:
        falhas.append("%s: %d formulas renderizadas, esperado >= %d"
                      % (rotulo, blocos + inline, minimo))
    for seg in segs:
        if seg[0] != "texto":
            continue
        achado = CRU.search(_prosa(seg))
        if achado:
            trecho = _prosa(seg)[achado.start():achado.start() + 36]
            falhas.append("%s: LaTeX cru na prosa -> %r" % (rotulo, trecho))


def checa_codigo(rotulo, texto, lang_esperada, deve_conter):
    """A cerca vira bloco de codigo, com linguagem e conteudo intactos."""
    blocos = [b for b in sidebar.parse_blocks(texto) if b[0] == "code"]
    if not blocos:
        falhas.append("%s: nenhuma cerca reconhecida" % rotulo)
        return
    kind, lang, corpo, fechado = blocos[0]
    if lang.strip().lower() != lang_esperada:
        falhas.append("%s: linguagem %r, esperado %r" % (rotulo, lang, lang_esperada))
    if not fechado:
        falhas.append("%s: cerca marcada como aberta" % rotulo)
    for trecho in deve_conter:
        if trecho not in corpo:
            falhas.append("%s: perdeu %r do corpo" % (rotulo, trecho))


def checa_sem_math(rotulo, texto):
    """Prosa com $ de shell ou de dinheiro nao pode virar formula."""
    segs = mdrender.segmentos(texto)
    for seg in segs:
        if seg[0] == "math":
            falhas.append("%s: virou formula -> %r" % (rotulo, str(seg[1])[:50]))
        elif 'size="larger"' in str(seg[1]):
            falhas.append("%s: trecho virou matematica inline -> %r"
                          % (rotulo, str(seg[1])[:60]))


# --------------------------------------------------------------------------
# 15 casos de matematica, com muitos simbolos
# --------------------------------------------------------------------------
MATEMATICA = [
    ("gaussiana",
     "O truque de Poisson da:\n\n"
     "$$\\left(\\int_{-\\infty}^{\\infty} e^{-x^2}\\,dx\\right)^2 = "
     "\\iint_{\\mathbb{R}^2} e^{-(x^2+y^2)}\\,dx\\,dy = \\pi$$"),
    ("basileia",
     "Por Parseval, $\\sum_{n=1}^{\\infty} \\frac{1}{n^2} = \\frac{\\pi^2}{6}$."),
    ("euler",
     "A identidade \\[ e^{i\\pi} + 1 = 0 \\] junta cinco constantes."),
    ("binomio",
     "$$\\binom{n}{k} = \\frac{n!}{k!\\,(n-k)!} \\quad\\text{para}\\quad 0 \\le k \\le n$$"),
    ("limite",
     "Temos $\\lim_{n\\to\\infty} \\left(1 + \\frac{1}{n}\\right)^n = e$."),
    ("derivada parcial",
     "$$\\frac{\\partial^2 u}{\\partial t^2} = c^2 \\nabla^2 u$$"),
    ("matriz",
     "$$A = \\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}, \\quad "
     "\\det A = ad - bc$$"),
    ("integral dupla",
     "\\iint_{[a,b]\\times[c,d]} f(x)\\,g(y)\\,dx\\,dy = "
     "\\left(\\int_a^b f(x)\\,dx\\right)\\!\\left(\\int_c^d g(y)\\,dy\\right)"),
    ("boxed",
     "Logo:\n\n$$\\boxed{I = \\sqrt{\\pi}}$$"),
    ("somatorio duplo",
     "$$\\sum_{i=1}^{n}\\sum_{j=1}^{m} a_{ij} x_i y_j \\ge 0$$"),
    ("resposta em negrito",
     "**Resposta:** $\\sqrt{\\pi}$"),
    ("misto na mesma linha",
     "Com $a_n = 0$ e $b_n = \\frac{2(-1)^{n+1}}{n}$:\n"
     "$$\\frac{1}{\\pi}\\cdot\\frac{2\\pi^3}{3} = \\sum_{n=1}^{\\infty}\\frac{4}{n^2}$$"),
    ("cerca math",
     "```math\n\\oint_C \\vec{F}\\cdot d\\vec{r} = "
     "\\iint_S (\\nabla\\times\\vec{F})\\cdot d\\vec{S}\n```"),
    ("teoria dos conjuntos",
     "Se $A \\subseteq B$ e $B \\subseteq C$, entao "
     "$A \\subseteq C$ para todo $A,B,C \\in \\mathcal{P}(X)$."),
    ("probabilidade",
     "$$\\mathbb{E}[X] = \\int_{-\\infty}^{\\infty} x\\,f_X(x)\\,dx, \\qquad "
     "\\operatorname{Var}(X) = \\mathbb{E}[X^2] - \\mathbb{E}[X]^2$$"),
    ("desigualdade",
     "Cauchy-Schwarz: $\\left|\\langle u,v\\rangle\\right| \\le \\|u\\|\\,\\|v\\|$."),
]

# --------------------------------------------------------------------------
# 5 casos de programacao (Python e JavaScript)
# --------------------------------------------------------------------------
CODIGO = [
    ("python funcao", "python",
     "```python\ndef media(xs):\n    return sum(xs) / len(xs)\n```",
     ["def media(xs):", "sum(xs) / len(xs)"]),
    ("python com f-string e $", "python",
     "```python\nimport os\ncaminho = os.environ['HOME']\nprint(f\"$HOME = {caminho}\")\n```",
     ["os.environ['HOME']", '$HOME = {caminho}']),
    ("python type hints", "python",
     "```python\nfrom typing import Iterable\n\n"
     "def soma(xs: Iterable[float]) -> float:\n    return sum(xs)\n```",
     ["Iterable[float]", "-> float"]),
    ("javascript async", "javascript",
     "```javascript\nconst dados = await fetch(url).then(r => r.json());\n"
     "console.log(`total: ${dados.length}`);\n```",
     ["await fetch(url)", "${dados.length}"]),
    ("javascript regex", "js",
     "```js\nconst re = /^\\$\\{(\\w+)\\}$/;\n"
     "if (re.test(s)) console.log('variavel');\n```",
     ["/^\\$\\{(\\w+)\\}$/", "re.test(s)"]),
]

# --------------------------------------------------------------------------
# 5 casos de shell e bash -- onde o $ colide com a matematica
# --------------------------------------------------------------------------
SHELL_CERCADO = [
    ("bash path", "bash",
     "```bash\nexport PATH=\"$HOME/.local/bin:$PATH\"\necho \"$PATH\"\n```",
     ['export PATH="$HOME/.local/bin:$PATH"']),
    ("bash argumentos", "bash",
     "```bash\n#!/usr/bin/env bash\necho \"primeiro: $1  segundo: $2\"\n"
     "echo \"total: $#\"\n```",
     ['primeiro: $1  segundo: $2', "$#"]),
    ("bash substituicao", "sh",
     "```sh\nagora=$(date +%s)\ntamanho=${#agora}\necho \"${agora} tem $tamanho digitos\"\n```",
     ["$(date +%s)", "${#agora}"]),
    ("bash loop", "bash",
     "```bash\nfor f in *.txt; do\n  mv -- \"$f\" \"${f%.txt}.md\"\ndone\n```",
     ['"${f%.txt}.md"']),
    ("bash condicional", "bash",
     "```bash\nif [ -z \"$VAR\" ]; then\n  echo 'vazia' >&2\n  exit 1\nfi\n```",
     ['[ -z "$VAR" ]']),
]

# Shell citado na PROSA, fora de cerca: o caso que mais quebra
SHELL_PROSA = [
    ("prosa com HOME e PATH",
     "A variavel $HOME aponta para a sua pasta e $PATH lista os diretorios."),
    ("prosa com argumentos",
     "Passe $1 e $2 para o script; $# tem a contagem."),
    ("prosa com chaves",
     "Use ${VAR} quando precisar colar o nome, ou $VAR quando nao precisar."),
    ("prosa com dinheiro",
     "A licenca custa $5 por mes, ou $50 no ano."),
    ("prosa com cifrao solto",
     "O prompt do root e # e o de usuario e $, por convencao."),
]


# O renderizador precisa dar conta das formulas, nao so o parser reconhece-las.
# Cada uma destas ja falhou por um comando que o mathtext nao conhece.
RENDERIZAVEIS = [
    r"\binom{n}{k} = \frac{n!}{k!\,(n-k)!} \quad\text{para}\quad 0 \le k \le n",
    r"\sum_{i=1}^{n}\sum_{j=1}^{m} a_{ij} x_i y_j \ge 0",
    r"\left|\langle u,v\rangle\right| \le \|u\|\,\|v\|",
    r"\begin{cases} x & \text{se } x>0 \\ -x & \text{caso contrario} \end{cases}",
    r"x \to \infty \implies f(x) \ne 0",
    r"A = \begin{pmatrix} a & b \\ c & d \end{pmatrix}, \quad \det A = ad - bc",
    r"\oint_C \vec{F}\cdot d\vec{r} = \iint_S (\nabla\times\vec{F})\cdot d\vec{S}",
    r"\mathbb{E}[X] = \int_{-\infty}^{\infty} x\,f_X(x)\,dx",
    r"P(A \mid B) = \frac{P(B \mid A)\,P(A)}{P(B)}",
    r"\lfloor x \rfloor \le x < \lfloor x \rfloor + 1",
    r"\underbrace{a+b+\cdots+z}_{26\text{ termos}}",
    r"\boxed{I = \sqrt{\pi}}",
]


def checa_render(tex):
    if not mathrender.render(tex, "#FFFFFF", 14, display=True):
        falhas.append("render: mathtext recusou -> %s" % tex[:70])


def main():
    for tex in RENDERIZAVEIS:
        checa_render(tex)
    for rotulo, texto in MATEMATICA:
        checa_math("math/" + rotulo, texto)
    for rotulo, lang, texto, contem in CODIGO:
        checa_codigo("code/" + rotulo, texto, lang, contem)
    for rotulo, lang, texto, contem in SHELL_CERCADO:
        checa_codigo("shell/" + rotulo, texto, lang, contem)
    for rotulo, texto in SHELL_PROSA:
        checa_sem_math("prosa/" + rotulo, texto)

    total = (len(MATEMATICA) + len(CODIGO) + len(SHELL_CERCADO)
             + len(SHELL_PROSA) + len(RENDERIZAVEIS))
    for linha in falhas:
        print("FALHA  " + linha)
    print("\n%d casos, %d falhas" % (total, len(falhas)))
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
