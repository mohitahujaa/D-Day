@echo off
setlocal
cd /d "%~dp0"
title D-Day local service

where python >nul 2>nul || (echo Python 3.11+ is required: https://www.python.org/downloads/ & pause & exit /b 1)

rem Start Ollama if it is not already answering.
curl -s -m 2 http://127.0.0.1:11434/api/tags >nul 2>nul
if errorlevel 1 (
  where ollama >nul 2>nul && (
    echo Starting Ollama...
    start "Ollama" /min ollama serve
    timeout /t 3 /nobreak >nul
  ) || echo WARNING: Ollama not found. Install it from https://ollama.com and run: ollama pull qwen3:1.7b
)

python -c "import flask, requests, dateutil, tzdata" 2>nul || (
  echo Installing Python dependencies...
  python -m pip install -r requirements.txt || (pause & exit /b 1)
)

python server.py
pause
