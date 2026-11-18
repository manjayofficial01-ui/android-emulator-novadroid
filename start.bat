@echo off
REM NovaDroid launcher — double-click to start. Opens http://localhost:8080
cd /d "%~dp0"

REM Find a REAL Python interpreter. A bare `python` can be the Microsoft
REM Store stub (it prints "Python was not found" and exits with an error),
REM so every candidate is validated with an actual `--version` run first.
set "PYEXE="
for %%P in ("python" "py" "%USERPROFILE%\.local\bin\python3.12.exe" "%USERPROFILE%\.local\bin\python3.13.exe") do (
    if not defined PYEXE (
        "%%~P" --version >nul 2>&1 && set "PYEXE=%%~P"
    )
)
REM Last resort: any uv-managed CPython under AppData\Roaming\uv\python.
if not defined PYEXE (
    for /d %%D in ("%APPDATA%\uv\python\cpython-*") do (
        if exist "%%D\python.exe" if not defined PYEXE set "PYEXE=%%D\python.exe"
    )
)
if not defined PYEXE (
    echo No working Python was found. Install one with either:
    echo   uv python install 3.12
    echo   ...or from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Starting NovaDroid with %PYEXE% ...
"%PYEXE%" novadroid.py
pause
