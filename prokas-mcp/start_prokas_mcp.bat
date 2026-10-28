@echo off
rem PROKAS-MCP starten.
rem   start_prokas_mcp.bat          -> MCP-Server (so startet ihn Claude Desktop)
rem   start_prokas_mcp.bat check    -> Selbsttest
rem   start_prokas_mcp.bat validate Z0127
rem   start_prokas_mcp.bat probe    -> Kommandozeile neu pruefen
rem Im Server-Modus darf diese Datei nichts auf den Bildschirm schreiben.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo PROKAS-MCP ist noch nicht eingerichtet. Bitte zuerst setup_prokas_mcp.bat ausfuehren. 1>&2
  exit /b 1
)
if not defined PROKAS_MCP_CONFIG set "PROKAS_MCP_CONFIG=%~dp0config.yaml"
".venv\Scripts\python.exe" -m prokas_mcp %*
