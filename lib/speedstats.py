"""Historico de velocidade de geracao por modelo.

Duas leituras diferentes, de proposito:

  media(modelo)   -> tokens/s medio das ultimas execucoes. E o numero que
                     acompanha o nome do modelo: "como ele vem rodando".
  o retorno de registrar() -> a medida daquela resposta especifica, mostrada
                     junto do tempo que ela levou.

Contagem: cada delta do streaming e contado como um token. E aproximacao, mas
consistente entre respostas do mesmo provedor, que e o que importa para
comparar. Provedores que informam contagem real podem passar `tokens=`.
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
import time

ARQUIVO = (pathlib.Path(os.environ.get(
    "XDG_STATE_HOME", str(pathlib.Path.home() / ".local/state")))
    / "hyde-ai" / "speed.json")

MAX_AMOSTRAS = 60          # por modelo
MIN_TOKENS = 8             # respostas curtas demais distorcem a media

_lock = threading.Lock()
_cache = None


def _carregar():
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(ARQUIVO) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    _cache = d.setdefault("modelos", {}) and d or {"modelos": {}}
    return _cache


def _gravar():
    try:
        ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
        tmp = ARQUIVO.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(_cache, f, indent=2)
        os.replace(tmp, ARQUIVO)
    except Exception:
        pass


def registrar(modelo, tokens, segundos, provedor=""):
    """Grava uma execucao e devolve a velocidade DELA (tok/s), ou None."""
    if not modelo or segundos <= 0 or tokens < MIN_TOKENS:
        return None
    vel = tokens / segundos
    with _lock:
        d = _carregar()
        lista = d["modelos"].setdefault(modelo, {"provedor": provedor,
                                                 "amostras": []})
        lista["provedor"] = provedor or lista.get("provedor", "")
        lista["amostras"].append({"t": round(tokens),
                                  "s": round(segundos, 3),
                                  "q": time.time()})
        del lista["amostras"][:-MAX_AMOSTRAS]
        _gravar()
    return vel


def media(modelo):
    """Media ponderada por tokens das ultimas execucoes, ou None."""
    if not modelo:
        return None
    with _lock:
        d = _carregar()
        item = d["modelos"].get(modelo)
        if not item or not item.get("amostras"):
            return None
        tot_t = sum(a["t"] for a in item["amostras"])
        tot_s = sum(a["s"] for a in item["amostras"])
    if tot_s <= 0:
        return None
    return tot_t / tot_s


def resumo():
    """Lista (modelo, provedor, media, execucoes) para exibir em /velocidade."""
    with _lock:
        d = _carregar()
        out = []
        for nome, item in d["modelos"].items():
            am = item.get("amostras") or []
            if not am:
                continue
            tt = sum(a["t"] for a in am)
            ts = sum(a["s"] for a in am)
            out.append((nome, item.get("provedor", ""),
                        (tt / ts) if ts > 0 else 0.0, len(am)))
    return sorted(out, key=lambda x: -x[2])


def formatar(vel):
    if not vel:
        return ""
    return "%.0f tok/s" % vel if vel >= 10 else "%.1f tok/s" % vel
