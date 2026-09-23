@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 正在创建虚拟环境并安装依赖...
  python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
echo 开始建立/续建索引（已完成的会跳过）...
".venv\Scripts\python.exe" phase_finder.py index
pause
