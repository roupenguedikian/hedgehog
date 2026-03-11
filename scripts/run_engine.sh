#!/bin/bash
# Hedgehog Engine v2 — cron wrapper
# Runs one cycle of collect → EMA → evaluate → execute
# Crontab: * * * * * /home/roup/hedgehog/scripts/run_engine.sh

cd /home/roup/hedgehog
source /home/roup/.bashrc 2>/dev/null
export PATH="/usr/bin:/usr/local/bin:$PATH"
exec /usr/bin/python3 scripts/collect_all.py >> logs/engine.log 2>&1
