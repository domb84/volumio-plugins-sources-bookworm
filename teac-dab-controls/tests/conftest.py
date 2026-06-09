"""Pytest setup: make the plugin importable off-device.

The plugin imports hardware- and Volumio-only modules (RPi.GPIO, pigpio,
spidev, socketio, rpilcdmenu, fastapi, uvicorn, retrying) at import time, none
of which are installable on a normal dev/CI box. We register lightweight stubs
in ``sys.modules`` before any plugin module is imported. The unit tests only
exercise pure logic, so the stubs never need real behaviour.

``setdefault`` is used throughout so that on the actual device — where the real
packages exist — the genuine modules are used instead of the stubs.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Make `import includes.xxx` and `import index` resolve against the plugin root.
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

# Simple top-level dependencies.
for _name in ("pigpio", "spidev", "socketio", "retrying", "fastapi", "uvicorn"):
    sys.modules.setdefault(_name, MagicMock(name=_name))

# RPi.GPIO — both the package and the submodule must be registered for
# `import RPi.GPIO as GPIO` to resolve.
_rpi = sys.modules.setdefault("RPi", MagicMock(name="RPi"))
sys.modules.setdefault("RPi.GPIO", _rpi.GPIO)

# rpilcdmenu + rpilcdmenu.items (`from rpilcdmenu.items import FunctionItem`).
_rpilcd = sys.modules.setdefault("rpilcdmenu", MagicMock(name="rpilcdmenu"))
sys.modules.setdefault("rpilcdmenu.items", _rpilcd.items)
