@echo off
rem RotMG Discord Rich Presence - requires Npcap (https://npcap.com) and Python 3.10+
cd /d "%~dp0"
python -c "import scapy" 2>nul || python -m pip install --user -r requirements.txt
python main.py %*
pause
