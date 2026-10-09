"""Test bootstrap for the mcbot_bridge plugin.

The repository directory is named ``n.e.k.o_plugin_mcbot_bridge`` (the market
naming convention), which is not a valid Python identifier — so the plugin
package cannot simply be imported by its folder name.

Rather than executing ``__init__.py`` (which needs the N.E.K.O SDK), this
registers a lightweight package stub whose ``__path__`` points at the repository
root. Submodules then import normally as ``mcbot_bridge.service`` etc., and the
relative imports inside them (``from . import prompts``) resolve against the
stub. That keeps these tests runnable without a host installation — the only
third-party requirement is ``httpx``, which ``client.py`` imports.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

if "mcbot_bridge" not in sys.modules:
    _pkg = types.ModuleType("mcbot_bridge")
    _pkg.__path__ = [str(_ROOT)]  # type: ignore[attr-defined]
    sys.modules["mcbot_bridge"] = _pkg