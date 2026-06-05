#!/bin/bash
# Resize VM 101 RAM to 16GB and restart cleanly
LOG=/var/log/ram-resize.log
echo "=== RAM resize + restart $(date) ===" >> $LOG

# Verify qwen3:32b is done before proceeding
MODEL_CHECK=$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@<server-ip> \
  "curl -s http://localhost:11434/api/tags | grep -c 'qwen3:32b'" 2>/dev/null)

if [ "$MODEL_CHECK" != "1" ]; then
  echo "qwen3:32b not done yet — aborting. Run manually when download completes." >> $LOG
  exit 1
fi

echo "All models confirmed — resizing RAM to 16GB" >> $LOG
qm set 101 --memory 16384 >> $LOG 2>&1

echo "Shutting down VM 101" >> $LOG
qm shutdown 101
sleep 90

echo "Starting VM 101" >> $LOG
qm start 101
echo "Done — VM restarting with 16GB RAM" >> $LOG
