@echo off
rem ContentFactory launcher (Windows cmd): cf go "<URL>" --channel <kenh> --open
setlocal
set "CF_ROOT=%~dp0"
set "PYTHONPATH=%CF_ROOT%src;%PYTHONPATH%"
set "PYTHONIOENCODING=utf-8"
if exist "%CF_ROOT%.venv\Scripts\python.exe" (set "CF_PY=%CF_ROOT%.venv\Scripts\python.exe") else (set "CF_PY=python")
"%CF_PY%" -m contentfactory --root "%CF_ROOT%." %*
exit /b %ERRORLEVEL%
