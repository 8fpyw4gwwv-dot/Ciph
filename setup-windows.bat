@echo off
cd /d "%~dp0"
call npm install --omit=dev
node src\setup.js
pause
