#!/bin/bash

# Uninstall dependendencies
# apt-get remove -y
echo "Uninstalling teac-dab-controls Dependencies"

# Remove the required packages via apt-get
apt-get remove -y pigpio python3-dev

# Remove python modules
pip3 uninstall -y python-engineio==3.14.2 python-socketio[client]==4.6.0 adafruit-blinka Adafruit-PlatformDetect adafruit-python-shell adafruit_circuitpython_mcp3xxx adafruit_circuitpython_bitbangio RPi.GPIO pigpio retrying RpiLCDMenu

# Remove service and reload daemons
rm -f  /lib/systemd/system/teac-dab-controls.service
systemctl daemon-reload -q

echo "Done"
echo "pluginuninstallend"