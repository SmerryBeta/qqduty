@echo off
title QQ Duty Bridge (VM duty)
cd /d S:\pyProjects\qqduty
echo Polling VM messages.jsonl, spawning local Qoder CLI for @bot mentions.
echo Close this window to stop the bridge.
E:\python\python.exe -X utf8 qoder_host_bridge.py
echo.
echo Bridge exited. Read the messages above.
pause
