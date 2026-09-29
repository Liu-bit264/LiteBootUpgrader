# 贡献指南（CONTRIBUTING）

感谢关注 LiteBootUpgrader！本工具是
[LiteBootLoader](https://github.com/Liu-bit264/LiteBootLoader)（固件仓）的官方上位机，
协议规范以固件仓 `docs/protocol.md` 为唯一契约。工具的使用方式见
[README.md](README.md)。

## 反馈 Bug

提交 Issue 时请尽量附上：

1. 工具版本（`bl_upgrade.py --version`）与运行方式（exe / 源码）
2. 配套固件版本（对端 BL/APP 的 `info` 输出）
3. 串口与硬件环境（调试器、USB 线）
4. 复现步骤与完整日志输出（GUI 日志窗或 CLI stdout）
5. 升级链路问题请说明阶段：擦除 / 写入 / 校验 / 跳转 / APP 运行

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

## 开发环境

- Python 依赖建议隔离运行：`uv run --python 3.12 --with pyserial <脚本>` 或 venv；
  运行时依赖仅 pyserial（GUI 另需 tkinter）

## 测试与验收

```bash
# 主机侧单测（无需硬件，32 项：CRC KAT/帧模板/解析器/响应解析含 OTA/SEQ 与 CMD 错位丢弃/GUI 导入/CLI 解析器/GUI 状态持久化）
uv run --python 3.12 --with pyserial python test_host_protocol.py

# 硬件在环：15 步升级流程自检（板子在线时）
uv run --python 3.12 --with pyserial bl_upgrade.py selftest --port COM4

# 验收 #9 钻具：写入风暴 + 复位注入（需板子 + 调试器）
uv run --python 3.12 --with pyserial --with pyocd bl_powerloss_drill.py --port COM4 --rounds 10
```

改动 CLI/GUI 或协议行为后，至少附主机侧单测输出；涉及升级流程的建议附硬件在环
selftest 结果。

## 提交 PR

1. 提交信息遵循 **Conventional Commits 1.0.0**（`feat` / `fix` / `docs` / `refactor` /
   `test` / `chore`）；版本遵循 **SemVer 2.0.0**（`fix` → PATCH，`feat` → MINOR）
2. 版本号只改 `bl_upgrade.py` 的 `VERSION` 常量（`--version`、CLI description、
   GUI 标题均引用它）；发版时更新 CHANGELOG.md
3. 协议层改动必须与固件仓 `docs/protocol.md` 一致，并补主机侧单测；
   涉及固件行为联动的，在 PR 中说明与固件仓的同步情况
4. 改动 CLI/GUI 后运行 `test_host_protocol.py` 并附输出；涉及升级流程的改动
   建议附硬件在环 selftest 结果

## 与固件仓的联动

- 协议契约：帧格式、命令、状态码、重试/超时约定（§7）以
  [LiteBootLoader docs/protocol.md](https://github.com/Liu-bit264/LiteBootLoader/blob/main/docs/protocol.md) 为准
- 固件仓协议或行为变更后，本仓需同步回归（`test_host_protocol.py` + 硬件 E2E）
