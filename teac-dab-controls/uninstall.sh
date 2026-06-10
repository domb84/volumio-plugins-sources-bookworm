#!/bin/bash

PLUGIN_DIR="/data/plugins/user_interface/teac-dab-controls"
# Venv lives outside the plugin directory (see install.sh).
VENV_PARENT="/data/teac-dab-controls"

# Uninstall dependendencies
echo "Uninstalling teac-dab-controls Dependencies"

# Make sure the plugin service is stopped before its unit file is removed.
systemctl stop teac-dab-controls.service 2>/dev/null || true

# Remove the apt packages installed by install.sh. python3-venv is left in
# place: it is part of the base python toolchain and other plugins or system
# tools may depend on it.
apt-get remove -y pigpio python3-dev

# Remove the python virtual environment (all python deps live in here)
rm -rf "${VENV_PARENT}"

# Remove the runtime marker/capture files the plugin drops in /tmp
rm -f /tmp/teac-dab-controls-restarting /tmp/teac-dab-controls-capture-on /tmp/teac-dab-controls-capture.json /tmp/teac-dab-controls-capture-baseline.json

# Remove service and reload daemons
rm -f /lib/systemd/system/teac-dab-controls.service
systemctl daemon-reload -q

# Note: the pigpiod.service tweak made by install.sh lives in a file owned by
# the pigpio package, so removing the package above cleans it up. The
# /etc/udev/rules.d/99-com.rules gpiomem fix is intentionally left in place -
# it corrects a Volumio-wide permission bug that other GPIO plugins rely on.

echo "Done"
echo "pluginuninstallend"
