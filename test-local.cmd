@echo off
title Receipt Sync - Local Test
set "PADDLE_PYTHON=C:\Users\15610\Documents\ChatGPT\DesktopAssistant\.venv-paddle\Scripts\python.exe"
set "TEST_DATA=%~dp0..\pc-local-test"
set "MODEL_CACHE=%LOCALAPPDATA%\ReceiptSync\paddle_models"

if not exist "%PADDLE_PYTHON%" (
  echo PaddleOCR Python was not found:
  echo %PADDLE_PYTHON%
  pause
  exit /b 1
)

echo Local review page: http://127.0.0.1:8764
echo Test data: %TEST_DATA%
echo Keep this window open while testing. Press Ctrl+C to stop.
"%PADDLE_PYTHON%" "%~dp0pc\receipt_sync_server.py" --data-dir "%TEST_DATA%" --model-cache "%MODEL_CACHE%" --host 127.0.0.1 --port 8764 --allow-insecure-http
pause
