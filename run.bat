@echo off
chcp 65001 > nul
echo ========================================================
echo  AutoStock - NH나무증권 실시간 MTS 연동 시세포착 자동매매
echo ========================================================
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py
) else (
    python main.py
)
pause
