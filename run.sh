#!/data/data/com.termux/files/usr/bin/bash
# AutoTrader2.0 - Termux Launcher
# No pip, no packages, just python3.
#
# Usage:
#   bash run.sh          (foreground)
#   bash run.sh &        (background)
#   nohup bash run.sh &  (survives terminal close)

cd "$(dirname "$0")"
python3 main.py "$@"
