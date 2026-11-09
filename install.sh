#!/bin/sh
# Install pwm-web as a systemd service. Run on the Orange Pi: sudo ./install.sh
set -eu

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo ./install.sh)"; exit 1; }

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/orangepi-pwm

apt-get install -y python3-venv i2c-tools

# Service user with access to /dev/i2c-*
getent group i2c >/dev/null || groupadd --system i2c
id pwm >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin -G i2c pwm
echo 'KERNEL=="i2c-[0-9]*", GROUP="i2c", MODE="0660"' > /etc/udev/rules.d/99-i2c.rules
udevadm control --reload-rules && udevadm trigger --subsystem-match=i2c-dev || true

# App files + virtualenv
mkdir -p "$DEST"
cp -r "$SRC/app" "$SRC/requirements.txt" "$DEST/"
python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"
chown -R root:root "$DEST"

# Config (kept if it already exists)
if [ ! -f /etc/default/pwm-web ]; then
cat > /etc/default/pwm-web <<'EOF'
PWM_I2C_BUS=0
PWM_I2C_ADDR=0x40
PWM_FREQ=50
PWM_PORT=8000
EOF
fi

cp "$SRC/pwm-web.service" /etc/systemd/system/pwm-web.service
systemctl daemon-reload
systemctl enable pwm-web
systemctl restart pwm-web

echo "Installed. Edit /etc/default/pwm-web to set the I2C bus, then: sudo systemctl restart pwm-web"
echo "Logs: journalctl -u pwm-web -f"
