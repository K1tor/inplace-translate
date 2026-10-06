@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0floating.py"
) else (
    start "" python "%~dp0floating.py"
)
