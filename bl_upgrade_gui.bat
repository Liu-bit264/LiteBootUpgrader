@echo off
rem LiteBootLoader 上位机 GUI 启动器（双击运行；依赖由 uv 隔离，不污染系统 Python）
cd /d "%~dp0"
uv run --python 3.12 --with pyserial bl_upgrade_gui.py
if errorlevel 1 pause
