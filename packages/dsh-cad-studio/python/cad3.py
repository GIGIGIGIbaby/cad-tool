#!/usr/bin/env python3
"""Checkout entry point; works without installing the package."""
from pathlib import Path
import sys

_SRC = Path(__file__).resolve().parent / "src"
# When this file is named cad3.py, Python otherwise treats it as the `cad3`
# module and hides the real src/cad3 package during `python cad3.py` or tests.
# Expose the package path before importing the package's CLI.
__path__ = [str(_SRC / "cad3")]
__package__ = "cad3"
__version__ = "3.0.0a1"
sys.path.insert(0, str(_SRC))
from cad3.cli import entrypoint

if __name__ == "__main__":
    entrypoint()
