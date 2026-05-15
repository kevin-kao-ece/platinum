@echo off
setlocal

REM Build onefile Windows Service EXE (PyInstaller spec)
REM Output: .\dist\melsecBridge.exe

cd /d "%~dp0"

py -3 -m PyInstaller --noconfirm "%CD%\melsecBridge.spec"
if errorlevel 1 (
  echo Build failed.
  exit /b 1
)

echo Build succeeded: "%CD%\dist\melsecBridge.exe"