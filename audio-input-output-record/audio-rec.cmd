@echo off
rem audio-rec - Windows entry point (see README.md).
rem   audio-rec.cmd            toggle recording (use this for a hotkey)
rem   audio-rec.cmd ui         the window (no console stays open)
rem   audio-rec.cmd start^|pause^|resume^|stop^|status^|devices  (--help)
rem Uses .venv\ next to this file if it exists, else the "py" launcher.
setlocal
set "HERE=%~dp0"
set "PY=" & set "PYW="
if exist "%HERE%.venv\Scripts\python.exe" (
  set "PY=%HERE%.venv\Scripts\python.exe"
  set "PYW=%HERE%.venv\Scripts\pythonw.exe"
)
if not defined PY (
  where py >nul 2>nul && (set "PY=py" & set "PYW=pyw")
)
if not defined PY (
  where python >nul 2>nul && (set "PY=python" & set "PYW=pythonw")
)
if not defined PY (
  echo audio-rec: Python 3 not found - see README.md, "Windows" 1>&2
  exit /b 1
)
if /i "%~1"=="ui" (
  start "" "%PYW%" "%HERE%audio_rec.py" %*
  exit /b 0
)
"%PY%" "%HERE%audio_rec.py" %*
exit /b %ERRORLEVEL%
