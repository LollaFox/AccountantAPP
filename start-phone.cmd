@echo off
title Receipt Sync Phone
cd /d "%~dp0"
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 windows\phone_app.py
) else (
  python windows\phone_app.py
)
if errorlevel 1 pause
