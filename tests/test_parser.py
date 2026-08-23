#!/usr/bin/env python3
"""Bateria do parser: saidas reais de modelo que ja quebraram a renderizacao.

Cada caso aqui veio de uma resposta que apareceu errada na tela. Rodar:

    python3 tests/test_parser.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import sidebar          # noqa: E402,F401  - registra os ganchos de matematica
import mdrender         # noqa: E402

# Qualquer coisa que sobre disto na prosa e LaTeX que nao foi renderizado.
CRU = re.compile(r"\\[A-Za-z]+|\$\$|\\\[|\\\(")
TAG = re.compile(r"<[^>]+>")

# (entrada, tipos de segmento esperados)
CASOS = [
    # negrito antes da formula: a linha inteira virava "formula" e saia crua
    ("**Resposta:** $\\sqrt{\\pi}$", ["texto"]),
    # $$ na mesma linha da frase: o markdown-it entrega inline, nao bloco
    ("Com $a_n = 0$ e $b_n = \\frac{2(-1)^{n+1}}{n}$:\n"
     "$$\\frac{1}{\\pi} = \\sum_{n=1}^\\infty \\frac{4}{n^2}$$", ["texto", "math"]),
    ("Segue:\n\n$$\\int_0^1 x^2 dx = \\frac13$$\n\nPortanto.",
     ["texto", "math", "texto"]),
    ("A identidade \\[ e^{i\\pi} + 1 = 0 \\] e famosa.", None),
    ("Considere \\(x^2\\) e \\(y^2\\).", ["texto"]),
    ("```math\n\\iint_{[a,b]} f(x)dx\n```", ["math"]),
    ("1. Defina $I = \\int_0^\\infty e^{-x}dx$\n2. Some", ["texto"]),
    ("\\boxed{I = \\sqrt{\\pi}}", ["math"]),
    ("Logo $I^2 = \\pi$, entao $I = \\sqrt{\\pi}$.", ["texto"]),
    ("Temos \\frac{1}{2} solto na prosa.", ["texto"]),
    ("\\iint_{[a,b]\\times[c,d]} f(x)\\,g(y)\\,dx\\,dy = "
     "\\left(\\int_a^b f(x)\\,dx\\right)\\!\\left(\\int_c^d g(y)\\,dy\\right)", ["math"]),
    ("Apenas texto, sem matematica.", ["texto"]),
]


# Corte do rabo incompleto durante o streaming.
STREAMING = [
    ("1. Calcule $b_n = \\frac{2(-1)^{", "1. Calcule"),
    ("Resposta: $\\sqrt{\\pi}$ pronto", "Resposta: $\\sqrt{\\pi}$ pronto"),
    ("Segue:\n$$\\int_0^1 x", "Segue:"),
    ("Segue:\n$$\\int_0^1 x dx$$ fim", "Segue:\n$$\\int_0^1 x dx$$ fim"),
    ("A identidade \\[ e^{i\\pi}", "A identidade"),
    ("A identidade \\[ e^{i\\pi} \\] e famosa", "A identidade \\[ e^{i\\pi} \\] e famosa"),
    ("$a$ e $b$ e $c", "$a$ e $b$ e"),
    ("preco: 10 dolares", "preco: 10 dolares"),
    ("texto sem matematica", "texto sem matematica"),
]


def main():
    falhas = []
    for entrada, esperado in STREAMING:
        obtido = sidebar._corta_math_incompleta(entrada)
        if obtido != esperado:
            falhas.append("streaming %-30r -> %r, esperado %r"
                          % (entrada[:30], obtido, esperado))

    for entrada, esperado in CASOS:
        segs = list(mdrender.segmentos(entrada))
        tipos = [s[0] for s in segs]
        rotulo = entrada.replace("\n", " ")[:46]

        if esperado is not None and tipos != esperado:
            falhas.append("%-48s tipos %s, esperado %s" % (rotulo, tipos, esperado))

        for seg in segs:
            if seg[0] != "texto":
                continue
            visivel = TAG.sub("", str(seg[1]))
            achado = CRU.search(visivel)
            if achado:
                falhas.append("%-48s LaTeX cru: %r"
                              % (rotulo, visivel[achado.start():achado.start() + 30]))

    for linha in falhas:
        print("FALHA  " + linha)
    print("\n%d casos, %d falhas" % (len(CASOS) + len(STREAMING), len(falhas)))
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
