@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  python -m venv .venv
  .venv\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cpu
  .venv\Scripts\pip install -r requirements.txt
)
set HF_HUB_DISABLE_SYMLINKS_WARNING=1
start "" http://127.0.0.1:5055
.venv\Scripts\python.exe app.py
