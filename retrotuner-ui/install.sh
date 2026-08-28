#!/bin/bash
set -xe

PLUGIN_DIR="/data/plugins/user_interface/retrotuner-ui"
# Keep the venv outside the plugin dir, or plugin updates fail - see NOTES.md ("Install").
VENV_DIR="/data/retrotuner-ui/venv"

echo "Installing retrotuner-ui Dependencies"
apt-get update

# Prebuilt GPIO packages keep python3-dev out of the install, and cava must always
# be running to drain the tap fifo. Both matter - see NOTES.md ("Install").
apt-get -y --no-install-recommends install pigpio python3-rpi.gpio python3-spidev cava

# --system-site-packages picks up the apt GPIO packages; virtualenv-as-zipapp avoids python3-venv.
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
cp "${PLUGIN_DIR}/retrotuner-cava.service" /lib/systemd/system/

systemctl daemon-reload -q

#requred to end the plugin install
echo "plugininstallend"
