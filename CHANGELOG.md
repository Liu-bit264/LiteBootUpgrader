# 更新日志（Changelog）

本项目的所有显著变更记录于此。格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer 2.0.0](https://semver.org/lang/zh-CN/)。

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
