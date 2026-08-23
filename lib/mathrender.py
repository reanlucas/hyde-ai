"""Renderizacao de matematica para o hyde-ai.

Usa o mathtext do matplotlib -- um parser TeX completo, o mesmo motor que
tipografa os rotulos do matplotlib. Da tipografia de verdade: fracoes
empilhadas, integrais com limites, raizes, chapeus, binomiais.

Cai para conversao Unicode quando o mathtext nao da conta (ambientes de
matriz, sobretudo), para nunca mostrar TeX cru ao usuario.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import re
import threading

CACHE = pathlib.Path(
    os.environ.get("XDG_CACHE_HOME", str(pathlib.Path.home() / ".cache"))
) / "hyde-ai" / "math"
CACHE.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()
_parser = None
_FontProperties = None


def _init():
    """Importa o matplotlib sob demanda: custa ~200ms e nem toda mensagem tem TeX."""
    global _parser, _FontProperties
    if _parser is not None:
        return True
    with _lock:
        if _parser is not None:
            return True
        try:
            import matplotlib
            matplotlib.use("Agg")
            from matplotlib import mathtext
            from matplotlib.font_manager import FontProperties
            # STIX: padrao de publicacao cientifica (consorcio IEEE/APS/AMS),
            # com italico matematico e espacamento corretos. O dejavusans
            # anterior era uma fonte de interface fazendo matematica.
            # Vem embutido no matplotlib, sem depender de fonte do sistema.
            matplotlib.rcParams["mathtext.fontset"] = "stix"
            matplotlib.rcParams["font.family"] = "STIXGeneral"
            matplotlib.rcParams["mathtext.default"] = "it"
            _parser = mathtext.MathTextParser("agg")
            _FontProperties = FontProperties
            return True
        except Exception:
            return False


# Normalizacoes que o mathtext nao entende mas aparecem o tempo todo
_AMB = re.compile(
    r"\\begin\{(p|b|B|v|V|)matrix\}(.*?)\\end\{\1matrix\}", re.S)
_ALIGN = re.compile(r"\\begin\{(align|aligned|equation|gather)\*?\}(.*?)"
                    r"\\end\{\1\*?\}", re.S)
_DELIMS = {"p": ("(", ")"), "b": ("[", "]"), "B": ("{", "}"),
           "v": ("|", "|"), "V": ("\u2016", "\u2016"), "": ("", "")}


def _achatar_matriz(m):
    """pmatrix -> linhas separadas por ';' entre delimitadores.

    O mathtext nao tem ambientes; isto preserva a leitura sem TeX cru.
    """
    abre, fecha = _DELIMS.get(m.group(1), ("(", ")"))
    corpo = m.group(2).strip()
    linhas = [l.strip() for l in re.split(r"\\\\", corpo) if l.strip()]
    # \; entre colunas: espaco simples e ignorado em modo matematico
    linhas = [r" \; ".join(c.strip() for c in l.split("&")) for l in linhas]
    return r"\left%s %s \right%s" % (
        abre or ".", r" ;\; ".join(linhas), fecha or ".")


# O mathtext nao tem \\underbrace/\\overbrace nem \\underset/\\overset.
# Rebaixar para subscrito/sobrescrito preserva a leitura ("isto vale aquilo")
# em vez de derrubar a formula inteira.
_CHAVES = re.compile(
    r"\\(underbrace|overbrace|underset|overset)\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}"
    r"\s*(?:([_^])\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\})?")


def _rebaixa_chaves(m):
    corpo = m.group(2)
    marca = m.group(3)
    rotulo = m.group(4)
    if not rotulo:
        return "{%s}" % corpo
    posicao = "_" if m.group(1) in ("underbrace", "underset") else "^"
    if marca in ("_", "^"):
        posicao = marca
    return "{%s}%s{%s}" % (corpo, posicao, rotulo)


# Comandos de TAMANHO de delimitador (\\big[ \\Bigl( \\Bigg\\{ ...): o mathtext
# nao os conhece e derruba a formula inteira. O delimitador em si e valido,
# entao basta remover o prefixo de tamanho -- o \\left/\\right ja dimensiona.
_TAM_DELIM = re.compile(r"\\(?:bigg?|Bigg?)[lrm]?(?=\s*[\\(\[\{\)\]\}|.])")

# \\boxed nao existe no mathtext. A caixa marca "esta e a resposta"; sem ela
# o conteudo continua correto, entao preservamos o conteudo e perdemos so o
# enfeite, em vez de perder a formula.
_BOXED = re.compile(r"\\(?:boxed|fbox|framebox)\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}")


def normalizar(tex: str) -> str:
    tex = _ALIGN.sub(lambda m: m.group(2), tex)
    tex = _BOXED.sub(lambda m: "{%s}" % m.group(1), tex)
    tex = _TAM_DELIM.sub("", tex)
    for _ in range(3):                       # aninhamento raso
        novo = _CHAVES.sub(_rebaixa_chaves, tex)
        if novo == tex:
            break
        tex = novo
    tex = _AMB.sub(_achatar_matriz, tex)
    tex = tex.replace(r"\displaystyle", "").replace(r"\textstyle", "")
    tex = re.sub(r"\\(?:label|tag|nonumber)\{[^}]*\}", "", tex)
    tex = re.sub(r"\\\\", r" \\quad ", tex)      # quebras -> espaco
    tex = re.sub(r"\s*&\s*", " ", tex)           # alinhamento
    return tex.strip()


def render(tex: str, cor: str = "#FFFFFF", tamanho: int = 19,
           display: bool = False) -> str | None:
    """Renderiza TeX para PNG transparente. Devolve o caminho, ou None."""
    if not _init():
        return None
    corpo = normalizar(tex)
    if not corpo:
        return None

    chave = hashlib.sha1(
        ("%s|%s|%s|%s" % (corpo, cor, tamanho, display)).encode("utf-8")
    ).hexdigest()[:20]
    destino = CACHE / ("%s.svg" % chave)
    if destino.exists():
        return str(destino)

    dpi = 320 if display else 280          # simbolos mais definidos em HiDPI
    try:
        with _lock:
            _parser.parse("$%s$" % corpo, dpi=dpi,
                          prop=_FontProperties(size=tamanho))
    except Exception:
        return None                        # deixa o chamador cair no Unicode

    try:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib import figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg

        fig = figure.Figure(figsize=(0.01, 0.01), dpi=dpi)
        fig.patch.set_alpha(0.0)
        t = fig.text(0, 0, "$%s$" % corpo, fontsize=tamanho, color=cor)
        canvas = FigureCanvasAgg(fig)
        canvas.draw()
        bbox = t.get_window_extent(renderer=canvas.get_renderer())
        pad = 2
        fig.set_size_inches((bbox.width + 2 * pad) / dpi,
                            (bbox.height + 2 * pad) / dpi)
        t.set_position((pad / (bbox.width + 2 * pad),
                        pad / (bbox.height + 2 * pad)))
        tmp = destino.with_suffix(".tmp.svg")
        fig.savefig(tmp, transparent=True, bbox_inches="tight",
                    pad_inches=0.02, format="svg")
        tmp.replace(destino)
        return str(destino)
    except Exception:
        return None


def limpar_cache(max_arquivos: int = 4000):
    try:
        arqs = sorted(CACHE.glob("*.png"), key=lambda p: p.stat().st_mtime)
        for p in arqs[:-max_arquivos]:
            p.unlink(missing_ok=True)
    except Exception:
        pass


# ---- rasterizacao vetorial ------------------------------------------------
# O Gdk.Texture carrega SVG, mas fixa no tamanho nominal do arquivo. Passando
# pelo Rsvg da para rasterizar na resolucao exata da tela, entao o simbolo sai
# nitido em qualquer corpo e em qualquer fator de escala.

def textura(svg_path, altura_alvo, escala=1.0):
    """Devolve um Gdk.Texture do SVG com `altura_alvo` px logicos."""
    import gi
    gi.require_version("Rsvg", "2.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Rsvg, Gdk, GdkPixbuf, GLib
    import cairo

    handle = Rsvg.Handle.new_from_file(svg_path)
    # API atual: get_dimensions/render_cairo estao obsoletas no librsvg 2.x
    ok, larg, alt = handle.get_intrinsic_size_in_pixels()
    if not ok or alt <= 0:
        return None
    fator = (altura_alvo * escala) / alt
    w = max(1, int(round(larg * fator)))
    h = max(1, int(round(alt * fator)))

    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    ctx = cairo.Context(surf)
    ret = Rsvg.Rectangle()
    ret.x, ret.y, ret.width, ret.height = 0, 0, w, h
    handle.render_document(ctx, ret)
    surf.flush()

    dados = GLib.Bytes.new(bytes(surf.get_data()))
    return Gdk.MemoryTexture.new(
        w, h, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED, dados, surf.get_stride())


# ---- quebra de equacoes longas -------------------------------------------
# Imagem nao quebra linha: uma equacao longa vira uma faixa larga que exige
# rolagem horizontal. O LaTeX resolve com o ambiente align, quebrando nos
# sinais de relacao. Fazemos o mesmo: partimos no "=" de nivel superior e
# devolvemos varias linhas, cada uma renderizada separadamente.

_RELACAO = ("=", r"\leq", r"\geq", r"\le", r"\ge", r"\neq", r"\approx",
            r"\equiv", "<", ">")


def _nivel_zero(tex):
    r"""Posicoes de '=' fora de chaves, colchetes e \left...\right."""
    fora = []
    prof = 0
    i = 0
    while i < len(tex):
        c = tex[i]
        if c == "\\":
            i += 2
            continue
        if c in "{[(":
            prof += 1
        elif c in "}])":
            prof -= 1
        elif c == "=" and prof == 0:
            # ignora >=, <=, != e ==
            ant = tex[i - 1] if i else ""
            prox = tex[i + 1] if i + 1 < len(tex) else ""
            if ant not in "<>!=" and prox != "=":
                fora.append(i)
        i += 1
    return fora


def quebrar(tex, max_chars=52):
    """Divide uma equacao longa em linhas, repetindo o alinhamento no '='.

    Devolve lista de trechos. Curta o bastante, devolve [tex] sem tocar.
    """
    limpo = tex.strip()
    if len(limpo) <= max_chars:
        return [limpo]
    cortes = _nivel_zero(limpo)
    if not cortes:
        return [limpo]

    linhas = []
    ini = 0
    for pos in cortes:
        trecho = limpo[ini:pos].strip()
        if not trecho:
            continue
        # so quebra se o acumulado ja passou do limite
        if len(trecho) >= max_chars * 0.5 or ini == 0:
            linhas.append(trecho if ini == 0 else "= " + trecho)
            ini = pos + 1
    resto = limpo[ini:].strip()
    if resto:
        linhas.append(resto if not linhas else "= " + resto)
    return [l for l in linhas if l] or [limpo]
