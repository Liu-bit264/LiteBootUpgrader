# LiteBootUpgrader — LiteBootLoader 上位机

LiteBootLoader（[主仓](../LiteBootLoader)）的官方上位机：串口一键升级、跳转、复位与流程自检。
协议流程的**唯一实现**在本仓 `bl_upgrade.py`；协议规范、固件与文档见主仓
`docs/protocol.md`（帧格式 CRC16/MODBUS、命令表、状态机）。

## 运行（依赖隔离约定）

本机 Miniforge base 禁止装包，一律 uv 隔离（wheel 只进 uv 缓存）：

```bash
# 图形界面（或直接双击 bl_upgrade_gui.bat）
uv run --python 3.12 --with pyserial bl_upgrade_gui.py

# 命令行
uv run --python 3.12 --with pyserial bl_upgrade.py upgrade <镜像.bin> --port COM4
uv run --python 3.12 --with pyserial bl_upgrade.py selftest --port COM4
```

## GUI

- **一键升级**：从任意状态（BL 或 APP）直接升——对端是 APP 时自动走
  "请求回 BL"（SET_META bl_request → 复位 → 消费标志），全程进度条 + 日志；
- **跳转 APP / 复位 / PING**：单命令操作，自动回读串口横幅；
- 串口互斥：使用前请关闭 VOFA+ / 串口助手。

## CLI 子命令

| 子命令 | 作用 |
|---|---|
| `upgrade <bin>` | 一键升级（ensure_bl → 擦除 → 分块写入 → 校验） |
| `selftest` | 15 步升级流程硬件在环自检 |
| `ping / info / meta` | 握手 / BL 信息与遥测 / 参数区元数据 |
| `erase / write <bin> / verify <size> <crc>` | 手动分步操作 |
| `jump / reset` | 跳转 / 复位 |
| `setmeta <f> <v> / raw <hex> / listen <秒>` | 元数据 / 原始字节 / 监听 |

重试约定（protocol.md §7）：单命令超时 1000 ms（ERASE/VERIFY 5000 ms），
重发 ≤3 次、间隔 2.2 s（≥BL 帧内 2000 ms 解析器复位窗口）。

## 测试工具

`bl_powerloss_drill.py`：验收 #9 参数区写入中断恢复钻具（SET_META 写入风暴 +
pyocd 随机复位注入，逐轮校验恢复不变量）：

```bash
uv run --python 3.12 --with pyserial --with pyocd bl_powerloss_drill.py --port COM4 --rounds 10
```

## 版本

- 1.1.0（2026-09-26）：CLI 库化重构（log/progress 回调）+ tkinter GUI；自主仓
  LiteBootLoader 迁出独立建仓。协议 VER 0x01，配套 BL 0.1.0+。
- 历史（≤1.0.0）见主仓 git 历史（tools/python/）。
