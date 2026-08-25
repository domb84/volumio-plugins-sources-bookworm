#!/bin/bash
set -xe

# If you need to differentiate install for armhf and i386 you can get the variable like this
#DPKG_ARCH=`dpkg --print-architecture`
# Then use it to differentiate your install

PLUGIN_DIR="/data/plugins/user_interface/retrotuner-ui"
# Keep the venv OUTSIDE the plugin directory. It is created as root during
# install, and a root-owned subfolder inside the plugin dir prevents Volumio
# (running as the volumio user) from removing the old folder on update, which
# makes the update's `mv` fail with "Directory not empty".
VENV_DIR="/data/retrotuner-ui/venv"

echo "Installing retrotuner-ui Dependencies"
apt-get update

# RPi.GPIO and spidev are the only requirements.txt packages that need
# compiling (everything else is pure Python) -- getting them prebuilt via apt
# instead avoids needing python3-dev at all. That matters because
# python3-dev/python3-venv are version-locked (`=`) to the exact python3.11
# build already on this image, so requesting them drags apt into upgrading
# python3.11/libc6/locales to match -- which previously triggered a mass
# service restart (needrestart, or the classic libc6 postinst prompt) that
# broke playback and crash-looped upmpdcli. python3-rpi.gpio/python3-spidev
# are ordinary application packages, so they depend on python3 by a normal
# range (any 3.11.x) rather than an exact pin, and don't drag in that upgrade.
apt-get -y --no-install-recommends install pigpio python3-rpi.gpio python3-spidev

# Create an isolated virtual environment for the plugin's remaining pure-
# Python dependencies, so they never clash with the system / other plugins.
# --system-site-packages lets it see the apt-installed RPi.GPIO/spidev above,
# so nothing needs compiling inside the venv.
#
# Uses virtualenv (as a standalone zipapp, needing no install of its own)
# rather than the stdlib `venv` module specifically to avoid the python3-venv
# package -- same exact-version-lock problem as python3-dev above. virtualenv
# also bundles its own pip, so it doesn't need python3-venv's ensurepip either.
echo "Creating python virtual environment in ${VENV_DIR}"
VIRTUALENV_PYZ="/tmp/virtualenv.pyz"
wget -qO "${VIRTUALENV_PYZ}" https://bootstrap.pypa.io/virtualenv.pyz
python3 "${VIRTUALENV_PYZ}" --system-site-packages "${VENV_DIR}"
rm -f "${VIRTUALENV_PYZ}"

# Install the python requirements into the venv.
echo "Installing python requirements into the virtual environment"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r "${PLUGIN_DIR}/requirements.txt"

# use pwm mode for pigpiod
sed -i "/ExecStart=/c\ExecStart=/usr/bin/pigpiod -t 0" /lib/systemd/system/pigpiod.service

# fix issue with 3.569 breaking gpio permisions
# https://community.volumio.com/t/update-to-volumio-3-569-breaks-gpio-permission/64095
sed -i "s/bcm2835-gpiomem/gpiomem/g" /etc/udev/rules.d/99-com.rules

cp "${PLUGIN_DIR}/retrotuner-ui.service" /lib/systemd/system/

systemctl daemon-reload -q

#requred to end the plugin install
echo "plugininstallend"
