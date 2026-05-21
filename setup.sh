#!/bin/bash
# SolarGuard v3.7.1 - kompletní setup script
# Spusť na čistém RPi (Debian/Raspbian).
#
# Použití:
#   1. rozbal solarguard-v3.7.1-FULL.zip do /home/pi/solarguard/
#   2. cd /home/pi/solarguard/
#   3. chmod +x setup.sh
#   4. ./setup.sh

set -e

echo "============================================"
echo "SolarGuard v3.7.1 - setup"
echo "============================================"

# 1. Kontrola lokace
if [ "$(pwd)" != "/home/pi/solarguard" ]; then
    echo "VAROVANI: tento script ocekava ze jsi v /home/pi/solarguard/"
    echo "  aktualne: $(pwd)"
    read -p "Pokracovat? [y/N]: " yn
    [ "$yn" != "y" ] && exit 1
fi

# 2. System packages
echo ""
echo "[1/5] Instaluji system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-pip

# 3. Virtual env
echo ""
echo "[2/5] Vytvarim Python venv..."
if [ -d ".venv" ]; then
    echo "  .venv jiz existuje, preskakuji"
else
    python3 -m venv .venv
fi

# 4. Python deps
echo ""
echo "[3/5] Instaluji Python zavislosti..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q

# 5. Log directory
echo ""
echo "[4/5] Vytvarim /var/log/solarguard..."
sudo mkdir -p /var/log/solarguard
sudo chown pi:pi /var/log/solarguard

# 6. Systemd service
echo ""
echo "[5/5] Instaluji systemd unit..."
sudo cp solarguard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable solarguard

echo ""
echo "============================================"
echo "Setup HOTOVO!"
echo ""
echo "Dalsi kroky:"
echo "  1. zkontroluj config.yaml (zejmena IP adresy zarizeni)"
echo "     nano config.yaml"
echo "  2. spust SolarGuard:"
echo "     sudo systemctl start solarguard"
echo "  3. sleduj log:"
echo "     sudo journalctl -u solarguard -f"
echo ""
echo "  4. otevri web UI: http://$(hostname -I | awk '{print $1}'):8000"
echo "============================================"
