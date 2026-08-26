#!/bin/bash
# -x to trace, -e so a failed step is visible rather than silently skipped.
set -xe

PLUGIN_DIR="/data/plugins/user_interface/retrotuner-ui"
# Venv lives outside the plugin directory (see install.sh).
VENV_PARENT="/data/retrotuner-ui"

# Uninstall dependendencies
echo "Uninstalling retrotuner-ui Dependencies"

# Make sure the plugin service is stopped before its unit file is removed.
systemctl stop retrotuner-ui.service 2>/dev/null || true
systemctl stop retrotuner-cava.service 2>/dev/null || true
systemctl disable retrotuner-cava.service 2>/dev/null || true

# Remove the apt packages installed by install.sh.
apt-get remove -y pigpio python3-rpi.gpio python3-spidev cava

# Remove the python virtual environment (all python deps live in here)
rm -rf "${VENV_PARENT}"

# Remove the runtime marker/capture files the plugin drops in /tmp
rm -f /tmp/retrotuner-ui-restarting /tmp/retrotuner-ui-capture-on /tmp/retrotuner-ui-capture.json /tmp/retrotuner-ui-capture-baseline.json
rm -f /tmp/retrotuner-audio.fifo /tmp/retrotuner-bars

# Remove service and reload daemons
rm -f /lib/systemd/system/retrotuner-ui.service /lib/systemd/system/retrotuner-cava.service
systemctl daemon-reload -q

# Note: the pigpiod.service tweak made by install.sh lives in a file owned by
# the pigpio package, so removing the package above cleans it up. The
# /etc/udev/rules.d/99-com.rules gpiomem fix is intentionally left in place -
# it corrects a Volumio-wide permission bug that other GPIO plugins rely on.

echo "Done"
echo "pluginuninstallend"
