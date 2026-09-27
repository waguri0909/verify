@echo off
chcp 65001 >nul
cd /d "%~dp0"
:loop
echo ================= 봇 시작 =================
python bot.py
echo.
echo 봇이 종료됨. 5초 후 자동 재시작... (끝내려면 Ctrl+C 연타)
timeout /t 5 >nul
goto loop
