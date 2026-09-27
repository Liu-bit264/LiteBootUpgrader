# 贡献指南（CONTRIBUTING）

感谢关注 LiteBootUpgrader！本工具是
[LiteBootLoader](https://github.com/Liu-bit264/LiteBootLoader)（固件仓）的官方上位机，
协议规范以固件仓 `docs/protocol.md` 为唯一契约。

## 反馈 Bug

提交 Issue 时请尽量附上：

1. 工具版本（`bl_upgrade.py --version`）与运行方式（exe / 源码）
2. 配套固件版本（对端 BL/APP 的 `info` 输出）
3. 串口与硬件环境（调试器、USB 线）
4. 复现步骤与完整日志输出（GUI 日志窗或 CLI stdout）
5. 升级链路问题请说明阶段：擦除 / 写入 / 校验 / 跳转 / APP 运行

## 开发环境

- Python 依赖建议隔离运行：`uv run --python 3.12 --with pyserial <脚本>` 或 venv；
  运行时依赖仅 pyserial（GUI 另需 tkinter）
- 主机侧单测（无需硬件）：

  ```bash
  uv run --python 3.12 --with pyserial python test_host_protocol.py
  ```

- 硬件在环验证：`bl_upgrade.py selftest --port COMx`（15 步升级流程自检）

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
