@echo off
cd /d "%~dp0"
python -W ignore::SyntaxWarning app\server.py --demo
pause
