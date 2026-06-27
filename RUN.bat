@echo off
REM Tella run wrapper (Windows). Activates .venv then dispatches to the CLI.
REM ASCII-only. Keeps the window open at the end so a double-click user can
REM read the result (success path or error traceback) before it closes.

setlocal

if not exist .venv (
  echo .venv not found. Run SETUP.bat first.
  pause
  exit /b 1
)

call .venv\Scripts\activate.bat

REM Force UTF-8 stdout for Vietnamese diacritics on Windows
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM No args -> interactive wizard. Any args -> pass straight through to the CLI.
if "%~1"=="" (
  python -m tella
) else (
  python -m tella %*
)

set EXITCODE=%ERRORLEVEL%

echo.
if "%EXITCODE%"=="0" (
  echo === Done. Your video is in the out\ folder. ===
) else (
  echo === Tella exited with an error ^(code %EXITCODE%^). Read the messages above. ===
)
echo.
pause

endlocal
exit /b %EXITCODE%
