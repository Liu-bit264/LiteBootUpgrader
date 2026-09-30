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
├── bl_upgrade.py          CLI 与协议库（升级流程唯一实现；run_upgrade 支持 log/progress/stop 回调）
├── bl_chip.py             芯片档案 + 主机侧芯片探查 + 镜像体检（消费固件仓 chips/*.json）
├── bl_factory.py          批量刷写引擎（GUI 工厂面板与 CLI factory 共用；依赖全可注入）
├── bl_chip_profiles.json  内置芯片档案包（生成物，`chips sync` 从固件仓重建，勿手改）
├── bl_upgrade_gui.py      tkinter 图形界面（基础 / 高级 / 工厂三种形态，后两者可同时开启）
├── bl_powerloss_drill.py  验收 #9 参数区写入中断恢复钻具（`--chip` 取 pyocd 目标/pack）
├── test_host_protocol.py  主机侧无硬件单测（136 项，纯脚本自计数）
├── factory/
│   └── local.example.json 工厂本地配置示例（复制为 local.json；实际配置与记录不入库）
├── bl_upgrade_gui.bat     GUI 双击启动器
├── build_exe.bat          Windows 可执行程序构建脚本（PyInstaller → dist/；
│                          含 --add-data 把 bl_chip_profiles.json 打进包内，勿删）
├── docs/                  GUI 截图（gui.png / gui_adv.png / gui_factory.png / gui_factory_adv.png）
├── LICENSE                MIT 许可证
└── dist/                  构建产物（不入库，见 .gitignore）
```

## 开发环境

- Python 依赖建议隔离运行：`uv run --python 3.12 --with pyserial <脚本>` 或 venv；
  运行时依赖仅 pyserial（GUI 另需 tkinter）

## 测试与验收

```bash
# 主机侧单测（无需硬件，136 项：CRC KAT/帧模板/解析器（含结构化 GET_INFO）/响应解析含 OTA/
# SEQ 与 CMD 错位丢弃/芯片档案与几何自检/探查四种结局/镜像体检/升级上限与停止不落字节/
# 批量引擎全流程与落盘续烧/多端口并行（假串口双口、共享记录单表头、停止广播）/钻具参数化/
# CLI 参数面/GUI 模式矩阵与版式（含小屏滚动替代裁剪）/状态持久化）
uv run --python 3.12 --with pyserial python test_host_protocol.py

# 硬件在环：15 步升级流程自检（板子在线时；非 F103 芯片加 --chip <id> 取对分区）
uv run --python 3.12 --with pyserial bl_upgrade.py selftest --port COM4 --chip f103c8t6

# 工厂批量（板子在线时；先 chips list 确认档案，镜像必须与芯片匹配——会做向量表体检）
uv run --python 3.12 --with pyserial bl_upgrade.py chips detect --port COM4
uv run --python 3.12 --with pyserial bl_upgrade.py factory --port COM4 --chip auto \
    --image f103c8t6=../LiteBootLoader/app/examples/f103c8t6_app/app.bin \
    --count 1 --auto-jump --records factory/records/records.csv --yes

# 多端口并行（一台机器插多个串口时；每口一个独立会话，停止广播到所有口）
uv run --python 3.12 --with pyserial bl_upgrade.py factory --ports COM4,COM5,COM6 \
    --chip auto --count 6 --skip-uptodate --auto-jump \
    --records factory/records/records.csv --yes

# 验收 #9 钻具：写入风暴 + 复位注入（需板子 + 调试器；--chip 选 pyocd 目标与 pack）
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

- 协议契约：帧格式、命令、状态码、重试/超时约定（§6）以
  [LiteBootLoader docs/protocol.md](https://github.com/Liu-bit264/LiteBootLoader/blob/main/docs/protocol.md) 为准
- 芯片档案契约：本仓 `bl_chip.py` 与固件仓 `chips/<id>.json`（ADR-015 构建侧事实源）同构消费。
  固件仓改分区/几何后，跑 `bl_upgrade.py chips sync` 重建内置档案包 —— 单测里的
  「内置包与固件仓同值」断言会拦下漂移；`chips list` 会打印每片档案的来源
- 固件仓协议或行为变更后，本仓需同步回归（`test_host_protocol.py` + 硬件 E2E）
