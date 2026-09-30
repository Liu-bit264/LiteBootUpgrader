# LiteBootUpgrader — LiteBootLoader 上位机

简体中文 | [English](README.en.md)

[LiteBootLoader](https://github.com/Liu-bit264/LiteBootLoader)（固件仓，本地同层目录
`../LiteBootLoader`）的官方上位机：串口（有线）/ 蓝牙（HC-05 SPP）一键升级、OTA 状态
查询、跳转、复位、流程自检与 GUI。

仓库两个入口加四个辅助模块：`bl_upgrade.py` 既是命令行工具也是协议库（升级流程的
**唯一实现**，帧格式/命令表/状态机等协议规范见固件仓 `docs/protocol.md`），
`bl_upgrade_gui.py` 是 tkinter 图形界面（基础 / 高级 / **工厂批量**三种形态，后两者可同时开启）；
`bl_chip.py` 是芯片档案与主机侧芯片探查（消费固件仓 `chips/<id>.json`），`bl_factory.py`
是批量刷写引擎（GUI 工厂模式与 CLI `factory` 子命令共用同一实现）；另有断电恢复验收钻具
`bl_powerloss_drill.py` 与无需硬件的主机侧单测 `test_host_protocol.py`。纯 Python 实现，
运行时仅依赖 pyserial（GUI 另需 tkinter）。

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

**工厂模式**——勾选底部「工厂模式」→ 弹窗确认 → 界面重启进入批量刷写面板：按探查出的
芯片自动取预设镜像、连续烧录、结果落盘可追溯（详见下文「工厂模式（批量烧录）」）：

![GUI 工厂模式](docs/gui_factory.png)

两个模式开关**互相独立、可同时开启**（勾一个不会夹带另一个；工厂模式下还能再进高级模式，
反之亦然）。同时开启时四个面板都在，界面自动加宽并压缩表格/日志行数以容纳内容：

![GUI 工厂模式 + 高级模式](docs/gui_factory_adv.png)

- **一键升级**：从任意状态（BL 或 APP）直接升——对端是 APP 时自动走
  "请求回 BL"（SET_META bl_request → 复位 → BL 消费标志），全程进度条 + 日志；
- **连接类型**：串口框内下拉「有线串口 / 蓝牙 HC-05」——蓝牙即配对后系统生成的
  SPP COM 口（模块需先一次性 AT 配置到 115200，见固件仓 `docs/dev/bluetooth_notes.md` §5），
  打开失败自动重试，协议栈与有线完全共用；
- **跳转 APP / 复位 / PING**：单命令操作，自动回读串口横幅进日志窗；
- **镜像栏**：显示补齐 4 字节对齐后的大小与 CRC32（与 VERIFY 期望值同口径）；
- **高级面板（仅高级模式）**：INFO / META / OTA 查询 / ERASE（二次确认）/ SELFTEST /
  VERIFY / LISTEN / RAW / SETMETA，及波特率、pace(ms) 参数——行为与 CLI 逐一对齐；
- **工厂面板（仅工厂模式）**：芯片与预设镜像表、批量刷写选项与计数、设备结果表；
  开工/停止共享 `busy` 互斥，与高级操作不会同时占串口；
- **串口互斥**：使用前请关闭 VOFA+ / 串口助手（COM 口独占）。

> 工厂/高级的隔离是**能力隔离、资源互斥**：工厂侧不读高级面板的任何变量（波特率、
> pace、签名私钥都用工厂面板自己的值——避免高级模式里残留的私钥被批次流程悄悄接管），
> 高级侧也不改工厂的预设、触发方式、计数与记录文件。单测里有逐项断言。

## CLI 子命令

| 子命令 | 作用 |
|---|---|
| `upgrade <bin>` | 一键升级（ensure_bl → 擦除 → 分块写入 → 校验；`--key <pem>` 启用签名校验） |
| `keygen` | 生成 P-256 测试密钥对（私钥 PEM + 公钥本地头；密钥任何形态不入库） |
| `chips list \| sync \| detect` | 芯片档案：列出（含来源） / 从固件仓重建内置档案包 / 连板探查芯片 |
| `factory` | 工厂批量刷写（无人值守；与 GUI 工厂模式同一引擎，`--help` 看选项） |
| `ota` | OTA 状态查询（BL/APP 版本、APP 有效性、到达通道、蓝牙连接） |
| `selftest` | 15 步升级流程硬件在环自检（`--chip` 后按档案取 APP 分区大小） |
| `ping / info / meta` | 握手 / BL 信息与遥测 / 参数区元数据 |
| `erase / verify <size> <crc32_hex>` | 手动分步操作 |
| `jump / reset` | 跳转 / 复位 |
| `setmeta <f> <v> / raw <hex> / listen <秒>` | 元数据 / 原始字节 / 监听 |

通用参数：`--port`（默认 COM4，按实际端口指定；蓝牙 SPP 口同样适用）、`--baud 115200`、
`--pace <ms>`（命令间延时，时序实验用）、`--conn serial|bt`（连接类型，bt=蓝牙 SPP，
打开失败自动重试）、`--chip <id|auto>`（按芯片档案驱动 APP 分区大小与镜像体检；
`auto` = 先确保进入 BL 再按 GET_INFO 探查）、`--profiles <文件>` / `--firmware-root <目录>`
（档案来源）、`--no-probe` / `--force-image`（探查与镜像体检的手动兜底）。
运行 `bl_upgrade.py -h`（`--help`）查看全部子命令与参数；`--version` 输出版本号。

### 签名验签升级（1.4.0 起，配套固件 BL 0.4.0 ADR-020）

```bash
# 1) 生成测试密钥对（私钥 PEM 本地保管；公钥头写入固件仓芯片端口目录，须在 .gitignore）
uv run --python 3.12 --with pyserial --with cryptography bl_upgrade.py keygen \
    --out-key sign_test_key.pem \
    --out-header ../LiteBootLoader/port/stm32f4/f411ceu6/bl_sign_pubkey_local.h
# 2) 固件侧 BL_SIGN_EN=1 重建（编译日志出现签名启用警告行属预期）
# 3) 签名升级：写块完成后改发 0x11 VERIFY_SIGNED，auth 置位方可跳转
uv run --python 3.12 --with pyserial --with cryptography bl_upgrade.py \
    upgrade app.bin --port COM4 --key sign_test_key.pem
```

篡改镜像任意字节 → 固件回 `SIGN_ERROR`、不持久化、拒绝跳转；不带 `--key` 走 legacy
VERIFY 的镜像在启用签名的固件上 auth=0 同样不可跳转。防物理/调试口攻击与回滚不在
范围（ADR-020 威胁模型）。

> 验证状态：主机侧单测通过（keygen 产物格式、签名→公钥回验、篡改失效、0x11 帧结构
> LEN=72）；**板级 HIL 未跑**——固件仓待办见其 `docs/dev/test_plan.md` §5.1。

## 工厂模式（批量烧录，1.5.0 起）

产线批量刷写：**插板即烧、按探查出的芯片自动取预设镜像、结果落盘可追溯**。
GUI（勾选「工厂模式」）与 CLI（`factory` 子命令）是同一引擎的两个前端。

### 配置：档案、预设镜像、记录

- **芯片档案**来源按序合并（先到先得、后者补缺，`chips list` 会打印每片的来源）：
  `--profiles` 显式文件 → `factory/local.json` 的 `chips` 覆盖 → 兄弟仓
  `../LiteBootLoader/chips/*.json`（开发机实时事实源）→ 内置 `bl_chip_profiles.json`
  （入库生成物，`chips sync` 从固件仓重建，保证单文件 exe 无固件仓也能用）。
- **预设镜像与记录**写在 `factory/local.json`（`factory/local.example.json` 是带字段说明的
  示例，复制改名即可；该文件不入库），GUI 里也能直接改（预设表双击指定镜像、记录路径可浏览，
  开工时自动保存到配置）。
- **没配预设镜像的芯片会停批并提示**——工厂按芯片取镜像，绝不猜。

### 芯片探查

BL 协议没有芯片标识字段：`GET_INFO` 只回 Flash 容量与 96 位 UID（UID 是**每片序列号**，
不是型号），所以按 **Flash 容量指纹**命中（F103C8T6 = 64 KiB，F411CEU6 = 512 KiB）。

容量撞车时用 `VERIFY` 做**只读**边界探查消歧：固件先判 `size > BL_APP_SIZE` 回
`RANGE_ERROR`、通过才算 CRC，因此「候选 APP 区大小 + 垃圾 CRC」一次往返即可判定该候选
装不装得下，全程不写 Flash（代价：2⁻³² 概率垃圾 CRC 恰好命中，会持久化一次元数据；
该设备随即整片重刷，实际无影响）。命令行可 `--no-probe` 关掉探查、改为如实报歧义。

未收录的容量、或对端进不了 BL —— 该台判失败并停下，**不会盲烧**。已知局限：容量与
APP 分区都相同的两片芯片无法仅凭协议区分，需要人工 `--chip <id>` 指定（更彻底的方案是
固件侧加芯片标识字段，或走调试口读 `DBGMCU_IDCODE`——都不在本次范围）。

### 镜像体检（防烧错）

开工前对每片芯片的预设镜像做体检：大小 ≤ 该芯片 APP 分区、自动补齐 4 字节对齐、
**向量表体检**（首字初始 MSP 落在该芯片 SRAM 区间、次字 Reset Handler 带 Thumb 位且落在
该芯片 APP 区间）。这挡住两类真实事故：烧了**别家芯片**的镜像、把 **BootLoader 镜像**
塞进 APP 区。体检不通过不烧；应急可用 `--force-image` 放行（日志会写明跳过了什么）。
镜像 SHA-256 与 CRC32 一并记入结果文件，便于追溯。

### 换板触发（两种，界面/命令行可切换）

| 模式 | 适用夹具 | 「下一块」判定 |
|---|---|---|
| 同端口轮询（默认） | 一个 USB 转换器接所有板，COM 口常驻，拔插的是板子 | PING 有应答且 **UID 变化**；或失联（连续无应答）后重现 |
| 新串口出现 | 每板一个转换器 / 板载 CDC，插板生成新 COM 口 | 端口集合差分；`--port` 此时是**白名单**，其余新口忽略（防误刷无关 CDC） |

### 跳过与续烧

- **跳过已是最新**（默认开）：设备 APP 有效且 CRC32 等于目标镜像 → 只记录不写入；
- **断点续烧**（`--resume`）：按记录文件里 `result=OK` 的 UID 跳过——换机/断电后接着干；
- **同板复烧**：本会话已烧过的 UID 会醒目提示并跳过（防误操作重复烧）。

### 停止

点「停止」= 软停（烧完当前台即停）；再点一次 = 立即停，打断进行中的升级
（时延上限 = 当前命令超时 ≤ 5 s，串口读不可中断）。被中断的那台判失败并在记录里写明
已写入字节数；重新开工整片重刷即可（ERASE/WRITE/VERIFY 全程幂等）。

### 记录字段

一行一台：序号 / 时间 / 端口 / 芯片 id / 器件名 / UID / 镜像路径 / 镜像大小 / CRC32 /
SHA-256 / 结果（OK·SKIP·FAIL） / 失败阶段 / 说明 / 耗时 / BL 版本。
CSV 以 `utf-8-sig` 追加写入（Excel 直接打开），可另加 JSONL 供脚本消费。

### CLI 等价用法

```bash
# 单端口批量：烧满 5 台、跳过已是最新、刷完跳转 APP、结果落盘
uv run --python 3.12 --with pyserial bl_upgrade.py factory \
    --port COM4 --chip auto \
    --image f103c8t6=../LiteBootLoader/app/examples/f103c8t6_app/app.bin \
    --count 5 --skip-uptodate --auto-jump --records factory/records/records.csv --yes

# 每板一个转换器的夹具：新串口出现即开工（--port 作白名单），F411 目标镜像
uv run --python 3.12 --with pyserial bl_upgrade.py factory \
    --port COM7 --trigger newport --resume \
    --image f411ceu6=D:/release/f411ceu6_app.bin --yes
```

> 验证状态：主机侧单测 117 项全绿（含模式矩阵、探查消歧、镜像体检、批量状态机、
> 落盘/续烧/停止）；**F103C8T6 板级实测通过**——自动探查命中、单台批量烧录（约 3.5 s/台，
> CRC 校验 + 跳转 APP）、跳过已是最新（真实 CRC32 一致）、断点续烧（真实 UID）、
> 升级中立即停止（中断判 FAIL，设备 `app_valid=0` 拒绝运行不完整 APP）。
> **F411CEU6 板级未跑**（无板），其档案与 `--chip` 路径仅主机侧单测覆盖。

### GUI 与 CLI 能力矩阵
| 界面 | 覆盖能力 |
|---|---|
| GUI 基础模式（默认） | 一键升级、跳转 APP、复位、PING + 串口枚举/刷新、连接类型（有线/蓝牙）、镜像 CRC32 预览、进度条/日志窗 |
| GUI 高级模式（勾选确认后重启进入） | 在基础模式之上增加 CLI 全量 10 个调试/诊断操作（info / meta / ota / erase / verify / selftest / listen / raw / setmeta / **keygen**）与波特率、pace 参数，升级支持签名私钥（可选）——**与 CLI 功能同步** |
| GUI 工厂模式（勾选确认后重启进入） | 芯片与预设镜像表、批量刷写（触发方式/自动开工/跳过已是最新/断点续烧/刷完跳转/APP 版本/签名私钥/记录文件）、开工·停止、实时计数与合格率、设备结果表——**与 CLI `factory` 同一引擎**；可与高级模式同时开启 |
| CLI | 16 个子命令全量 + `-h/--help`、`--version`、`--conn`、`--key`、`--chip`、`--profiles` |

### 重试与超时（对应 LiteBootLoader 仓库 docs/protocol.md §6）

`upgrade` 全流程自动重试：单命令响应超时（探测 1 s、擦除/校验 5 s、分块写入 2 s）后重发，
至多 3 次尝试（首发 1 + 重发 ≤2）、间隔 2.2 s（≥ BL 帧内 2000 ms 解析器复位窗口）；
收到错误状态码不重试（那是真实答复）。**手工单命令不自动重发**，超时上限分别为
`ping/info/meta/setmeta` 1 s、`ota/jump/reset` 2 s、`verify` 3 s、`erase` 8 s。

## 构建 Windows 可执行程序

双击 `build_exe.bat`，或命令行：

```bash
uv run --python 3.12 --with pyserial --with pyinstaller \
  pyinstaller --noconfirm --onefile --windowed --name bl_upgrade_gui bl_upgrade_gui.py
uv run --python 3.12 --with pyserial --with pyinstaller \
  pyinstaller --noconfirm --clean --onefile --console --name bl_upgrade bl_upgrade.py
```

产物在 `dist\`（`build/`、`*.spec` 为中间产物，均不入库）。

## 配套与兼容性

| 项 | 值 |
|---|---|
| 支持范围 | 按 LiteBootLoader 升级协议（VER 0x01）工作，**与芯片型号解耦**：任何实现该协议的 LiteBootLoader BL 均可配合；已验证组合 = BL 0.3.0 + STM32F103C8T6（全流程 + 工厂批量）、STM32F411CEU6 最小包（初始查询/升级/跳转/setmeta 回环）；芯片几何按固件仓 `chips/<id>.json` 档案驱动（现含 F103C8T6 / F411CEU6） |
| 协议版本 | VER 0x01（LiteBootLoader 仓库 `docs/protocol.md`，0x10 OTA_QUERY 随 BL 0.2.0 起可用）——1.5.0 工厂模式**未改协议、未改固件** |
| 配套固件 | LiteBootLoader BL 0.3.0+（0.1.0/0.2.0 亦可，仅差 `ota` 查询与蓝牙通道）；**蓝牙连接需固件启用蓝牙通道**（`BL_TRANSPORT_BT_EN=1`，0.3.0 起默认示例配置未启用，见固件仓 porting_guide §3.1）；**签名升级（`--key`）另需 BL 0.4.0 以上 + 该支持包 `BL_SIGN_EN=1`**（见上「签名验签升级」，现仅 F411 支持） |
| 镜像上限 | 按芯片档案校验：F103C8T6 46 KiB（0xB800）、F411CEU6 448 KiB（0x70000），4 字节对齐自动补 0xFF；`--chip <id>` 或工厂模式的自动探查会选对分区（不再硬编码 46K） |
| F411 已知限制 | 已随 1.5.0 `--chip` 参数化解决：`selftest`「越界防护」步按档案取 APP 分区末尾（不再假 FAIL），`bl_powerloss_drill.py` 的 pyocd 目标名/pack 路径随 `--chip` 走——**但 F411 侧这些改动只有主机侧单测覆盖，板级未跑** |
| 镜像校验 | CRC-32/ISO-HDLC（zlib 兼容）；帧校验 CRC16/MODBUS；工厂模式另做向量表体检（MSP/Reset Handler） |
| 串口 | 115200 8N1，Windows COMx / Linux ttyUSBx；蓝牙 = HC-05 配对后的 SPP COM 口（固件仓 bluetooth_notes.md §5 一次性 AT 配置） |

## 故障排查

| 症状 | 处理 |
|---|---|
| 打开串口失败（拒绝访问） | 关闭占用者：VOFA+ / 串口助手 / 另一个本工具实例 |
| 蓝牙口打不开/反复断开 | 确认模块上电、PC 已配对（默认 PIN 1234）；`--conn bt`/「蓝牙 HC-05」已含打开重试，仍失败重插模块 |
| 蓝牙口全部无响应 | 模块数据模式不是 115200：按固件仓 `docs/dev/bluetooth_notes.md` §5 重新 AT 配置 |
| 找不到 COM 口 | 设备管理器确认调试器 CDC 串口；换 USB 线/口（接触不良实测出现过） |
| 全部命令无响应 | 对端可能在跑 APP 或停留在调试器暂停态：用调试器复位后重试 |
| 偶发命令超时后成功 | USB-CDC 抖动触发 BL 帧内 2 s 超时，属正常（`upgrade` 会自动重发，手工单命令重跑一次即可） |
| `verify 失败: CRC_ERROR` | 升级中断导致内容不完整，重新 `upgrade`（整片重擦重写） |
| 工厂模式：等待设备/换板一直不动 | 确认触发方式与夹具匹配（共用转换器选「同端口轮询」；每板一个转换器选「新串口出现」，且 `--port`/串口框要填**该板会出现的那个口名**作白名单） |
| 工厂模式：`Flash xxx KiB 无匹配档案` | 该容量未收录：`chips list` 看已有档案，用 `--chip <id>` 人工指定，或补 `chips/<id>.json` 后 `chips sync` |
| 工厂模式：`镜像体检不通过` | 镜像与本芯片不符（Reset Handler/MSP 不在该芯片分区）：确认烧的是该芯片的 APP 镜像、不是 BootLoader 或别家芯片的；确需强烧用 `--force-image`（GUI 无此开关，走 CLI） |

## 版本与许可

当前版本 **v1.5.0**，历史与变更明细见 [CHANGELOG.md](CHANGELOG.md)。

[MIT](LICENSE) © 2026 Qingc。LiteBootLoader 亦采用 MIT 许可证（捆绑的 third_party/CMSIS
保留其原始许可，见其 LICENSES.md）。

## 参与贡献

反馈问题与提交 PR 的流程、开发环境、测试与验收钻具命令见 [CONTRIBUTING.md](CONTRIBUTING.md)。
