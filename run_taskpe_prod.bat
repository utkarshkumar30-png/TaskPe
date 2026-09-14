@echo off
setlocal
title TaskPe - Production WSGI Server
cd /d "%~dp0"
if not exist ".venv\Scripts\waitress-serve.exe" (
  echo Waitress is not installed in .venv.
  echo Install dependencies first: .venv\Scripts\python.exe -m pip install -r requirements.txt
  exit /b 1
)
.venv\Scripts\waitress-serve.exe --listen=127.0.0.1:5000 --threads=4 wsgi:app
