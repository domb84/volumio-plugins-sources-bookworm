#!/bin/bash

PLUGIN_DIR="/data/plugins/user_interface/teac-dab-controls"
VENV_DIR="${PLUGIN_DIR}/venv"

# Uninstall dependendencies
echo "Uninstalling teac-dab-controls Dependencies"

# Remove the required apt packages
apt-get remove -y pigpio python3-dev

# Remove the python virtual environment (all python deps live in here)
rm -rf "${VENV_DIR}"

# Remove service and reload daemons
rm -f /lib/systemd/system/teac-dab-controls.service
systemctl daemon-reload -q

echo "Done"
echo "pluginuninstallend"
