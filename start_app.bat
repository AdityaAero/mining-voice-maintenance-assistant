@echo off
rem Registro de mantencion por voz - debug start: server log in this console, app in a browser tab.
rem Normal use: the "Registro de Mantencion" shortcut on the Desktop (launcher.pyw, no console window).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo No se encuentra .venv. Vea README.md para instalar.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" app\server.py
pause
