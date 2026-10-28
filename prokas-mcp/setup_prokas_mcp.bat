@echo off
rem Einrichtung: Python-Umgebung anlegen, Pakete installieren, Selbsttest.
setlocal
cd /d "%~dp0"
echo.
echo === PROKAS-MCP Einrichtung ===
echo.
where uv >nul 2>nul
if errorlevel 1 (
  echo uv wird installiert ...
  powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
)
where uv >nul 2>nul
if errorlevel 1 set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>nul
if errorlevel 1 (
  echo FEHLER: uv konnte nicht installiert werden. Siehe README, Abschnitt Fehlerbehebung.
  pause
  exit /b 1
)
echo Python 3.12 und Pakete werden eingerichtet ...
uv sync --python 3.12 --no-dev
if errorlevel 1 (
  echo FEHLER bei der Installation. Siehe Meldungen oben.
  pause
  exit /b 1
)
echo.
call "%~dp0start_prokas_mcp.bat" check
echo.
echo ---------------------------------------------------------------
echo Diesen Eintrag in claude_desktop_config.json uebernehmen
echo (Claude Desktop: Einstellungen - Entwickler - Konfiguration bearbeiten):
echo ---------------------------------------------------------------
call "%~dp0start_prokas_mcp.bat" config
echo.
pause
