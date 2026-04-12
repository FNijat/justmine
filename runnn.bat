@echo off

REM Go to your project directory
cd /d "D:\Python projects\bot project"

REM Activate virtual environment (inside the project folder)
call "venv\Scripts\activate"

REM Wait 3 seconds
timeout /t 3 /nobreak >nul

REM Run the script
python tg.py

pause