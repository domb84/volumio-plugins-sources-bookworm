#!/bin/bash
set -xe

# If you need to differentiate install for armhf and i386 you can get the variable like this
#DPKG_ARCH=`dpkg --print-architecture`
# Then use it to differentiate your install

PLUGIN_DIR="/data/plugins/user_interface/teac-dab-controls"
# Keep the venv OUTSIDE the plugin directory. It is created as root during
# install, and a root-owned subfolder inside the plugin dir prevents Volumio
# (running as the volumio user) from removing the old folder on update, which
# makes the update's `mv` fail with "Directory not empty".
VENV_DIR="/data/teac-dab-controls/venv"

echo "Installing teac-dab-controls Dependencies"
apt-get update
# System packages: the pigpio daemon, headers for building the native python
# wheels (RPi.GPIO/spidev/pigpio), and venv support. python3-pip is not needed:
# the venv bootstraps its own pip via ensurepip (provided by python3-venv).
apt-get -y install pigpio python3-dev python3-venv

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
