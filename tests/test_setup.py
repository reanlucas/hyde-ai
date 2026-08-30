#!/usr/bin/env python3
"""Regressao: --setup nunca troca o venv valido por ``python3``."""

import json
import os
import runpy
import tempfile
from pathlib import Path
from types import SimpleNamespace


def main():
    root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(str(root / "bin" / "hyde-ai"),
                               run_name="hyde_ai_setup_test")
    setup = namespace["cmd_setup"]
    globais = setup.__globals__

    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        checkout = work / "hypr-ia"
        python = checkout / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        python.chmod(0o755)
        (checkout / "pyproject.toml").write_text("[project]\nname='test'\n",
                                                  encoding="utf-8")

        config = work / "config.json"
        config.write_text(json.dumps({
            "sidebar": {"edge": "right"},
            "hypria": {"path": str(checkout), "python": "python3"},
        }), encoding="utf-8")
        css = work / "hyde-ai.css"
        css.write_text("ok", encoding="utf-8")

        perguntas = []

        def ask(prompt, default=""):
            perguntas.append(prompt)
            return default

        globais.update({
            "CONFIG_FILE": str(config),
            "WALLBASH_CSS": str(css),
            "WALLBASH_TEMPLATE": str(work / "missing.dcol"),
            "SNIPPET_FILE": str(work / "missing.lua"),
            "_isatty": lambda: True,
            "ask": ask,
            "check_hypria_gateway": lambda executable, cwd: (True, "gateway respondeu"),
        })

        assert setup(SimpleNamespace()) == 0
        written = json.loads(config.read_text(encoding="utf-8"))
        assert written["hypria"]["python"] == str(python)
        assert written["hypria"]["path"] == str(checkout)
        assert written["sidebar"]["edge"] == "right"
        assert perguntas == ["  checkout do hypr-ia"]

    print("ok: setup preserves Hypr-IA venv")


if __name__ == "__main__":
    main()
