# Tests

Unit tests for the Python side of the plugin. They cover pure logic only — no
hardware, no Volumio, no network — so they run on any machine with Python 3.7+.

## What's covered

| File | Module | Focus |
|------|--------|-------|
| `test_utils.py` | `includes/utils.py` | `parse_button_config` — values, ranges, malformed input |
| `test_index_config.py` | `index.py` | config parsing helpers (`parse_int_field`, `load_button_config`, `load_config`, …) |
| `test_controls.py` | `includes/controls.py` | `normalize_value`, `_lookup_button`, and the capture press-detection state machine |
| `test_volumio.py` | `includes/volumio.py` | URI regexes, pushState dedup, and the `only_if_pending` debounce guard |
| `test_menu_manager.py` | `includes/menu_manager.py` | the restart-marker gate (fresh / absent / stale) |

## How it works

The plugin imports hardware/Volumio-only packages (`RPi.GPIO`, `pigpio`,
`spidev`, `socketio`, `rpilcdmenu`, `fastapi`, `uvicorn`, `retrying`) at import
time. `conftest.py` registers lightweight stubs for these in `sys.modules` and
puts the plugin root on `sys.path`, so the modules import cleanly off-device.
The stubs use `setdefault`, so on the actual device the real packages are used.

Classes that do I/O in `__init__` are constructed with `__new__` in the tests,
and only the attributes a given method needs are set.

## Running

From the plugin directory (`teac-dab-controls/`):

```bash
pip install -r requirements-dev.txt
pytest
```

Or a single file / test:

```bash
pytest tests/test_controls.py
pytest tests/test_volumio.py::TestPushStateDedup
```
