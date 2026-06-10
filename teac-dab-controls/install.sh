#!/bin/bash
set -e

# Abort loudly if any step fails, so the install does not silently leave a
# broken service behind.
trap 'echo "ERROR: teac-dab-controls install failed at line ${LINENO}" >&2' ERR

# If you need to differentiate install for armhf and i386 you can get the variable like this
#DPKG_ARCH=`dpkg --print-architecture`
# Then use it to differentiate your install

PLUGIN_DIR="/data/plugins/user_interface/teac-dab-controls"
VENV_DIR="${PLUGIN_DIR}/venv"

echo "Installing teac-dab-controls Dependencies"
apt-get update
# System packages: build tooling for the native python wheels, the pigpio
# daemon, and python venv support.
apt-get -y install pigpio python3-dev python3-pip python3-venv

# Create an isolated virtual environment for the plugin so its python
# dependencies never clash with the system / other plugins.
echo "Creating python virtual environment in ${VENV_DIR}"
python3 -m venv "${VENV_DIR}"

# Install the python requirements into the venv.
echo "Installing python requirements into the virtual environment"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r "${PLUGIN_DIR}/requirements.txt"

# use pwm mode for pigpiod
sed -i "/ExecStart=/c\ExecStart=/usr/bin/pigpiod -t 0" /lib/systemd/system/pigpiod.service

# fix issue with 3.569 breaking gpio permisions
# https://community.volumio.com/t/update-to-volumio-3-569-breaks-gpio-permission/64095
sed -i "s/bcm2835-gpiomem/gpiomem/g" /etc/udev/rules.d/99-com.rules

cp "${PLUGIN_DIR}/teac-dab-controls.service" /lib/systemd/system/

systemctl daemon-reload -q

#requred to end the plugin install
echo "plugininstallend"
