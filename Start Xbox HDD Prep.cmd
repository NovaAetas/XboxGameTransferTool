@echo off
cd /d "%~dp0"
"XboxHDDPrep.exe" --no-pause %*
set "exitcode=%ERRORLEVEL%"
echo.
if not "%exitcode%"=="0" echo Xbox HDD Prep finished with errors.
echo Check the text report path shown above for the result and recommended next step.
echo Press any key to close this window.
pause >nul
exit /b %exitcode%
