# 更新日志（Changelog）

本项目的所有显著变更记录于此。格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer 2.0.0](https://semver.org/lang/zh-CN/)。

## [1.5.0] - 2026-09-30

工厂模式（批量刷写）+ 芯片档案层。**协议 VER 0x01 不变、固件无需改动**（纯主机侧特性）。

### Added

- **工厂模式（GUI，勾选确认后窗口重启进入）**：芯片与预设镜像表、批量刷写选项
  （换板触发方式 / 自动开工 / 跳过已是最新 / 断点续烧 / 刷完跳转 APP / 写 APP 版本 /
  签名私钥 / 记录文件）、开工·停止、实时计数与合格率、设备结果表。与「高级模式」
  **互相独立、可同时开启**——两个开关各自持久化在 `~/.litebootupgrader_gui.json` 的
  `advanced` / `factory` 键，勾一个不会夹带另一个，工厂模式下仍可再进高级模式（反之亦然）；
  工厂侧不读高级面板的任何变量（波特率/pace/签名私钥用工厂面板自己的值）。
- **`bl_factory.py` 批量引擎**（GUI 工厂面板与 CLI `factory` 共用）：状态机
  等设备→探查→体检→（跳过判定）→擦写校验→收尾→记录→等换板；两种换板触发——
  同端口轮询（适配器共用的夹具，靠 PING 应答 + **UID 变化**或失联后重现判定），
  新串口出现（每板一个转换器，端口集合差分且 `--port` 作白名单，防误刷无关 CDC）；
  结果落盘 CSV（`utf-8-sig`，Excel 直接打开）+ 可选 JSONL；断点续烧（按既有记录里
  `result=OK` 的 UID 跳过）；软停（烧完当前台）与立即停（打断进行中的升级）。
- **`bl_chip.py` 芯片档案与主机侧探查**：档案按 `--profiles` → `factory/local.json`
  → 兄弟仓 `chips/*.json` → 内置 `bl_chip_profiles.json` 顺序合并（**不新造 schema**，
  直接读固件仓键；先到先得、后者补缺，几何不一致会提示）；加载即做几何自检
  （APP 区不得越出 Flash、4 字节对齐等），非法档案直接拒绝。
  探查按 **Flash 容量指纹**命中，容量撞车时用 `VERIFY` 做**只读**边界探查消歧
  （固件先判 `size > BL_APP_SIZE` 回 `RANGE_ERROR`、通过才算 CRC，故零写入）；
  未收录容量或对端进不了 BL 一律停下，不盲烧。
- **镜像体检（防烧错）**：大小 ≤ 该芯片 APP 分区、4 字节补齐、**向量表体检**
  （初始 MSP 落在芯片 SRAM 区间、Reset Handler 带 Thumb 位且落在 APP 区间）——
  挡住「烧了别家芯片的镜像」与「把 BootLoader 镜像塞进 APP 区」；`--force-image`
  应急放行并在日志写明跳过了什么。镜像 SHA-256/CRC32 记入结果文件。
- **CLI 新面**：`chips list|sync|detect` 与 `factory` 两个子命令；全局 `--chip <id|auto>`、
  `--profiles`、`--firmware-root`、`--no-probe`、`--force-image`，以及 factory 选项组
  （`--trigger/--count/--image CHIP=PATH/--skip-uptodate/--resume/--auto-jump/--app-version/
  --records/--jsonl/--yes`）。
- `factory/local.example.json`：工厂本地配置示例（chips 覆盖 / images 预设 / 记录路径）；
  实际配置文件、量产镜像与结果记录已列入 `.gitignore`。

### Changed

- **`--chip` 参数化（兑现已记录的跨仓欠账）**：`APP_SIZE=0xB800` 不再是唯一口径——
  `run_upgrade` / `selftest` 接受档案的 APP 分区大小，`--chip auto` 先确保进入 BL 再探查；
  `parse_info` 拆出结构化同源 `parse_info_fields()`（字符串输出逐字节不变，单测回归兜底）。
- **断电钻具 `bl_powerloss_drill.py`**：pyocd 目标名与 Keil pack 目录随 `--chip` 档案走
  （`--pack-root` / `--pack` / `--pyocd-target` 可覆盖），不再是 F103 字面常量。
- **GUI 版式修复**：高级面板的提示行与模式开关行此前超出窗口宽度被静默裁掉
  （内容需求 1027 px > 窗口 680 px），现按四种模式组合重排并逐档定尺寸，
  单测加「内容装得进窗口」断言；双开（工厂+高级）时自动压缩表格行数与日志窗高度。
- **GUI 行为**：开工与模式切换此前会清空结果表与计数——现改为跨批次累计
  （清空由「清空计数/结果表」按钮负责）；结果表行号跨会话唯一
  （两次会话的 seq 都从 1 起，此前第二次会话会覆盖第一次的行）；
  模式切换销毁窗口前取消 pump 轮询（消除 Tk 回调告警）；
  串口连接类型改为主线程取好（原先在工作线程读 Tk 变量）。
- **打包与冻结路径**：`build_exe.bat` 给两个 exe 都加 `--add-data "bl_chip_profiles.json;."`
  ——档案包是**运行期读取的数据文件**（不是 import），漏打则换机/无固件仓时工具报
  「未发现芯片档案」；冻结路径与源码运行分离（exe 侧 app 目录 = exe 所在目录、只读资源走
  `_MEIPASS`），工厂配置、结果记录与预设镜像按 exe 目录解析而非进程 CWD；
  `build_exe.bat` 同时改为 CRLF 行尾 + `chcp 65001`（原先 UTF-8 + LF 在 GBK 控制台下会把
  中文注释行解析成乱码，构建直接失败）。`dist` 下两个 exe 已随本版重建。
- 主机侧单测扩至 122 项（模式矩阵与版式、档案来源一致性与几何自检、探查四种结局、
  镜像体检、升级上限与停止不落一字节、批量引擎全流程/同 UID 跳过/已是最新/缺预设停批/
  校验失败/软停/硬停/CSV+JSONL 落盘与续烧、钻具参数化、打包路径、CLI 新参数面）。

### Fixed

- F411CEU6 上 `selftest` 的「越界防护」步不再假 FAIL（按档案取 APP 分区末尾）。
- 工厂模式「自动开工」遇到未配预设镜像的芯片**停批并提示**，绝不猜镜像。

### Notes

- **协议与固件行为无改动**：升级帧 VER 0x01 未变（帧格式/命令/状态码均不变），
  固件仓无需同步发版——工厂模式的芯片识别只用 GET_INFO 已有的 Flash 容量与 UID，
  未新增协议字段。
- 已在文档写明的局限：同 Flash 容量且同 APP 分区的芯片无法仅凭协议区分（需人工
  `--chip`；更彻底的做法是固件侧加芯片标识字段，或走调试口读 `DBGMCU_IDCODE`）；
  边界探查有 2⁻³² 概率命中垃圾 CRC 而持久化一次元数据（该设备随即整片重刷）；
  立即停止的时延上限 = 当前命令超时（≤5 s，串口读不可中断）。
- 验证状态：主机侧单测 122/122 通过；**F103C8T6 板级实测**——自动探查命中、
  单台批量烧录（~3.5 s/台，CRC 校验通过 + 跳转 APP）、跳过已是最新（真实 CRC32 一致）、
  断点续烧（真实 UID）、升级中立即停止（中断判 FAIL，设备 `app_valid=0` 拒绝运行不完整 APP）；
  **打包 exe 同样实测**——`dist/bl_upgrade.exe chips detect` 真机命中、单台批量烧录 OK，
  隔离目录只放一个 exe 也能列出 2 片档案（内置包生效），GUI exe 以工厂模式启动、面板与
  档案表正常；**F411CEU6 板级未跑**（其档案与 `--chip` / 钻具路径仅主机侧单测覆盖）。

## [Unreleased]

### Changed

- 文档同步固件仓 BL 0.3.0（ADR-019：服务可选挂载与默认示例配置最小化）：
  「已验证组合」更新为 BL 0.3.0 + STM32F103C8T6（全流程）/ STM32F411CEU6 最小包
  （info/upgrade/jump/setmeta 回环）；补充「蓝牙连接需固件启用蓝牙通道」前提；
  镜像上限说明标注 F411 已验证但仍受 46K 上限约束
- 文档明确重试/超时的适用范围与章节号：自动重发只覆盖 `upgrade` 全流程，手工单命令不重试
  （各自超时上限 1–8 s）；协议章节引用由 §7 更正为 §6（SEQ 语义与超时），补 F411 已知限制
  行（selftest 边界步假 FAIL 与断电钻具不可用）
- 文档跟进（签名验签 1.4.0）：README(.en) 兼容性表「配套固件」行补「签名升级（`--key`）
  另需固件 BL 0.4.0 以上 + 该支持包 `BL_SIGN_EN=1`」；「签名验签升级」章节补验证状态
  ——主机侧单测通过（keygen 产物格式、签名→公钥回验、篡改失效、0x11 帧结构 LEN=72），
  板级 HIL 未跑（固件仓 `docs/dev/test_plan.md` §5.1 待办）

### Security

- **密钥材料加入 .gitignore**：新增 `*.pem` / `*.key` / `bl_sign_pubkey_local.h`（含任意
  层级）。`keygen --out-key` 默认把私钥写成仓库根下的 `./sign_test_key.pem`，此前不在忽略
  列表——`git add -A` 会把私钥纳入提交，与 ADR-020「密钥任何形态不入库」不符

### Notes

- 协议与固件行为无改动：升级协议 VER 0x01 未变（帧格式/命令/状态码均不变）
- 待办（跨仓 follow-up）：`--chip` 参数化——`bl_upgrade.py` 的 `APP_SIZE=0xB800`
  与断电钻具的 pyocd 目标名均硬编码 F103（F411 上 selftest 的「越界防护」步假 FAIL、
  钻具不可用）

## [1.4.0] - 2026-09-30

签名验签集成（配套固件 LiteBootLoader 0.4.0 ADR-020）。协议 VER 0x01 不变。

### Added

- **`keygen` 子命令（密钥对生成器模块）**：生成 P-256 测试密钥对——私钥
  PEM（本地保管）+ 公钥本地头 `bl_sign_pubkey_local.h`（写入固件仓芯片端口目录，
  与固件 `core/bl_sign.c` 消费约定一致；注释刻意全 ASCII——armcc 以本地编码读源
  文件，UTF-8 中文注释有吞换行风险）。**密钥任何形态不入库**（ADR-020）。
- **`upgrade --key <pem>` 签名校验**：写块完成后改发 `0x11 VERIFY_SIGNED`
  （DATA = size+crc32+signature 64B r‖s 大端），auth 置位方可跳转；`SIGN_ERROR(0x06)`
  时给出排查提示（固件非 BL_SIGN_EN=1 / 公钥不配对）。cryptography 懒加载——
  非签名路径零新依赖；超时/重试沿用 VERIFY 档（T_VERIFY 5s ×3 尝试）。
- **GUI 同步**：高级模式新增「签名私钥（可选）」行（浏览选择 + 「生成密钥对」按钮，
  走 save 对话框），一键升级自动携带——与 CLI 功能同步。

### Changed

- 主机侧单测扩至 45 项（keygen 产物格式、签名→公钥回验/篡改失效——经
  裸 r‖s ↔ DER 往返完整验证固件消费口径、0x11 帧结构 LEN=72/CMD/CRC、
  keygen/--key CLI 解析）；签名用例在缺 cryptography 时自动跳过。
- CMD 表 + `verify_signed`、STATUS 表 + `SIGN_ERROR`。

## [1.3.1] - 2026-09-30

审计 2026-09-29（主仓 `docs/review/audit-2026-09-29.md`，本地文档）修复。协议 VER 0x01
不变。

### Fixed

- **selftest 补传 `--conn`**（审计 P2-3）：`selftest --conn bt` 此前静默退化为
  1 次打开尝试（打开重试逻辑由 `conn` 决定），蓝牙 SPP 重连竞态下首开易失败；
  GUI 路径本就正确，仅 CLI 分发漏传
- **`parse_info` 短响应守卫 19→31 B**（审计 P3-2）：19~30 B 的 CRC 合法但长度异常
  响应原本落入 `unpack_from(d, 19)` 触发 `struct.error` 栈回溯退出；现归入
  「短响应」提示，且 CLI 分发层统一兜底 `RuntimeError/IndexError` 友好退出
  （现网 BL/APP 不会产生此类响应，防御性加固）

### Changed

- 主机侧单测扩至 34 项（parse_info 30B 防御、selftest `--conn` 传参断言）

## [1.3.0] - 2026-09-27

配套固件仓 LiteBootLoader 0.2.0《空口蓝牙串口及OTA》（ADR-016）。

### Added

- **蓝牙连接支持**（目标 1）：CLI `--conn serial|bt`（bt=蓝牙 SPP/HC-05，打开失败自动
  重试 ×3、间隔 0.5 s，容重连竞态）；GUI 串口框新增「连接类型」下拉（有线串口/蓝牙
  HC-05），经唯一开漏斗 `_open_and_close` 传导——协议栈（帧/重试/幂等）与有线完全共用
- **OTA 状态查询**（目标 3）：CLI 新增 `ota` 子命令、GUI 高级面板新增「OTA 查询」按钮，
  发送 0x10 OTA_QUERY 并解析 22 B 响应（BL/APP 版本、APP 实时有效性、app_size/CRC、
  元数据 seq、请求到达通道、蓝牙连接状态）；对 BL 0.1.0 旧固件回 STATE_ERROR
- 模块需一次性 AT 配置数据模式到 115200，步骤见固件仓 `docs/dev/bluetooth_notes.md` §5

### Changed

- 主机侧单测扩至 32 项（parse_ota 全量/状态/防御、OTA_QUERY 帧实测模板、
  `ota` + `--conn` 解析器、命令表含 ota）

## [1.2.0] - 2026-09-27

### Added

- **GUI 高级模式**（勾选"高级模式"→ 弹窗确认 → 窗口级重启后生效；取消勾选对称处理；
  状态记录在 `~/.litebootupgrader_gui.json`）：高级面板与 CLI 全量功能同步——
  INFO / META / ERASE（二次确认）/ SELFTEST（print 桥接进日志窗）/ VERIFY / LISTEN /
  RAW / SETMETA，以及波特率、pace(ms) 参数；基础模式行为不变（日常四操作）
- CLI 新增 `--version`；版本号收敛为模块常量 `VERSION` 单源（CLI description、
  `--version`、GUI 标题共用）
- 主机侧单测扩至 25 项（CLI 解析器回归 + GUI 状态持久化往返）

### Fixed

- 移除 CLI 的 `write` 幽灵子命令：此前在 choices 中但无实现分支，选中后静默无输出；
  分块写入由 `upgrade` 内部完成（协议命令映射 `CMD["write"]` 不受影响）
- GUI 三个工作线程统一捕获 `(Exception, SystemExit)`（串口打开失败 sys.exit 退出
  线程时界面不再卡"运行中"）

## [1.1.3] - 2026-09-27

- review 处理——GUI 工作线程捕获 `SystemExit`（串口占用时界面不再卡"运行中"）；
  `cmd()` 对 CMD 不匹配帧与 SEQ 错位同样丢弃续等；响应 DATA 最小长度防御
  （parse_meta/verify/状态字节，友好退出替代栈回溯）；`build_frame` 拒绝超长 DATA；
  单测扩至 19 项。

## [1.1.2] - 2026-09-26

- review 处理——`run_upgrade` 改 with 打开镜像且 `cmd_upgrade` 捕获 OSError
  （坏路径友好退出，实测验证）；`cmd()` 对 SEQ 错位帧丢弃并等到 deadline
  （迟到响应不再误当本条答复，重试层兜底，单测+真机回归）；钻具 Event 动态属性
  改独立容器；双 exe 重建至本版本。

## [1.1.1] - 2026-09-26

- 新增主机侧单测、PyInstaller 双 exe 构建脚本；修复 GUI 日志窗 `tk.Text.state()`
  误用（那是 ttk 控件 API）导致 UI 刷新链断裂。

## [1.1.0] - 2026-09-26

- CLI 库化重构（log/progress 回调）+ tkinter GUI；自 LiteBootLoader 迁出独立建仓。

## [≤1.0.0]

- 见 LiteBootLoader 仓库 git 历史（`tools/python/` 时期）。
