#!/bin/bash

# Move to script directory (so it works from anywhere)
cd "$(dirname "$0")"

# Create venv if not exists
if [ ! -d "venv" ]; then
    echo "[*] Creating virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
    echo "[*] Installing dependencies..."
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

echo "[*] Starting ESP32 Debug & Logs Monitor..."
python3 esp32_serial_monitor.py

