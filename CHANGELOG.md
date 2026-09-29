# 更新日志（Changelog）

本项目的所有显著变更记录于此。格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer 2.0.0](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Changed

- 文档同步固件仓 BL 0.3.0（ADR-019：服务可选挂载与默认示例配置最小化）：
  「已验证组合」更新为 BL 0.3.0 + STM32F103C8T6（全流程）/ STM32F411CEU6 最小包
  （info/upgrade/jump/setmeta 回环）；补充「蓝牙连接需固件启用蓝牙通道」前提；
  镜像上限说明标注 F411 已验证但仍受 46K 上限约束

### Notes

- 本次代码无需改动：升级协议 VER 0x01 未变（帧格式/命令/状态码均不变）
- 待办（跨仓 follow-up）：`--chip` 参数化——`bl_upgrade.py` 的 `APP_SIZE=0xB800`
  与断电钻具的 pyocd 目标名均硬编码 F103（F411 上 selftest 第 9 步假 FAIL、
  钻具不可用）

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
