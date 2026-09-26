@echo off
rem 构建 Windows 可执行程序（CLI + GUI）到 dist\
rem 依赖由 uv 隔离拉取（pyinstaller/pyserial 只进 uv 缓存，不污染系统 Python）
cd /d "%~dp0"
echo == [1/2] 构建 CLI（控制台）bl_upgrade.exe ==
uv run --python 3.12 --with pyserial --with pyinstaller ^
  pyinstaller --noconfirm --clean --onefile --console --name bl_upgrade bl_upgrade.py || goto :err
echo == [2/2] 构建 GUI（窗口）bl_upgrade_gui.exe ==
uv run --python 3.12 --with pyserial --with pyinstaller ^
  pyinstaller --noconfirm --onefile --windowed --name bl_upgrade_gui bl_upgrade_gui.py || goto :err
echo ---- 产物（dist\）----
dir dist
exit /b 0
:err
echo [X] 构建失败
exit /b 1
