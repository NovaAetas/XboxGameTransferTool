@echo off
cd /d "%~dp0"
if exist "%~dp0XboxGamePrepTool.exe" (
    start "" "%~dp0XboxGamePrepTool.exe"
    exit /b 0
)
if exist "%~dp0dist\XboxGamePrepTool\XboxGamePrepTool.exe" (
    start "" "%~dp0dist\XboxGamePrepTool\XboxGamePrepTool.exe"
    exit /b 0
)
echo XboxGamePrepTool.exe was not found.
echo.
echo Expected either:
echo   %~dp0XboxGamePrepTool.exe
echo or:
echo   %~dp0dist\XboxGamePrepTool\XboxGamePrepTool.exe
echo.
pause
exit /b 1
