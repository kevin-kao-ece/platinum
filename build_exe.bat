@echo off
setlocal

REM Build onefile Windows Service EXE (PyInstaller spec)
REM Output: .\dist\melsecBridge.exe

cd /d "%~dp0"

python -m PyInstaller --noconfirm "%CD%\melsecBrider.spec"
if errorlevel 1 (
  echo Build failed.
  exit /b 1
)

echo Build succeeded: "%CD%\dist\melsecBridge.exe"