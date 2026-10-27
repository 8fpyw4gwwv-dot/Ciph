@echo off
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
  echo Please right-click this file and choose "Run as administrator".
  pause
  exit /b 1
)
call npm install --omit=dev
node src\setup.js --remote
pause
