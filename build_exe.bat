@echo off
chcp 65001 >nul
rem 构建 Windows 可执行程序（CLI + GUI）到 dist\ 目录
rem 依赖由 uv 隔离拉取（pyinstaller/pyserial 只进 uv 缓存，不污染系统 Python）
rem 注意：bl_chip_profiles.json 是运行期读的数据文件（不是 import），必须 --add-data 打进包内；
rem 否则单文件 exe 拿不到内置芯片档案包（换机/无固件仓时会报「未发现芯片档案」）
cd /d "%~dp0"
echo == [1/2] 构建 CLI（控制台）bl_upgrade.exe ==
uv run --python 3.12 --with pyserial --with pyinstaller ^
  pyinstaller --noconfirm --clean --onefile --console --name bl_upgrade ^
  --add-data "bl_chip_profiles.json;." bl_upgrade.py || goto :err
echo == [2/2] 构建 GUI（窗口）bl_upgrade_gui.exe ==
uv run --python 3.12 --with pyserial --with pyinstaller ^
  pyinstaller --noconfirm --onefile --windowed --name bl_upgrade_gui ^
  --add-data "bl_chip_profiles.json;." bl_upgrade_gui.py || goto :err
echo ---- 产物（dist\）----
dir dist
exit /b 0
:err
echo [X] 构建失败
exit /b 1
