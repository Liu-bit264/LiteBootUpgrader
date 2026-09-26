# LiteBootUpgrader 审查与修改意见

> 生成日期：2026-09-26
> 依据：对 bl_upgrade.py / bl_upgrade_gui.py / bl_powerloss_drill.py / test_host_protocol.py
> 的只读审查，以及本机实际执行的主机侧单测（13/13 通过，见 §3）。
> 说明：本文列出审查结论与改进建议，不含已实现代码的重写。
> 优先级 P1（重要）> P2 > P3（次要）。

## 0. 总体结论

一套成熟、克制的上位机工具：CLI 库化、GUI 薄封装复用同一协议实现、
主机侧单测覆盖到位。协议实现与固件端（LiteBootLoader）自洽，测试可复现。
本轮发现均为健壮性/可读性层面，无正确性缺陷。

本机实测：`test_host_protocol.py` 13/13 通过（用最小 serial stub 绕开无硬件环境，
纯逻辑路径真实执行）。覆盖 CRC16 KAT、帧模板一致性、recv_frame 重同步、
parse_info/parse_meta、GUI 可导入、命令表齐全。

## 1. 修改意见（按优先级）

### [P2] run_upgrade / cmd_upgrade 缺文件打开异常兜底
- 位置：`bl_upgrade.py` `run_upgrade()`、`cmd_upgrade()`。
- 现象：`run_upgrade` 用 `open(path, "rb").read()` 未包 try，且未显式关闭句柄；
  `cmd_upgrade` 只捕获 `ValueError`。CLI `upgrade` 传不存在的路径会抛裸
  `FileNotFoundError` 栈回溯，而非友好退出。
- 影响：CLI 误传路径时体验差；文件句柄依赖 GC 回收。
- 建议：`run_upgrade` 改用 `with open(path, "rb") as f:`；`cmd_upgrade` 同时捕获
  `OSError`（含 `FileNotFoundError`）并走 `sys.exit` 友好提示。
- 优先级理由：唯一会产生裸栈回溯的入口，改动局部、收益直接。

### [P3] cmd() 的 SEQ 错位仅告警不纠正
- 位置：`bl_upgrade.py` `cmd()`。
- 现象：响应 SEQ 与请求不符时打印 `[!] SEQ 错位`，但仍把该帧当有效返回。
- 影响：极端场景（迟到的上一条响应）会被误当本条答复。串口点对点场景风险低。
- 建议（可选）：SEQ 不匹配时视为未收到，继续等待至 deadline，超时再交由重试层。

### [P3] 掉电钻具在 Event 对象上动态挂属性
- 位置：`bl_powerloss_drill.py` `one_round()`。
- 现象：在 `threading.Event` 实例上动态设置 `reset_failed` 属性（带 `# type: ignore`）。
- 影响：功能无误，但把失败状态耦合进 Event 对象，可读性差。
- 建议：改用独立可变容器（如单元素 `list` 或 `dict`）在线程间传递失败标志。

## 2. 已核验为正确的设计（无需改动）

- `recv_frame` 帧同步稳健：非法长度丢 1 字节重同步、坏 CRC 整帧丢弃计入噪声、
  保留尾字节防 SOF 被截断。
- CRC16/MODBUS 与固件一致；`build_frame` 覆盖 VER..DATA，与固件 bl_protocol.c 对齐。
- `ensure_bl` 的 APP->BL 自动回流（SET_META bl_request -> 等复位 -> 重探）与固件
  app_request.c 消费路径吻合。
- `run_upgrade`：4 字节对齐补齐、CHUNK_PAYLOAD=252（DATA<=256 减 4B 头）、
  重试间隔 2.2s >= 固件 2000ms 帧内超时，约定自洽。
- GUI 线程模型正确：工作线程只经 queue 回传，主线程 after 轮询刷 UI，不跨线程碰控件。
- pyocd 注入命令为全字面常量、shell=False，无外部输入注入面。

## 3. 本机实际执行的测试

命令（无硬件环境，用最小 serial stub 提供 SerialException/Serial/list_ports.comports，
测试本身使用 FakeSerial，不触真实串口 I/O）：

```
$env:PYTHONPATH=<stub>; python test_host_protocol.py
```

结果：`== 主机侧单测：13/13 通过 ==`
- CRC16 KAT '123456789'==0x4B37、空串==0xFFFF
- PING / SET_META 帧与实测模板逐字节一致
- recv_frame：分块+噪声前缀 / 坏 CRC 丢弃 / 伪 SOF 重同步
- parse_info 67B 全量 + 1B 短响应；parse_meta 字段往返
- GUI 模块可导入；4B 补齐 CRC 口径一致；命令表齐全

## 4. 本环境未执行 / 未验证项（复现命令）

- 需真实串口/硬件的路径未运行（本机无 COM 设备）：
  - `uv run --python 3.12 --with pyserial python bl_upgrade.py selftest --port COM4`
  - `uv run --python 3.12 --with pyserial python bl_upgrade.py upgrade <app.bin> --port COM4`
  - `uv run --python 3.12 --with pyserial --with pyocd python bl_powerloss_drill.py --port COM4 --rounds 10`
- pyserial 无法经 uv 联网或本地缓存获取；13/13 单测为 serial stub 下的纯逻辑结果，
  未触及真实 serial.Serial I/O。
- PyInstaller 双 exe 构建（build_exe.bat）未执行。

## 5. 建议的下一步

优先处理第 1 项（run_upgrade 文件打开加 with + OSError 兜底），消除 CLI 传错路径时的
裸栈回溯。其余两项为可读性/健壮性增强，可随后续维护一并处理。


---

## 6. 处理记录（2026-09-26，ZCode 复核并实施）

| 项 | 处置 | 证据 |
|---|---|---|
| [P2] 文件打开异常兜底 | **已修**：`run_upgrade` 改 `with open(...)`；`cmd_upgrade` 增捕 `OSError` 并 `sys.exit` 友好提示 | 实测 `upgrade no_such_image.bin --port COM4` → `[X] 打开镜像失败：[Errno 2] …`，无裸栈回溯 |
| [P3] SEQ 错位仅告警 | **已修（采纳建议）**：`cmd()` 改为 deadline 内循环——SEQ 错位帧丢弃、打印告警、继续等待；deadline 到仍无正确响应返回 None 交重试层。全命令幂等，丢弃迟到帧无副作用 | 新增 2 项单测（错位帧丢弃后收正确帧 / 仅迟到帧超时返 None），15/15 通过；真机 setmeta→upgrade→jump 回归通过 |
| [P3] Event 动态属性 | **已修**：改为独立字典容器 `rst = {"failed": bool}` 在线程间传递 | 代码审阅 |
| §4 未执行项 | **已补验**：selftest 15/15、真实 APP upgrade+jump、钻具 10/10、PyInstaller 双 exe 构建与冒烟均已在有硬件的本机完成（见主报告与 git 历史）；本轮改动后双 exe 已重建至 v1.1.2 并复验 | `dist/bl_upgrade.exe` SHA `03abb144…`、`dist/bl_upgrade_gui.exe` SHA `ade32bc5…` |

版本随处理升至 **1.1.2**（SemVer：fix → PATCH）。
