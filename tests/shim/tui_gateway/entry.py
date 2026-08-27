"""Shim de teste: faz `python -m tui_gateway.entry` (com cwd=tests/shim)
executar o fake_gateway.py com os modos vindos de $FAKE_GATEWAY_ARGS."""
import os
import runpy
import sys

_raiz = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.argv = ["fake_gateway"] + [a for a in os.environ.get("FAKE_GATEWAY_ARGS", "").split() if a]
runpy.run_path(os.path.join(_raiz, "fake_gateway.py"), run_name="__main__")
