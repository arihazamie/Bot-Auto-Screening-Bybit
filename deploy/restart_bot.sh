#!/bin/bash
sudo systemctl restart bybit_bot
echo "Bot Restarted at $(date)" >> /var/log/bot_restart.log