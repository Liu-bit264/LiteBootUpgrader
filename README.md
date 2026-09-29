# LiteBootUpgrader — LiteBootLoader 上位机

简体中文 | [English](README.en.md)

[LiteBootLoader](https://github.com/Liu-bit264/LiteBootLoader)（固件仓，本地同层目录
`../LiteBootLoader`）的官方上位机：串口（有线）/ 蓝牙（HC-05 SPP）一键升级、OTA 状态
查询、跳转、复位、流程自检与 GUI。
协议流程的**唯一实现**在本仓 `bl_upgrade.py`；帧格式/命令表/状态机等协议规范见
LiteBootLoader 仓库的 `docs/protocol.md`。

## 仓库结构

```text
LiteBootUpgrader/
├── bl_upgrade.py          CLI 与协议库（run_upgrade/ensure_bl/cmd_retry 支持 log/progress 回调）
├── bl_upgrade_gui.py      tkinter 图形界面（有线/蓝牙连接、一键升级/跳转/复位/PING）
├── bl_powerloss_drill.py  验收 #9 参数区写入中断恢复钻具（依赖 bl_upgrade）
├── test_host_protocol.py  主机侧无硬件单测（CRC/帧/解析器/CLI/GUI 状态，32 项）
├── bl_upgrade_gui.bat     GUI 双击启动器
├── build_exe.bat          Windows 可执行程序构建脚本（PyInstaller → dist/）
├── docs/gui.png           GUI 界面截图
├── LICENSE                MIT 许可证
└── dist/                  构建产物（不入库，见 .gitignore）
```

## 快速开始

### 方式 A：可执行程序（免 Python 环境）

双击 `dist\bl_upgrade_gui.exe`（构建方式见下文），或在命令行使用
`dist\bl_upgrade.exe`。单文件、免安装；换机分发只需拷贝 exe。

### 方式 B：源码运行（依赖隔离建议）

> **依赖隔离建议**：Python 依赖建议装在隔离环境（`uv run --with` 或 venv），
> 避免污染全局解释器；wheel 只进包管理器缓存。以下示例按 uv 书写，换成已装
> pyserial 的任意 Python 直跑亦可。

```bash
# 图形界面（或双击 bl_upgrade_gui.bat）
uv run --python 3.12 --with pyserial bl_upgrade_gui.py

# 命令行
uv run --python 3.12 --with pyserial bl_upgrade.py upgrade <镜像.bin> --port COM4
uv run --python 3.12 --with pyserial bl_upgrade.py selftest --port COM4
```

## GUI 使用

**基础模式**（默认）：

![GUI 基础模式](docs/gui.png)

**高级模式**——勾选底部「高级模式」→ 弹窗确认 → 界面重启进入全量功能；取消勾选同样
确认后重启返回。模式记录在 `~/.litebootupgrader_gui.json`：

![GUI 高级模式](docs/gui_adv.png)

- **一键升级**：从任意状态（BL 或 APP）直接升——对端是 APP 时自动走
  "请求回 BL"（SET_META bl_request → 复位 → BL 消费标志），全程进度条 + 日志；
- **连接类型**：串口框内下拉「有线串口 / 蓝牙 HC-05」——蓝牙即配对后系统生成的
  SPP COM 口（模块需先一次性 AT 配置到 115200，见固件仓 `docs/dev/bluetooth_notes.md` §5），
  打开失败自动重试，协议栈与有线完全共用；
- **跳转 APP / 复位 / PING**：单命令操作，自动回读串口横幅进日志窗；
- **镜像栏**：显示补齐 4 字节对齐后的大小与 CRC32（与 VERIFY 期望值同口径）；
- **高级面板（仅高级模式）**：INFO / META / OTA 查询 / ERASE（二次确认）/ SELFTEST /
  VERIFY / LISTEN / RAW / SETMETA，及波特率、pace(ms) 参数——行为与 CLI 逐一对齐；
- **串口互斥**：使用前请关闭 VOFA+ / 串口助手（COM 口独占）。

## CLI 子命令

| 子命令 | 作用 |
|---|---|
| `upgrade <bin>` | 一键升级（ensure_bl → 擦除 → 分块写入 → 校验） |
| `ota` | OTA 状态查询（BL/APP 版本、APP 有效性、到达通道、蓝牙连接） |
| `selftest` | 15 步升级流程硬件在环自检 |
| `ping / info / meta` | 握手 / BL 信息与遥测 / 参数区元数据 |
| `erase / verify <size> <crc>` | 手动分步操作 |
| `jump / reset` | 跳转 / 复位 |
| `setmeta <f> <v> / raw <hex> / listen <秒>` | 元数据 / 原始字节 / 监听 |

通用参数：`--port`（默认 COM4，按实际端口指定；蓝牙 SPP 口同样适用）、`--baud 115200`、
`--pace <ms>`（命令间延时，时序实验用）、`--conn serial|bt`（连接类型，bt=蓝牙 SPP，
打开失败自动重试）。运行 `bl_upgrade.py -h`（`--help`）查看全部子命令与参数；
`--version` 输出版本号。

### GUI 与 CLI 能力矩阵

| 界面 | 覆盖能力 |
|---|---|
| GUI 基础模式（默认） | 一键升级、跳转 APP、复位、PING + 串口枚举/刷新、连接类型（有线/蓝牙）、镜像 CRC32 预览、进度条/日志窗 |
| GUI 高级模式（勾选确认后重启进入） | 在基础模式之上增加 CLI 全量 9 个调试/诊断操作（info / meta / ota / erase / verify / selftest / listen / raw / setmeta）与波特率、pace 参数——**与 CLI 功能同步** |
| CLI | 13 个子命令全量 + `-h/--help`、`--version`、`--conn` |

### 重试与超时约定（对应 LiteBootLoader 仓库 docs/protocol.md §7）

单命令响应超时 1000 ms（ERASE/VERIFY 5000 ms），超时重发 ≤3 次、间隔 2.2 s
（≥ BL 帧内 2000 ms 解析器复位窗口）；收到错误状态码不重试（那是真实答复）。

## 配套与兼容性

| 项 | 值 |
|---|---|
| 支持范围 | 按 LiteBootLoader 升级协议（VER 0x01）工作，**与芯片型号解耦**：任何实现该协议的 LiteBootLoader BL 均可配合；当前已验证组合 = BL 0.3.0 + STM32F103C8T6（全流程）、STM32F411CEU6 最小包（初始查询/升级/跳转/setmeta 回环）；多芯片参数化随固件仓 CSP 扩展 |
| 协议版本 | VER 0x01（LiteBootLoader 仓库 `docs/protocol.md`，0x10 OTA_QUERY 随 BL 0.2.0 起可用） |
| 配套固件 | LiteBootLoader BL 0.3.0+（0.1.0/0.2.0 亦可，仅差 `ota` 查询与蓝牙通道）；**蓝牙连接需固件启用蓝牙通道**（`BL_TRANSPORT_BT_EN=1`，0.3.0 起默认示例配置未启用，见固件仓 porting_guide §3.1） |
| 镜像上限 | 当前按 STM32F103C8T6 分区校验 46 KiB（0xB800），4 字节对齐自动补 0xFF——F411CEU6（448K 分区）升级/跳转已验证但镜像仍受该 46K 上限约束；`--chip` 参数化见固件仓 CSP 路线 |
| 镜像校验 | CRC-32/ISO-HDLC（zlib 兼容）；帧校验 CRC16/MODBUS |
| 串口 | 115200 8N1，Windows COMx / Linux ttyUSBx；蓝牙 = HC-05 配对后的 SPP COM 口（固件仓 bluetooth_notes.md §5 一次性 AT 配置） |

## 测试

```bash
# 主机侧单测（无需硬件，32 项：CRC KAT/帧模板/解析器/响应解析含 OTA/SEQ 与 CMD 错位丢弃/GUI 导入/CLI 解析器/GUI 状态持久化）
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
| 蓝牙口打不开/反复断开 | 确认模块上电、PC 已配对（默认 PIN 1234）；`--conn bt`/「蓝牙 HC-05」已含打开重试，仍失败重插模块 |
| 蓝牙口全部无响应 | 模块数据模式不是 115200：按固件仓 `docs/dev/bluetooth_notes.md` §5 重新 AT 配置 |
| 找不到 COM 口 | 设备管理器确认调试器 CDC 串口；换 USB 线/口（接触不良实测出现过） |
| 全部命令无响应 | 对端可能在跑 APP 或停留在调试器暂停态：用调试器复位后重试 |
| 偶发命令超时后成功 | USB-CDC 抖动触发 BL 帧内 2 s 超时，属正常，自动重发 |
| `verify 失败: CRC_ERROR` | 升级中断导致内容不完整，重新 `upgrade`（整片重擦重写） |

## 版本

当前版本 **v1.3.0**，历史与变更明细见 [CHANGELOG.md](CHANGELOG.md)。

## 参与贡献

反馈问题与提交 PR 的流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

[MIT](LICENSE) © 2026 Qingc。LiteBootLoader 亦采用 MIT 许可证（捆绑的 third_party/CMSIS
保留其原始许可，见其 LICENSES.md）。
