# LiteBootUpgrader — LiteBootLoader 上位机

LiteBootLoader（[主仓](../LiteBootLoader)）的官方上位机：串口一键升级、跳转、复位、
流程自检与 GUI。协议流程的**唯一实现**在本仓 `bl_upgrade.py`；帧格式/命令表/状态机等
协议规范见主仓 `docs/protocol.md`。

## 仓库结构

```text
LiteBootUpgrader/
├── bl_upgrade.py          CLI 与协议库（run_upgrade/ensure_bl/cmd_retry 支持 log/progress 回调）
├── bl_upgrade_gui.py      tkinter 图形界面（一键升级/跳转/复位/PING）
├── bl_powerloss_drill.py  验收 #9 参数区写入中断恢复钻具（依赖 bl_upgrade）
├── test_host_protocol.py  主机侧无硬件单测（CRC/帧/解析器，13 项）
├── bl_upgrade_gui.bat     GUI 双击启动器（uv 隔离依赖）
├── build_exe.bat          Windows 可执行程序构建脚本（PyInstaller → dist/）
├── docs/gui.png           GUI 界面截图
└── dist/                  构建产物（不入库，见 .gitignore）
```

## 快速开始

### 方式 A：可执行程序（免 Python 环境）

双击 `dist\bl_upgrade_gui.exe`（构建方式见下文），或在命令行使用
`dist\bl_upgrade.exe`。单文件、免安装；换机分发只需拷贝 exe。

### 方式 B：源码运行（依赖隔离约定）

本机 Miniforge base 禁止装包，一律 uv 隔离（wheel 只进 uv 缓存）：

```bash
# 图形界面（或双击 bl_upgrade_gui.bat）
uv run --python 3.12 --with pyserial bl_upgrade_gui.py

# 命令行
uv run --python 3.12 --with pyserial bl_upgrade.py upgrade <镜像.bin> --port COM4
uv run --python 3.12 --with pyserial bl_upgrade.py selftest --port COM4
```

## GUI 使用

![GUI](docs/gui.png)

- **一键升级**：从任意状态（BL 或 APP）直接升——对端是 APP 时自动走
  "请求回 BL"（SET_META bl_request → 复位 → BL 消费标志），全程进度条 + 日志；
- **跳转 APP / 复位 / PING**：单命令操作，自动回读串口横幅进日志窗；
- **镜像栏**：显示补齐 4 字节对齐后的大小与 CRC32（与 VERIFY 期望值同口径）；
- **串口互斥**：使用前请关闭 VOFA+ / 串口助手（COM 口独占）。

## CLI 子命令

| 子命令 | 作用 |
|---|---|
| `upgrade <bin>` | 一键升级（ensure_bl → 擦除 → 分块写入 → 校验） |
| `selftest` | 15 步升级流程硬件在环自检 |
| `ping / info / meta` | 握手 / BL 信息与遥测 / 参数区元数据 |
| `erase / write <bin> / verify <size> <crc>` | 手动分步操作 |
| `jump / reset` | 跳转 / 复位 |
| `setmeta <f> <v> / raw <hex> / listen <秒>` | 元数据 / 原始字节 / 监听 |

通用参数：`--port COM4`（默认 COM4）、`--baud 115200`、`--pace <ms>`（命令间延时，时序实验用）。

### 重试与超时约定（与主仓 protocol.md §7 一致）

单命令响应超时 1000 ms（ERASE/VERIFY 5000 ms），超时重发 ≤3 次、间隔 2.2 s
（≥ BL 帧内 2000 ms 解析器复位窗口）；收到错误状态码不重试（那是真实答复）。

## 配套与兼容性

| 项 | 值 |
|---|---|
| 协议版本 | VER 0x01（主仓 protocol.md） |
| 配套固件 | LiteBootLoader BL 0.1.0+（历史版本见主仓 git） |
| 镜像上限 | 46 KiB（0xB800），4 字节对齐自动补 0xFF |
| 镜像校验 | CRC-32/ISO-HDLC（zlib 兼容）；帧校验 CRC16/MODBUS |
| 串口 | 115200 8N1，Windows COMx |

## 测试

```bash
# 主机侧单测（无需硬件，13 项：CRC KAT/帧模板/解析器/响应解析/GUI 导入）
uv run --python 3.12 --with pyserial python test_host_protocol.py

# 硬件在环：15 步升级流程自检（板子在线时）
uv run --python 3.12 --with pyserial bl_upgrade.py selftest --port COM4

# 验收 #9 钻具：写入风暴 + 复位注入（需板子 + 调试器）
uv run --python 3.12 --with pyserial --with pyocd bl_powerloss_drill.py --port COM4 --rounds 10
```

## 构建 Windows 可执行程序

双击 `build_exe.bat`，或命令行：

```bash
uv run --python 3.12 --with pyserial --with pyinstaller \
  pyinstaller --noconfirm --onefile --windowed --name bl_upgrade_gui bl_upgrade_gui.py
uv run --python 3.12 --with pyserial --with pyinstaller \
  pyinstaller --noconfirm --clean --onefile --console --name bl_upgrade bl_upgrade.py
```

产物在 `dist\`（`build/`、`*.spec` 为中间产物，均不入库）。

## 故障排查

| 症状 | 处理 |
|---|---|
| 打开串口失败（拒绝访问） | 关闭占用者：VOFA+ / 串口助手 / 另一个本工具实例 |
| 找不到 COM 口 | 设备管理器确认 DAPLink CDC；换 USB 线/口（接触不良实测出现过） |
| 全部命令无响应 | 对端可能在跑 APP 或停留在调试器暂停态：`pyocd reset` 后重试 |
| 偶发命令超时后成功 | USB-CDC 抖动触发 BL 帧内 2 s 超时，属正常，自动重发 |
| `verify 失败: CRC_ERROR` | 升级中断导致内容不完整，重新 `upgrade`（整片重擦重写） |

## 版本历史

- **1.1.1**（2026-09-26）：新增主机侧单测、PyInstaller 双 exe 构建脚本；修复 GUI
  日志窗 `tk.Text.state()` 误用（那是 ttk 控件 API）导致 UI 刷新链断裂。
- **1.1.0**（2026-09-26）：CLI 库化重构（log/progress 回调）+ tkinter GUI；
  自主仓 LiteBootLoader 迁出独立建仓。
- **≤1.0.0**：见主仓 git 历史（tools/python/）。
