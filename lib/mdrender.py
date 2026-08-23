"""Renderizacao de markdown para o hyde-ai, sobre markdown-it-py.

Substitui ~22 regexes escritas a mao por um parser CommonMark/GFM de verdade
-- o mesmo motor (porte Python) que os fronts de LLM usam. A matematica vem
do plugin dollarmath, no nivel do parser, em vez de cacada com regex.

Entrega uma lista de segmentos que a interface transforma em widgets:
    ("texto", markup_pango)
    ("math",  latex)          -- formula em bloco
    ("tabela", cabec, alinhamentos, linhas)   -- celulas ja em markup
"""
from __future__ import annotations

import html
import re

from markdown_it import MarkdownIt

try:
    from mdit_py_plugins.dollarmath import dollarmath_plugin
    _TEM_MATH = True
except Exception:  # pragma: no cover
    _TEM_MATH = False


def _novo_md():
    md = MarkdownIt("commonmark")
    md.enable(["table", "strikethrough"])
    if _TEM_MATH:
        # allow_space=False: o "$" de fechamento nao pode vir depois de um
        # espaco, senao "a variavel $HOME ... e $PATH" vira uma formula.
        # allow_digits=False: "$1", "$2" de shell e "$5" de dinheiro tambem
        # deixam de ser confundidos com matematica.
        md = md.use(dollarmath_plugin, double_inline=True,
                    allow_space=False, allow_digits=False)
    return md


_MD = _novo_md()

# matematica inline continua em Unicode; o conversor vive no sidebar para
# reaproveitar a tabela e o pylatexenc.
_inline_math_hook = None


def registrar_math_inline(fn):
    """A interface injeta aqui como transformar TeX inline em markup."""
    global _inline_math_hook
    _inline_math_hook = fn


_solto_hook = None


def registrar_math_solto(fn):
    """Converte comandos TeX que aparecem sem delimitador na prosa."""
    global _solto_hook
    _solto_hook = fn


# Os modelos alternam entre os delimitadores TeX ( \\[ \\] e \\( \\) ) e os de
# dolar. O dollarmath so entende dolar, e pior: o markdown trata "\\[" como
# escape e come a barra, deixando "[ A\\mathbf{v} ]" na tela. Normalizamos
# antes de parsear, protegendo o que estiver dentro de crase.
_CERCA = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]*`)", re.S)
_TEX_BLOCO = re.compile(r"\\\[(.+?)\\\]", re.S)
_TEX_INLINE = re.compile(r"\\\((.+?)\\\)", re.S)


def normaliza_delimitadores(texto):
    partes = _CERCA.split(texto or "")
    for i in range(0, len(partes), 2):          # indices pares = fora de crase
        t = partes[i]
        t = _TEX_BLOCO.sub(lambda m: "\n\n$$%s$$\n\n" % m.group(1).strip(), t)
        t = _TEX_INLINE.sub(lambda m: "$%s$" % m.group(1).strip(), t)
        partes[i] = t
    return "".join(partes)


def _esc(t):
    return html.escape(t, quote=True)


def _inline_markup(token):
    """Converte os filhos de um token inline em markup Pango."""
    out = []
    pilha = []
    for t in (token.children or []):
        tipo = t.type
        if tipo == "text":
            out.append(_esc(t.content))
        elif tipo == "code_inline":
            out.append("<tt>%s</tt>" % _esc(t.content))
        elif tipo == "softbreak":
            out.append("\n")
        elif tipo == "hardbreak":
            out.append("\n")
        elif tipo == "strong_open":
            out.append("<b>"); pilha.append("</b>")
        elif tipo == "em_open":
            out.append("<i>"); pilha.append("</i>")
        elif tipo == "s_open":
            out.append("<s>"); pilha.append("</s>")
        elif tipo in ("strong_close", "em_close", "s_close"):
            out.append(pilha.pop() if pilha else "")
        elif tipo == "link_open":
            href = t.attrGet("href") or ""
            out.append('<a href="%s">' % _esc(href)); pilha.append("</a>")
        elif tipo == "link_close":
            out.append(pilha.pop() if pilha else "")
        elif tipo == "math_inline":
            if _inline_math_hook:
                out.append(_inline_math_hook(t.content))
            else:
                out.append("<i>%s</i>" % _esc(t.content))
        elif tipo in ("softbreak", "hardbreak"):
            out.append("\n")
        elif tipo == "image":
            out.append(_esc(t.attrGet("alt") or ""))
        elif tipo == "html_inline":
            out.append(_esc(t.content))
    while pilha:
        out.append(pilha.pop())
    texto = "".join(out)
    # Os modelos escrevem TeX solto na prosa, sem $ nem \\(: "a constante
    # \\frac{1}{\\sqrt{2\\pi}} existe...". O parser antigo convertia isso e eu
    # perdi o comportamento ao migrar; sem ele o comando aparece cru.
    if _solto_hook is not None and "\\" in texto:
        try:
            texto = _solto_hook(texto)
        except Exception:
            pass
    return texto


def _partir_display(inline):
    """Quebra um paragrafo em trechos de prosa e formulas de display.

    Devolve pares ("texto"|"math", conteudo) na ordem em que aparecem. Um
    paragrafo sem $$ volta como um unico par de texto, igual a antes.
    """
    filhos = inline.children or []
    if not any(c.type in ("math_inline_double", "math_block") for c in filhos):
        return [("texto", _inline_markup(inline))]

    partes = []
    corrente = []

    def fecha():
        if not corrente:
            return
        falso = _InlineFalso(corrente)
        markup = _inline_markup(falso).strip("\n")
        if markup.strip():
            partes.append(("texto", markup))
        corrente.clear()

    for filho in filhos:
        if filho.type in ("math_inline_double", "math_block"):
            fecha()
            conteudo = (filho.content or "").strip()
            if conteudo:
                partes.append(("math", conteudo))
        elif filho.type in ("softbreak", "hardbreak") and not corrente:
            continue          # quebra colada na formula: nao vira linha vazia
        else:
            corrente.append(filho)
    fecha()
    return partes


class _InlineFalso(object):
    """Token inline sintetico, so com os filhos de um trecho."""

    __slots__ = ("children", "content", "type")

    def __init__(self, filhos):
        self.children = list(filhos)
        self.content = ""
        self.type = "inline"


_TAM_TITULO = {1: "x-large", 2: "large", 3: "large", 4: "medium",
               5: "medium", 6: "medium"}


# Um paragrafo que e SO matematica, sem delimitador, tambem deve virar
# formula. Exigimos comandos de estrutura e ausencia de prosa, para nao
# promover por engano uma frase que mencione uma barra invertida.
_TEX_ESTRUTURA = re.compile(
    # (?![a-zA-Z]) e nao \b: em "\iint_{" o underscore conta como caractere
    # de palavra, entao \b nao casava e a formula passava batido
    r"\\(?:iint|int|sum|prod|frac|sqrt|lim|oint|partial|nabla|left|binom)(?![a-zA-Z])")
# Palavra de prosa: 4+ letras, inicial podendo ser maiuscula. Aceitar a
# maiuscula importa -- "Resposta:" abre frase o tempo todo, e casar so
# minusculas fazia a linha inteira ser lida como formula. Siglas em caixa
# alta (LHS, QED) e nomes de comando seguem de fora.
_PALAVRA = re.compile(r"(?<![\\{])\b[A-Za-z\u00c0-\u00ff][a-z\u00e0-\u00ff]{3,}\b")

# Enfase, titulo ou item de lista sao marcas de texto; uma formula solta
# nunca as carrega, e o renderizador de TeX nao sabe o que fazer com elas.
_MARCA_MD = re.compile(r"\*\*|__|^\s*(?:[-*+]\s|\d+\.\s|#{1,6}\s)", re.M)


def _parece_formula(txt):
    t = (txt or "").strip()
    if not t or "\\" not in t or len(t) > 400:
        return False
    if not _TEX_ESTRUTURA.search(t):
        return False
    if _MARCA_MD.search(t):
        return False
    # remove comandos e chaves; o que sobrar de palavra longa indica prosa
    limpo = re.sub(r"\\[A-Za-z]+", " ", t)
    limpo = re.sub(r"[{}\[\]()^_$&\\]", " ", limpo)
    return len(_PALAVRA.findall(limpo)) == 0


def segmentos(texto):
    """Quebra o markdown em segmentos prontos para virar widget."""
    tokens = _MD.parse(normaliza_delimitadores(texto))
    segs = []
    buf = []

    def descarrega():
        if buf:
            markup = "\n".join(x for x in buf if x is not None)
            if markup.strip():
                segs.append(("texto", markup))
            buf.clear()

    i = 0
    nivel_lista = 0
    contador = []
    while i < len(tokens):
        t = tokens[i]

        if t.type == "math_block":
            descarrega()
            segs.append(("math", t.content.strip()))
        elif t.type == "table_open":
            descarrega()
            j, tabela = _le_tabela(tokens, i)
            segs.append(tabela)
            i = j
        elif t.type == "heading_open":
            nivel = int(t.tag[1])
            corpo = _inline_markup(tokens[i + 1])
            buf.append('<span size="%s" weight="bold">%s</span>'
                       % (_TAM_TITULO.get(nivel, "medium"), corpo))
            i += 2
        elif t.type == "paragraph_open":
            inline = tokens[i + 1]
            bruto = inline.content or ""
            # _parece_formula existe para TeX solto, sem delimitador. Se o
            # parser ja achou $...$ ou $$...$$ aqui, ele entendeu melhor do
            # que a heuristica -- nao passar por cima.
            ja_delimitado = any(
                c.type in ("math_inline", "math_inline_double", "math_block")
                for c in (inline.children or []))
            if not ja_delimitado and _parece_formula(bruto):
                descarrega()
                segs.append(("math", bruto.strip()))
                i += 2
                continue
            # Os modelos escrevem $$...$$ na mesma linha da frase, sem linha
            # em branco antes. O markdown-it entrega isso como um token inline
            # (math_inline_double) e nao como bloco; sem tratar aqui a formula
            # sai crua no meio do texto.
            for tipo_p, corpo_p in _partir_display(inline):
                if tipo_p == "math":
                    descarrega()
                    segs.append(("math", corpo_p))
                elif corpo_p:
                    buf.append(corpo_p)
            i += 2
        elif t.type == "fence" and (t.info or "").strip().lower() in (
                "math", "latex", "tex", "equation"):
            # ```math e ```latex sao formatos comuns de saida dos modelos;
            # sem isto a formula caia como bloco de codigo cru
            descarrega()
            segs.append(("math", t.content.strip()))
        elif t.type == "fence" or t.type == "code_block":
            # blocos de codigo sao tratados fora (CodeBlock), mas se chegarem
            # aqui viram monoespaçado em vez de sumir
            descarrega()
            segs.append(("texto", "<tt>%s</tt>" % _esc(t.content.rstrip("\n"))))
        elif t.type == "bullet_list_open":
            nivel_lista += 1; contador.append(None)
        elif t.type == "ordered_list_open":
            nivel_lista += 1; contador.append(int(t.attrGet("start") or 1))
        elif t.type in ("bullet_list_close", "ordered_list_close"):
            nivel_lista = max(0, nivel_lista - 1)
            if contador:
                contador.pop()
        elif t.type == "list_item_open":
            recuo = "    " * (nivel_lista - 1)
            if contador and contador[-1] is not None:
                marca = "%d." % contador[-1]
                contador[-1] += 1
            else:
                marca = "•"
            # o proximo paragrafo entra na mesma linha do marcador
            if i + 2 < len(tokens) and tokens[i + 1].type == "paragraph_open":
                corpo = _inline_markup(tokens[i + 2])
                buf.append("%s%s %s" % (recuo, marca, corpo))
                i += 3
                while i < len(tokens) and tokens[i].type == "paragraph_close":
                    i += 1
                continue
        elif t.type == "blockquote_open":
            buf.append(None)
        elif t.type == "hr":
            buf.append("<tt>%s</tt>" % ("─" * 28))
        i += 1

    descarrega()
    return segs


def _le_tabela(tokens, i):
    """Le um bloco de tabela e devolve (indice_final, segmento)."""
    cabec, alin, linhas = [], [], []
    atual = None
    em_cabec = False
    j = i
    while j < len(tokens):
        t = tokens[j]
        if t.type == "thead_open":
            em_cabec = True
        elif t.type == "thead_close":
            em_cabec = False
        elif t.type == "tr_open":
            atual = []
        elif t.type == "tr_close":
            if em_cabec:
                cabec = atual or []
            else:
                linhas.append(atual or [])
            atual = None
        elif t.type in ("th_open", "td_open"):
            estilo = t.attrGet("style") or ""
            a = 0.5 if "center" in estilo else (1.0 if "right" in estilo else 0.0)
            if em_cabec:
                alin.append(a)
            corpo = ""
            if j + 1 < len(tokens) and tokens[j + 1].type == "inline":
                corpo = _inline_markup(tokens[j + 1])
            if atual is not None:
                atual.append(corpo)
        elif t.type == "table_close":
            break
        j += 1
    return j, ("tabela", cabec, alin, linhas)
