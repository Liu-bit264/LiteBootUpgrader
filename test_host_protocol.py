#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_host_protocol.py — 上位机协议层主机侧单测（无需硬件、无需串口、不落盘）

与本仓 bl_upgrade.py 同目录放置，直接 import，不做任何路径回溯。
运行：
  uv run --python 3.12 --with pyserial python test_host_protocol.py
  签名用例（1.4.0）需 cryptography，推荐统一：
  uv run --python 3.12 --with pyserial --with cryptography python test_host_protocol.py
  （或已装依赖的任意 python 直接运行；缺 cryptography 时签名用例自动跳过）

覆盖：
  1. CRC16/MODBUS 已知答案（KAT）
  2. 帧构造与实测帧一致性（docs/protocol.md §9.2 的 PING/SET_META 实测帧）
  3. recv_frame 解析：分块到达/噪声前缀/坏 CRC 丢弃/伪 SOF 重同步
  4. parse_info / parse_info_fields 各长度档（67B 全量 + 31B 精简 + 短响应）
  5. parse_meta 字段往返
  6. 镜像 4 字节补齐后的 CRC 与 GUI 信息栏口径一致 + GUI 模块可导入
  7. CLI 解析器：write 幽灵子命令已移除、--version 可用、ota 子命令与 --conn 校验
  8. GUI 模式状态持久化（缺失/回读/损坏容错；用系统临时目录，自动清理）
  9. parse_ota 与 OTA_QUERY 请求帧实测模板（1.3.0，protocol.md §5.10/§7.5）
  10. 芯片档案层（1.5.0）：内置包与固件仓 chips/*.json 同值、几何自检拒绝非法档案
  11. 芯片探查：容量指纹命中 / 未收录容量 / 同容量撞车走 VERIFY 只读探针 / 短响应
  12. 镜像体检：向量表（MSP/Reset Handler）与大小上限、--force 应急口
  13. 升级上限随档案放宽 + 停止请求不落一字节
  14. 批量引擎：单台全流程、同 UID 复烧跳过、跳过已是最新、缺预设停批、
      校验失败、软停、CSV 落盘与断点续烧
  15. CLI 新参数（--chip/--profiles/--no-probe/chips/factory）与条目校验
  16. GUI 模式矩阵：工厂与高级互相独立、可共存、互不夹带
退出码 0=全部通过。
"""
import csv
import inspect
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

import bl_chip as bc
import bl_factory as bf
import bl_upgrade as blp

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


class FakeSerial:
    """假串口：按预设块序列返回 read()，用于驱动 recv_frame/cmd。"""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.wrote = b""

    def read(self, n):
        if self.chunks:
            return self.chunks.pop(0)
        return b""

    def write(self, data):
        self.wrote += data

    def flush(self):
        pass


def make_bl(chunks):
    bl = object.__new__(blp.BootLoader)   # 跳过 __init__（不开真串口）
    bl.buf = b""
    bl.s = FakeSerial(chunks)
    bl.noise = b""
    bl.sent_bytes = 0
    bl.expected_delivered = 0
    bl.pace = 0
    bl.seq = 0
    return bl


# ---- 应答式假串口：解析写出的请求帧，按 handler 生成响应（驱动完整命令往返） ----
C_PING, C_INFO, C_ERASE, C_WRITE, C_VERIFY, C_SET_META, C_GET_META, C_JUMP = (
    blp.CMD[n] for n in ("ping", "info", "erase", "write", "verify",
                         "set_meta", "get_meta", "jump"))


class EchoSerial:
    """假串口：write() 时解析请求帧，按 handler(cmd, seq, data) 生成响应帧入队。"""

    def __init__(self, handler):
        self.handler = handler
        self.chunks = []
        self.wrote = b""
        self.closed = False

    def read(self, n):
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, data):
        self.wrote += data
        if len(data) < 11:
            return
        cmd, seq = data[3], data[4]
        ln = data[5] | (data[6] << 8)
        resp = self.handler(cmd, seq, data[7:7 + ln])
        if resp is not None:
            self.chunks.append(blp.build_frame(cmd | 0x80, seq, resp))

    def flush(self):
        pass

    def close(self):
        self.closed = True


def make_echo_bl(handler):
    bl = object.__new__(blp.BootLoader)
    bl.buf = b""
    bl.s = EchoSerial(handler)
    bl.noise = b""
    bl.sent_bytes = 0
    bl.expected_delivered = 0
    bl.pace = 0
    bl.seq = 0
    return bl


def info_payload(uid=b"\x01" * 12, flash_kib=64, app_valid=0, app_size=0, app_crc=0,
                 ver=(0, 5, 0), seq=0):
    """GET_INFO 67B 全量响应（status + ver3 + uid12 + flsz2 + valid1 +
    size/crc/seq 12 + 遥测 20 + 现场 16）。"""
    return (bytes([0x00]) + bytes(ver) + uid + struct.pack("<H", flash_kib) +
            bytes([app_valid]) + struct.pack("<III", app_size, app_crc, seq) +
            struct.pack("<IIIII", 0, 0, 0, 0, 0) + struct.pack("<IIII", 0, 0, 0, 0))


def _no_fw_root():
    """一个必然不存在的固件仓路径（让档案只来自内置包，测试可确定）。"""
    return os.path.join(tempfile.gettempdir(), "litebl-no-such-firmware-root")


class PortScript:
    """按序返回串口集合；用完后重复最后一个（newport 模式测试用）。"""

    def __init__(self, seq):
        self.seq = [list(s) for s in seq]
        self.i = 0

    def __call__(self):
        v = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return list(v)


def t_crc16():
    check("CRC16 KAT '123456789'==0x4B37", blp.crc16_modbus(b"123456789") == 0x4B37)
    check("CRC16 空串==0xFFFF(初值)", blp.crc16_modbus(b"") == 0xFFFF)


def t_build_frame():
    f = blp.build_frame(0x01, 0x01)
    check("PING 帧与实测模板一致",
          f == bytes.fromhex("AA55010101000049FC55AA"), f.hex().upper())
    f2 = blp.build_frame(0x06, 0x01, bytes([0x01, 0x01]))
    check("SET_META(01,01) 帧与模板一致",
          f2 == bytes.fromhex("AA5501060102000101F78E55AA"))
    # review 2026-09-27 P3：超长 DATA 拒绝（LEN 只有 2B，静默截断会生成不一致帧）
    try:
        blp.build_frame(0x01, 0x01, b"\x00" * 257)
        check("build_frame 超长 DATA 拒绝", False, "未抛出")
    except ValueError:
        check("build_frame 超长 DATA 拒绝", True)


def t_recv_frame():
    good = blp.build_frame(0x81, 0x07, bytes([0x00, 0x01]))
    # 分两块 + 前置噪声，应解析成功
    bl = make_bl([b"\x00\x11garbage" + good[:5], good[5:] + b"tail"])
    r = bl.recv_frame(0.2)
    check("分块+噪声前缀解析", r is not None and r["cmd"] == 0x81
          and r["seq"] == 0x07 and r["data"] == bytes([0x00, 0x01]))
    # 坏 CRC：静默丢弃返回 None，坏帧计入噪声
    bad = bytearray(good)
    bad[7] ^= 0xFF
    bl = make_bl([bytes(bad)])
    r = bl.recv_frame(0.05)
    check("坏 CRC 静默丢弃", r is None and bl.noise == bytes(bad))
    # 伪 SOF（LEN>256）：丢字节重同步后仍能取到真帧
    stream = b"\xAA\x55" + b"\x01\x02" + b"\xFF\x01" + good
    bl = make_bl([stream])
    r = bl.recv_frame(0.2)
    check("伪 SOF 重同步", r is not None and r["cmd"] == 0x81)


def t_parse_info():
    # 67B 全量遥测：status + ver(3) + uid(12) + flsz(2) + valid(1) + size/crc/seq(12)
    #                + rx/vf(8) + crcfail/bytetimeout(8) + pend(4) + detail(16)
    d = bytearray(b"\x00" + bytes([0, 1, 0]) + b"\x01" * 12 +
                  struct.pack("<H", 64) + b"\x01" +
                  struct.pack("<III", 5540, 0x7FB143E0, 175) +
                  struct.pack("<IIIII", 100, 98, 1, 2, 3) +
                  struct.pack("<IIII", 1500, 0, 2, 9))
    assert len(d) == 67
    s = blp.parse_info(bytes(d))
    check("parse_info 67B 全量", "v0.1.0" in s and "app_size=5540" in s
          and "seq=175" in s and "rx=100" in s and "饥饿1500ms" in s)
    check("parse_info 1B 短响应提示", "短响应" in blp.parse_info(b"\x03"))
    # 审计 2026-09-29 P3-2：守卫 19→31B——19~30B 原会落入 unpack_from(d,19)
    # 触发 struct.error，现应归入短响应提示
    check("parse_info 30B 短响应防御", "短响应" in blp.parse_info(b"\x00" * 30))


def t_parse_meta():
    d = (b"\x00" + struct.pack("<II", 175, 0x01) + bytes([0, 1, 0]) +
         struct.pack("<II", 5540, 0x7FB143E0) + b"\x01")
    m = blp.parse_meta(d)
    check("parse_meta 字段往返",
          m["seq"] == 175 and m["flags"] == 1 and m["ver"] == (0, 1, 0)
          and m["size"] == 5540 and m["crc"] == 0x7FB143E0 and m["active_copy"] == 1)
    # review 2026-09-27 P2：畸形短响应防御（RuntimeError 而非 struct.error 栈回溯）
    try:
        blp.parse_meta(b"\x00\x01")
        check("parse_meta 短响应防御", False, "未抛出")
    except RuntimeError:
        check("parse_meta 短响应防御", True)


def t_cmd_seq():
    """review P3 + 2026-09-27 P2：SEQ 错位与 CMD 不匹配帧均应丢弃并继续等待。"""
    bl = make_bl([])
    bl.seq = 0x05                       # cmd 内自增 → 期望 SEQ=0x06
    stale = blp.build_frame(0x81, 0x04, bytes([0x00, 0x01]))
    good = blp.build_frame(0x81, 0x06, bytes([0x00, 0x01]))
    bl.s = FakeSerial([stale, good])
    r = bl.cmd("ping", timeout=0.5)
    check("SEQ 错位帧丢弃后收到正确响应",
          r is not None and r["seq"] == 0x06 and r["data"] == bytes([0x00, 0x01]))
    bl = make_bl([])
    bl.seq = 0x05
    bl.s = FakeSerial([stale])
    r = bl.cmd("ping", timeout=0.2)
    check("仅有迟到帧时等到超时返回 None（交重试层）", r is None)
    # review 2026-09-27 P2：CMD 不匹配（如对端切换/异己应答）同样丢弃续等
    wrong_cmd = blp.build_frame(0x84, 0x06, bytes([0x00]))   # 期望 0x81，来的是 0x84
    bl = make_bl([])
    bl.seq = 0x05
    bl.s = FakeSerial([wrong_cmd, good])
    r = bl.cmd("ping", timeout=0.5)
    check("CMD 错位帧丢弃后收到正确响应", r is not None and r["seq"] == 0x06)
    bl = make_bl([])
    bl.seq = 0x05
    bl.s = FakeSerial([wrong_cmd])
    r = bl.cmd("ping", timeout=0.2)
    check("仅有 CMD 错位帧时等到超时返回 None（交重试层）", r is None)


def t_misc():
    import bl_upgrade_gui  # noqa: F401  GUI 模块可导入（含 Tk 依赖面）
    check("GUI 模块可导入", True)
    # 镜像 4 字节补齐后的 CRC（CLI 与 GUI 信息栏共同口径）
    img = b"\x01\x02\x03"
    if len(img) % 4:
        img += b"\xFF" * (4 - len(img) % 4)
    check("4B 补齐 CRC 口径一致",
          zlib.crc32(img) & 0xFFFFFFFF == zlib.crc32(b"\x01\x02\x03\xFF") & 0xFFFFFFFF)
    check("命令表齐全", set(blp.CMD) == {"ping", "info", "erase", "write", "verify",
                                        "set_meta", "get_meta", "jump", "reset",
                                        "ota", "verify_signed"})
    check("状态码表含 SIGN_ERROR", blp.STATUS.get(0x06) == "SIGN_ERROR")


def t_parse_ota():
    """0.2.0/1.3.0：OTA_QUERY(0x10) 响应解析 + 请求帧实测模板（protocol.md §5.10/§7.5）。"""
    # 22B 全量：status + BL/APP 版本(3+3) + valid + size/crc/seq + 通道 + BT
    d = (b"\x00" + bytes([0, 2, 0]) + bytes([1, 0, 0]) + b"\x01" +
         struct.pack("<III", 47104, 0x7FB143E0, 3) + b"\x01" + b"\x01")
    assert len(d) == 22
    s = blp.parse_ota(d)
    check("parse_ota 22B 全量", "v0.2.0" in s and "v1.0.0" in s and "有效" in s
          and "蓝牙 UART2" in s and "已连接" in s, s.replace("\n", " | "))
    check("parse_ota 非 OK 状态直显", "CRC_ERROR" in blp.parse_ota(b"\x01"))
    check("parse_ota 空响应防御", "空响应" in blp.parse_ota(b""))
    try:
        blp.parse_ota(b"\x00\x01")
        check("parse_ota 短响应防御", False, "未抛出")
    except RuntimeError:
        check("parse_ota 短响应防御", True)
    f = blp.build_frame(0x10, 0x01)
    check("OTA_QUERY 帧与 §7.5 实测模板一致",
          f == bytes.fromhex("AA5501100100004CC055AA"), f.hex().upper())


def _have_crypto() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def t_signing():
    """1.4.0（ADR-020）：keygen / sign_image_bytes / 0x11 帧构造。
    cryptography 缺失时自动跳过（非签名路径零依赖）。"""
    if not _have_crypto():
        check("签名用例（cryptography 缺失，跳过）", True)
        return
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        pem = str(Path(td) / "sign_test_key.pem")
        header = str(Path(td) / "bl_sign_pubkey_local.h")
        rc = blp.cmd_keygen(pem, header)
        check("keygen 退出码 0", rc == 0)
        pem_text = Path(pem).read_text(encoding="utf-8")
        check("keygen 私钥 PEM 落盘", "BEGIN PRIVATE KEY" in pem_text)
        h = Path(header).read_text(encoding="utf-8")
        check("keygen 公钥头格式",
              "#define BL_SIGN_PUBKEY_BYTES \\" in h
              and h.count(", \\") == 7 and h.count("0x") == 64)
        # 签名→公钥回验：合法镜像过、篡改镜像不过（与固件 uECC 验签同构）
        img = bytes(range(256)) * 4 + b"\xFF" * 4            # 1 KiB 任意内容 + 填充
        sig = blp.sign_image_bytes(img, pem)
        check("签名长度 64B", len(sig) == 64)
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import \
            encode_dss_signature
        key = serialization.load_pem_private_key(Path(pem).read_bytes(), password=None)
        pub = key.public_key()
        good = bad = None

        def _verify_raw(sig_raw: bytes, data: bytes) -> bool:
            # cryptography 的 verify 只收 DER：把裸 r‖s 大端重编回 DER——
            # 恰好完整验证「DER 签名 → 裸 64B → 回验」往返（固件消费裸 64B）
            r = int.from_bytes(sig_raw[:32], "big")
            s = int.from_bytes(sig_raw[32:], "big")
            try:
                pub.verify(encode_dss_signature(r, s), data, ec.ECDSA(hashes.SHA256()))
                return True
            except Exception:
                return False

        good = _verify_raw(sig, img)
        bad = _verify_raw(sig, img[:-1] + bytes([img[-1] ^ 0xFF]))
        check("签名对原镜像可回验", good is True)
        check("篡改 1 字节后签名失效", bad is False)
        # 0x11 帧结构：LEN=72、CMD=0x11、CRC 可自校验（protocol.md §5.11）
        frame = blp.build_frame(blp.CMD["verify_signed"], 0x21,
                                struct.pack("<II", len(img), 0xDEADBEEF) + sig)
        check("VERIFY_SIGNED 帧长 83B/LEN=72",
              len(frame) == 83 and frame[5] == 72 and frame[3] == 0x11)
        check("VERIFY_SIGNED 帧 CRC 自洽",
              blp.crc16_modbus(frame[2:-4]) == frame[-4] | (frame[-3] << 8))


def t_cli():
    import contextlib
    import io
    ap = blp.build_parser()
    try:
        ns = ap.parse_args(["upgrade", "x.bin"])
        ok = ns.command == "upgrade"
    except SystemExit:
        ok = False
    check("CLI upgrade 子命令可解析", ok)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            ap.parse_args(["write"])
        ok = False
    except SystemExit as e:
        ok = e.code == 2
    check("CLI 无 write 幽灵子命令（无实现分支，upgrade 内部自走分块写）", ok)
    buf = io.StringIO()
    ok = False
    try:
        with contextlib.redirect_stdout(buf):
            ap.parse_args(["--version"])
    except SystemExit as e:
        ok = e.code in (0, None)
    check("CLI --version 输出版本号", ok and blp.VERSION in buf.getvalue())
    try:
        ns = ap.parse_args(["ota", "--conn", "bt"])
        ok = ns.command == "ota" and ns.conn == "bt"
    except SystemExit:
        ok = False
    check("CLI ota 子命令与 --conn bt 可解析", ok)
    # 1.4.0：keygen 子命令与 upgrade --key 解析
    try:
        ns = ap.parse_args(["keygen", "--out-key", "k.pem",
                            "--out-header", "bl_sign_pubkey_local.h"])
        ok = ns.command == "keygen" and ns.out_key == "k.pem"
    except SystemExit:
        ok = False
    check("CLI keygen 子命令与 --out-key/--out-header 可解析", ok)
    try:
        ns = ap.parse_args(["upgrade", "x.bin", "--key", "sign_test_key.pem"])
        ok = ns.command == "upgrade" and ns.key == "sign_test_key.pem"
    except SystemExit:
        ok = False
    check("CLI upgrade --key 可解析", ok)
    try:
        ns = ap.parse_args(["ping", "--conn", "wifi"])
        ok = False
    except SystemExit as e:
        ok = e.code == 2
    check("CLI --conn 非法取值拒绝", ok)
    # 注：selftest 传 --conn 的回归在 t_cli_extra（1.5.0 起 selftest 多了 app_size 形参）


def t_cli_extra():
    """1.5.0：--chip/--profiles/chips/factory 参数面与条目校验。"""
    import contextlib
    import io
    ap = blp.build_parser()
    try:
        ns = ap.parse_args(["upgrade", "x.bin", "--chip", "f103c8t6",
                            "--profiles", "p.json", "--force-image"])
        ok = (ns.chip == "f103c8t6" and ns.profiles == "p.json" and ns.force_image)
    except SystemExit:
        ok = False
    check("CLI upgrade --chip/--profiles/--force-image 可解析", ok)
    try:
        ns = ap.parse_args(["chips", "sync", "--firmware-root", "../LiteBootLoader"])
        ok = ns.command == "chips" and ns.arg == "sync"
    except SystemExit:
        ok = False
    check("CLI chips sync 可解析", ok)
    try:
        ns = ap.parse_args(["chips", "detect", "--no-probe"])
        ok = ns.arg == "detect" and ns.no_probe
    except SystemExit:
        ok = False
    check("CLI chips detect/--no-probe 可解析", ok)
    try:
        ns = ap.parse_args(["factory", "--trigger", "newport", "--count", "5",
                            "--image", "f103c8t6=a.bin", "--skip-uptodate",
                            "--resume", "--auto-jump", "--app-version", "1.2.3",
                            "--jsonl", "r.jsonl", "--yes"])
        ok = (ns.command == "factory" and ns.trigger == "newport" and ns.count == 5
              and ns.image == ["f103c8t6=a.bin"] and ns.skip_uptodate
              and ns.resume and ns.auto_jump and ns.app_version == "1.2.3"
              and ns.jsonl == "r.jsonl" and ns.yes)
    except SystemExit:
        ok = False
    check("CLI factory 参数可解析", ok)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            ap.parse_args(["factory", "--trigger", "magic"])
        ok = False
    except SystemExit as e:
        ok = e.code == 2
    check("CLI --trigger 非法取值拒绝", ok)
    check("CLI parse_app_version 正常", blp.parse_app_version("1.2.3") == (1, 2, 3))
    bad_ok = True
    for bad in ("1.2", "1.x.3", "1.2.300", ""):
        try:
            blp.parse_app_version(bad)
            bad_ok = False
        except ValueError:
            pass
    check("CLI parse_app_version 非法拒绝", bad_ok)
    try:
        bc.resolve_chip_arg("nope", None, {"f103c8t6": bc.ChipProfile(id="f103c8t6",
                                                                    name="x")})
        ok = False
    except bc.ChipError as e:
        ok = "未知芯片" in str(e)
    check("CLI 未知 --chip → ChipError（不静默降级）", ok)
    check("CLI 不传 --chip 时保持旧行为（返回 None）",
          bc.resolve_chip_arg(None, None, {}) == (None, ""))
    # 1.3.1 审计项回归：selftest 必须把 --conn 传给 BootLoader（1.5.0 另加 app_size）
    src = inspect.getsource(blp.main)
    check("selftest 传递 --conn 到 BootLoader",
          "selftest(bl, app_size=" in src
          and "BootLoader(a.port, a.baud, a.pace, a.conn)" in src)


def t_gui_state():
    """GUI 高级模式状态持久化（1.2.0）：缺失/回读/损坏均不得抛异常。"""
    import os
    import tempfile
    from pathlib import Path

    import bl_upgrade_gui as gui
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "state.json")
        check("GUI 状态缺失回落基础模式", gui.read_state(p) == {})
        check("GUI 状态写入回读 advanced=true",
              gui.write_state({"advanced": True}, p)
              and gui.read_state(p).get("advanced") is True)
        bad = os.path.join(td, "bad.json")
        Path(bad).write_text("not-json{{", encoding="utf-8")
        check("GUI 状态损坏回落基础模式", gui.read_state(bad) == {})


def t_info_fields():
    """parse_info_fields：结构化字段 + parse_info 字符串口径逐字节回归（1.5.0）。"""
    d = (b"\x00" + bytes([2, 5, 1]) + b"\xAB" * 12 + struct.pack("<H", 512) + b"\x01" +
         struct.pack("<III", 4096, 0x11223344, 7) +
         struct.pack("<IIIII", 11, 22, 33, 44, 55) + struct.pack("<IIII", 66, 77, 88, 99))
    f = blp.parse_info_fields(d)
    check("parse_info_fields 全量字段",
          f["bl_version"] == (2, 5, 1) and f["uid"] == b"\xAB" * 12
          and f["flash_kib"] == 512 and f["app_valid"] is True
          and f["app_size"] == 4096 and f["app_crc32"] == 0x11223344
          and f["meta_seq"] == 7 and f["rx_total"] == 11 and f["delivered"] == 22
          and f["crc_fail"] == 33 and f["byte_timeout"] == 44
          and f["timeout_pending"] == 55 and f["timeout_detail"] == (66, 77, 88, 99)
          and f["short"] is False)
    expect = ("BL v2.5.1 flash=512KB app_valid=1 app_size=4096 app_crc=0x11223344 "
              "seq=7 uid=ABABABABABABABABABABABAB rx=11 vf=22 crcfail=33 "
              "bytetimeout=44 pend=55 | 超时现场: 饥饿66ms 缓冲77B 状态88 已收99")
    got = blp.parse_info(d)
    check("parse_info 字符串口径逐字节不变（重构回归）", got == expect, got)
    f31 = blp.parse_info_fields(d[:31])
    check("parse_info_fields 31B 最小实现可解析",
          f31["short"] is False and f31["flash_kib"] == 512 and "rx_total" not in f31)
    shorts = [blp.parse_info_fields(b""), blp.parse_info_fields(b"\x03"),
              blp.parse_info_fields(bytes(30))]
    check("parse_info_fields 短响应一律标记 short", all(x["short"] for x in shorts))


def _mk_chip(**over):
    """最小合法芯片清单（chips/<id>.json 形状），字段可覆盖。"""
    d = {
        "id": "f103c8t6", "family": "stm32f1",
        "device": {"name": "STM32F103C8", "pack_id": "P", "cputype": "Cortex-M3"},
        "memory": {"flash_base": "0x08000000", "flash_size": "0x00010000",
                   "sram_base": "0x20000000", "sram_size": "0x00005000"},
        "partitions": {"bootloader": {"base": "0x08000000", "size": "0x4000"},
                       "app": {"base": "0x08004000", "size": "0xB800"}},
        "erase_units": {"uniform": True, "unit_size": "0x400", "count": 64,
                        "typical_erase_ms": 4},
        "sysmem": {"uid_addr": "0x1FFFF7E8", "flsize_addr": "0x1FFFF7E0"},
        "build": {"app_example_dir": "app/examples/f103c8t6_app"},
    }
    d.update(over)
    return d


def t_chip_profiles():
    """档案层：内置包可用、与固件仓同值、几何自检拒绝非法档案。"""
    check("内置档案包存在（打包 exe 单机可用）", os.path.isfile(bc.BUNDLE_PATH),
          bc.BUNDLE_PATH)
    profs = bc.load_profiles(firmware_root=_no_fw_root(), cfg={})
    f1 = profs.get("f103c8t6")
    f4 = profs.get("f411ceu6")
    check("内置包加载 f103c8t6 几何",
          f1 is not None and f1.flash_kib == 64 and f1.app_base == 0x08004000
          and f1.app_size == 0xB800 and f1.pyocd_target == "stm32f103c8",
          f1.describe() if f1 else "缺失")
    check("内置包加载 f411ceu6 几何",
          f4 is not None and f4.flash_kib == 512 and f4.app_base == 0x08010000
          and f4.app_size == 0x70000 and f4.pyocd_target == "stm32f411ce"
          and f4.erase_ms() == 4035, f4.describe() if f4 else "缺失")
    fw = bc.FIRMWARE_ROOT_SIBLING
    if os.path.isdir(os.path.join(fw, "chips")):
        man = bc.load_manifest_dir(fw)
        same = all(man[k].app_base == profs[k].app_base
                   and man[k].app_size == profs[k].app_size
                   and man[k].flash_kib == profs[k].flash_kib
                   for k in man if k in profs)
        check("内置包与固件仓 chips/*.json 同值（单事实源守卫）",
              same and set(man) <= set(profs) and len(man) >= 2, f"固件仓 {sorted(man)}")
        check("chips sync 幂等（重建文本与入库文件逐字节一致）",
              bc.bundle_text(man) == Path(bc.BUNDLE_PATH).read_text(encoding="utf-8"))
    else:
        check("固件仓不在同层，跳过同值/幂等检查（非失败）", True)
    bad = [("APP 区超出 Flash",
            _mk_chip(partitions={"bootloader": {"base": "0x08000000", "size": "0x4000"},
                                 "app": {"base": "0x08004000", "size": "0x20000"}})),
           ("APP 区起点早于 Flash 基址",
            _mk_chip(partitions={"bootloader": {"base": "0x08000000", "size": "0x4000"},
                                 "app": {"base": "0x07000000", "size": "0x8000"}})),
           ("APP 区非 4 字节对齐",
            _mk_chip(partitions={"bootloader": {"base": "0x08000000", "size": "0x4000"},
                                 "app": {"base": "0x08004000", "size": "0x8002"}})),
           ("缺 memory 子键", _mk_chip(memory={"flash_base": "0x08000000"})),
           ("缺 id", {"memory": {}}),
           ("非法十六进制",
            _mk_chip(memory={"flash_base": "0xZZ", "flash_size": "0x10000",
                             "sram_base": "0x20000000", "sram_size": "0x5000"}))]
    not_rejected = []
    for label, doc in bad:
        try:
            bc.validate_profile(bc.profile_from_chip_dict(doc, "t.json"))
            not_rejected.append(label)
        except bc.ChipError:
            pass
    check("几何自检拒绝 6 类非法档案", not not_rejected, "; ".join(not_rejected))


def t_detect():
    """芯片探查：容量指纹 / 未收录 / 同容量撞车走 VERIFY 只读探针 / 短响应 / 无响应。"""
    profs = bc.load_profiles(firmware_root=_no_fw_root(), cfg={})
    quiet = lambda m: None

    def info_only(kib):
        return make_echo_bl(lambda c, s, d: info_payload(flash_kib=kib)
                            if c == C_INFO else None)

    d = bc.detect_chip(info_only(64), profs, log=quiet)
    check("探查：64 KiB → f103c8t6（容量指纹）",
          d.ok and d.profile.id == "f103c8t6" and d.method == "flash", d.reason)
    d = bc.detect_chip(info_only(512), profs, log=quiet)
    check("探查：512 KiB → f411ceu6（容量指纹）",
          d.ok and d.profile.id == "f411ceu6", d.reason)
    d = bc.detect_chip(info_only(256), profs, log=quiet)
    check("探查：未收录容量 → 明确失败（不猜）",
          (not d.ok) and "无匹配" in d.reason, d.reason)
    d = bc.detect_chip(make_echo_bl(lambda c, s, dd: bytes([0x03])
                                    if c == C_INFO else None), profs, log=quiet)
    check("探查：短响应 → 判为 APP 而非 BL", (not d.ok) and "APP" in d.reason, d.reason)
    d = bc.detect_chip(make_echo_bl(lambda c, s, dd: None), profs, log=quiet)
    check("探查：无响应 → 失败", (not d.ok) and "无响应" in d.reason, d.reason)

    def twin(pid, app_size):
        return bc.ChipProfile(id=pid, name=pid, flash_base=0x08000000, flash_kib=64,
                              sram_base=0x20000000, sram_size=0x5000,
                              app_base=0x08004000, app_size=app_size,
                              erase_entries=[{"size": 1024, "count": 64,
                                              "typical_erase_ms": 4}])
    twins = {"twin-s": twin("twin-s", 0x8000), "twin-b": twin("twin-b", 0x9000)}
    seen = []

    def probe_handler(c, s, payload):
        seen.append(c)
        if c == C_INFO:
            return info_payload(flash_kib=64)
        if c == C_VERIFY:
            size = struct.unpack_from("<I", payload, 0)[0]
            st = bc.ST_RANGE_ERROR if size > 0x8000 else 0x01      # 0x01 = CRC_ERROR
            return bytes([st]) + b"\x00" * 8
        return None

    d = bc.detect_chip(make_echo_bl(probe_handler), twins, log=quiet)
    check("探查：同容量撞车 → VERIFY 只读探针命中装得下的那片",
          d.ok and d.profile.id == "twin-s" and d.method == "probe", d.reason)
    check("探查：消歧全程无写命令（只读）", C_WRITE not in seen, str(seen))
    d = bc.detect_chip(make_echo_bl(probe_handler), twins, allow_probe=False, log=quiet)
    check("探查：探针关闭时如实报歧义（不猜）",
          (not d.ok) and d.method == "ambiguous", d.reason)


def t_image_check():
    """镜像体检：向量表与大小上限 + --force 应急口 + 真实镜像交叉校验。"""
    profs = bc.load_profiles(firmware_root=_no_fw_root(), cfg={})
    f1 = profs["f103c8t6"]

    def vec(msp, rh, size=64):
        return struct.pack("<II", msp, rh) + b"\x00" * (size - 8)

    with tempfile.TemporaryDirectory() as td:
        def put(name, data):
            p = os.path.join(td, name)
            Path(p).write_bytes(data)
            return p

        good = put("good.bin", vec(0x20005000, 0x08004105))
        c = bc.check_image(good, f1)
        check("镜像体检：合法镜像通过（大小/CRC 与升级口径一致）",
              c.ok and c.size == 64
              and c.crc32 == (zlib.crc32(Path(good).read_bytes()) & 0xFFFFFFFF), c.msg)
        c = bc.check_image(put("msp.bin", vec(0x30000000, 0x08004105)), f1)
        check("镜像体检：MSP 越界拒绝", (not c.ok) and "SRAM" in c.msg, c.msg)
        c = bc.check_image(put("thumb.bin", vec(0x20005000, 0x08004104)), f1)
        check("镜像体检：Reset Handler 缺 Thumb 位拒绝",
              (not c.ok) and "Thumb" in c.msg, c.msg)
        c = bc.check_image(put("bl.bin", vec(0x20000be8, 0x08000105)), f1)
        check("镜像体检：他分区镜像（如 BootLoader 自身）拒绝",
              (not c.ok) and "APP 区" in c.msg, c.msg)
        c = bc.check_image(put("big.bin", b"\x00" * (f1.app_size + 4)), f1)
        check("镜像体检：超出芯片 APP 区拒绝", (not c.ok) and "超出" in c.msg, c.msg)
        c = bc.check_image(put("empty.bin", b""), f1)
        check("镜像体检：空镜像拒绝", (not c.ok) and "空" in c.msg, c.msg)
        c = bc.check_image(os.path.join(td, "nope.bin"), f1)
        check("镜像体检：文件不存在拒绝", (not c.ok) and "读取失败" in c.msg, c.msg)
        c = bc.check_image(put("force.bin", vec(0x20005000, 0x08000105)), f1, force=True)
        check("镜像体检：--force 应急口放行并写明原因", c.ok and "跳过" in c.msg, c.msg)

    S = os.path.join(bc.FIRMWARE_ROOT_SIBLING, "app", "examples")
    real103 = os.path.join(S, "f103c8t6_app", "app.bin")
    real411 = os.path.join(S, "f411ceu6_app", "app.bin")
    real_bl = os.path.join(bc.FIRMWARE_ROOT_SIBLING, "bootloader.bin")
    if os.path.isfile(real103) and os.path.isfile(real411):
        check("真实 F103 APP 过 F103 档案体检", bc.check_image(real103, f1).ok)
        check("真实 F411 APP 过 F411 档案体检",
              bc.check_image(real411, profs["f411ceu6"]).ok)
        check("真实 F411 APP 被 F103 档案拒绝（防烧错芯片）",
              not bc.check_image(real411, f1).ok)
        check("真实 BootLoader 镜像被拒（不能塞进 APP 区）",
              not bc.check_image(real_bl, f1).ok)
    else:
        check("固件仓不在同层，跳过真实镜像交叉校验（非失败）", True)


def t_upgrade_bounds():
    """镜像上限随档案放宽 + 停止请求不落一字节（1.5.0）。"""
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "big.bin")
        Path(p).write_bytes(b"\xAA" * (blp.APP_SIZE + 1))
        try:
            blp.run_upgrade(make_echo_bl(lambda *a: None), p, log=lambda *a: None)
            check("镜像上限：默认 APP_SIZE（F103 46 KiB）拒绝超限", False, "未抛出")
        except ValueError as e:
            check("镜像上限：默认 APP_SIZE（F103 46 KiB）拒绝超限",
                  str(blp.APP_SIZE) in str(e), str(e))
        old = blp.RETRY_DELAY
        blp.RETRY_DELAY = 0
        try:
            bl = make_echo_bl(lambda *a: None)
            rc = blp.run_upgrade(bl, p, log=lambda *a: None, app_size=0x10000)
            check("镜像上限：传档案 app_size 后放行（继续到握手）",
                  rc == 1 and bl.s.wrote != b"", f"rc={rc}")
        finally:
            blp.RETRY_DELAY = old
        bl = make_echo_bl(lambda *a: None)
        rc = blp.run_upgrade(bl, p, log=lambda *a: None, app_size=0x10000,
                             should_stop=lambda: True)
        check("停止请求：升级前中止且未写一字节",
              rc == 1 and bl.s.wrote == b"", f"rc={rc} wrote={len(bl.s.wrote)}B")


def t_factory_engine():
    """批量引擎（全假串口驱动）：单台全流程 / 同 UID 跳过 / 已是最新跳过 /
    缺预设停批 / 未收录芯片 / 校验失败 / 软停 / 硬停 / CSV+JSONL 落盘与断点续烧。"""
    f1 = bc.load_profiles(firmware_root=_no_fw_root(), cfg={})["f103c8t6"]
    uid_a = bytes.fromhex("A1" * 12)
    uid_b = bytes.fromhex("B2" * 12)
    img = struct.pack("<II", 0x20005000, 0x08004105) + b"\x00" * 56   # 64B 合法 APP 镜像
    img_crc = zlib.crc32(img) & 0xFFFFFFFF
    quiet = lambda *a: None       # run_upgrade/impl 的 log 回调按 print 语义可多参

    def make_session(uid=uid_a, app_valid=0, app_crc=0, verify_st=0, records=None,
                     **optkw):
        state = {"writes": 0, "verify": 0, "jump": 0, "set_meta": 0,
                 "uid": uid if isinstance(uid, dict) else {"v": uid}}

        def handler(c, s, payload):
            if c == C_INFO:
                return info_payload(uid=state["uid"]["v"], flash_kib=64,
                                    app_valid=app_valid, app_size=len(img),
                                    app_crc=app_crc)
            if c == C_PING:
                return bytes([0x00, 0x01])
            if c == C_ERASE:
                return bytes([0x00])
            if c == C_WRITE:
                state["writes"] += 1
                return bytes([0x00])
            if c == C_VERIFY:
                state["verify"] += 1
                return bytes([verify_st]) + struct.pack("<II", img_crc, len(img))
            if c == C_JUMP:
                state["jump"] += 1
                return bytes([0x00])
            if c == C_SET_META:
                state["set_meta"] += 1
                return bytes([0x00])
            return None

        bl = make_echo_bl(handler)
        opts = bf.Options(chip_id="auto", trigger="newport",
                          image_map={"f103c8t6": path_bin}, **optkw)
        sess = bf.BatchSession({"f103c8t6": f1}, opts, open_fn=lambda p: bl,
                               records=records, log=quiet)
        # 默认端口脚本：第 2 轮出现 COM10（白名单口）→ 触发一台；之后无新口
        sess._list_ports = PortScript([["COM9"], ["COM9", "COM10"]])
        return sess, bl, state

    with tempfile.TemporaryDirectory() as td:
        path_bin = os.path.join(td, "app.bin")
        Path(path_bin).write_bytes(img)

        sess, bl, st = make_session(count=1)
        summary = sess.run("COM10")
        check("批量：单台成功（探查→体检→擦写→校验→跳转）",
              summary.ok == 1 and summary.fail == 0 and summary.total == 1
              and st["writes"] == 1 and st["verify"] == 1 and st["jump"] == 1,
              str(summary))

        sess, bl, st = make_session(count=2)
        # newport 模式：白名单端口「消失再出现」= 换板（每板一个转换器/板载 CDC）
        sess._list_ports = PortScript([["COM9"], ["COM9", "COM10"],
                                       ["COM9", "COM10"], ["COM9"],
                                       ["COM9", "COM10"]])
        summary = sess.run("COM10")
        check("批量：同 UID 复烧 → 跳过且不再写入",
              summary.ok == 1 and summary.skip == 1 and st["writes"] == 1
              and summary.fail == 0, str(summary))

        # 白名单：非白名单新口不触发（防误刷无关 CDC 设备）
        sess, bl, st = make_session(count=1)
        sess._list_ports = PortScript([["COM9"], ["COM9", "COM11"]])
        sess._sleep = lambda s: sess.request_stop()   # 首轮空转即停，避免无限等待
        summary = sess.run("COM10")
        check("批量：非白名单新口不触发（防误刷无关 CDC）",
              summary.total == 0 and st["writes"] == 0, str(summary))

        # 同端口轮询（默认模式）：换板靠 UID 变化识别
        sess, bl, st = make_session(count=2)
        sess.opt.trigger = "poll"
        calls = {"n": 0}

        def swap_sleep(sec):
            calls["n"] += 1
            if calls["n"] == 1:
                st["uid"]["v"] = uid_b            # 模拟换板：UID 变
        sess._sleep = swap_sleep
        summary = sess.run("COM10")
        check("批量：同端口轮询换板（UID 变化 → 第 2 台）",
              summary.ok == 2 and st["writes"] == 2 and summary.total == 2,
              str(summary))

        sess, bl, st = make_session(count=1, app_valid=1, app_crc=img_crc)
        summary = sess.run("COM10")
        check("批量：设备已是本镜像 → 跳过且不写入",
              summary.skip == 1 and st["writes"] == 0 and st["verify"] == 0,
              str(summary))

        sess, bl, st = make_session(count=1)
        sess.opt.image_map = {}
        summary = sess.run("COM10")
        check("批量：芯片无预设镜像 → 停批且不写入（绝不猜镜像）",
              summary.fail == 1 and bool(summary.halted) and st["writes"] == 0,
              f"{summary} halted={summary.halted!r}")

        sess, bl, st = make_session(count=1)
        bl.s.handler = lambda c, s, payload=None: (info_payload(flash_kib=256)
                                                   if c == C_INFO else None)
        summary = sess.run("COM10")
        check("批量：未收录芯片 → 该台 FAIL（不误烧）",
              summary.fail == 1 and not summary.halted and st["writes"] == 0,
              str(summary))

        sess, bl, st = make_session(count=1, verify_st=0x01)
        summary = sess.run("COM10")
        check("批量：校验失败 → 该台 FAIL 且计数正确",
              summary.fail == 1 and summary.ok == 0 and st["verify"] >= 1,
              str(summary))

        sess, bl, st = make_session()
        sess.request_stop()
        summary = sess.run("COM10")
        check("批量：软停生效（开工前请求 → 0 台）", summary.total == 0, str(summary))

        # 升级进行中请求立即停止（擦除已发、写块未发）→ 该台 FAIL 且未写入
        sess, bl, st = make_session(count=1)
        base_handler = bl.s.handler

        def stopping_handler(c, s, payload):
            if c == C_ERASE:
                sess.request_stop(immediate=True)
            return base_handler(c, s, payload)

        bl.s.handler = stopping_handler
        summary = sess.run("COM10")
        check("批量：升级中立即停止 → 不写入且该台 FAIL",
              summary.fail == 1 and summary.total == 1 and st["writes"] == 0,
              str(summary))

        rec = os.path.join(td, "records.csv")
        sess, bl, st = make_session(count=1, records=bf.RecordWriter(rec, None))
        sess.run("COM10")
        rows = list(csv.DictReader(open(rec, encoding="utf-8-sig")))
        check("批量：CSV 落盘字段完整（UID/镜像/结果）",
              len(rows) == 1 and rows[0]["uid"] == uid_a.hex().upper()
              and rows[0]["result"] == "OK" and rows[0]["chip_id"] == "f103c8t6"
              and rows[0]["image_crc32"] == f"{img_crc:08X}", str(rows[:1]))

        sess, bl, st = make_session(count=1, uid=uid_a, resume=True,
                                    records=bf.RecordWriter(rec, None))
        summary = sess.run("COM10")
        check("批量：断点续烧按既有记录跳过已烧 UID",
              summary.skip == 1 and st["writes"] == 0, str(summary))

        sess, bl, st = make_session(count=1, uid=uid_b, resume=True,
                                    records=bf.RecordWriter(rec, None))
        summary = sess.run("COM10")
        check("批量：续烧只跳已烧过的 UID（新 UID 照烧）",
              summary.ok == 1 and st["writes"] == 1, str(summary))

        jl = os.path.join(td, "records.jsonl")
        sess, bl, st = make_session(count=1, uid=uid_b,
                                    records=bf.RecordWriter(None, jl))
        sess.run("COM10")
        lines = [ln for ln in Path(jl).read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        import json as _json
        jrec = _json.loads(lines[0]) if lines else {}
        check("批量：JSONL 落盘可解析", len(lines) == 1
              and jrec.get("result") == "OK" and jrec.get("chip_id") == "f103c8t6",
              str(lines[:1]))


def t_gui_modes():
    """GUI 模式矩阵：工厂与高级互相独立、可共存、互不夹带（1.5.0）。"""
    import tkinter as tk

    import bl_upgrade_gui as gui
    try:
        probe = tk.Tk()
        probe.destroy()
    except tk.TclError:
        check("GUI 模式矩阵（无 Tk 环境，跳过非失败）", True)
        return
    combos = [(False, False, False, False), (True, False, True, False),
              (False, True, False, True), (True, True, True, True)]
    for adv, fac, want_adv, want_fac in combos:
        root = tk.Tk()
        root.withdraw()
        try:
            app = gui.App(root, advanced=adv, factory=fac)
            root.update()
            has_adv = hasattr(app, "v_size")
            has_fac = hasattr(app, "unit_tv")
            has_ops = hasattr(app, "btn_upgrade")
            ok = (has_adv == want_adv and has_fac == want_fac
                  and has_ops == (not fac))
            if fac:
                ok = ok and app.btn_f_start in app.lock_btns \
                     and app.btn_f_stop not in app.lock_btns
            # 版式：内容必须装得进窗口（宽度不足会静默裁掉右侧控件——1.5.0 修复项）
            rw, rh = root.winfo_reqwidth(), root.winfo_reqheight()
            w, h = root.winfo_width(), root.winfo_height()
            fit = rw <= w and rh <= h
            detail = (f"高级面板={has_adv} 工厂面板={has_fac} 基础操作={has_ops}；"
                      f"版式 内容{rw}x{rh} ≤ 窗口{w}x{h} {'✓' if fit else '✗ 溢出'}")
            ok = ok and fit
        finally:
            app._closing = True
            root.destroy()
        check(f"GUI 模式矩阵 advanced={adv} factory={fac}", ok, detail)

    root = tk.Tk()
    root.withdraw()
    try:
        app = gui.App(root, advanced=False, factory=True)
        app.baud_var.set("9600")          # 高级面板的值：工厂侧一律不读
        app.pace_var.set("77")
        app.key_var.set("adv-only.pem")
        app.f_trigger.set("新串口出现")
        opts = app._factory_options()
        ok = (opts.key_path is None and opts.chip_id == "auto"
              and opts.trigger == "newport" and opts.app_version is None
              and opts.skip_uptodate is True)
    finally:
        root.destroy()
    check("GUI 工厂选项只取工厂面板值（不读高级的波特率/pace/私钥）", ok,
          f"key={opts.key_path!r} trigger={opts.trigger}")

    root = tk.Tk()
    root.withdraw()
    try:
        app = gui.App(root, advanced=False, factory=True)
        # 结果表行号跨会话唯一：两次会话的 seq 都从 1 开始，不能互相覆盖
        row = {"seq": 1, "started": "t1", "chip_id": "f103c8t6", "uid": "AAAA",
               "image": "a.bin", "result": "OK", "elapsed_s": "1.0", "reason": "x"}
        app._unit_row(row, start=False)
        app._row_map = {}          # 模拟第二次开工时的映射重置（计数/表保留）
        app._unit_row(dict(row, started="t2", uid="BBBB"), start=False)
        rows = app.unit_tv.get_children()
        ok = (len(rows) == 2
              and app.unit_tv.item(rows[0], "values")[3] == "AAAA"
              and app.unit_tv.item(rows[1], "values")[3] == "BBBB")
        check("GUI 结果表：两次会话的行互不覆盖", ok, str(rows))
        # 开工校验失败（未选串口）不得动既有计数
        app.f_counts = {"ok": 3, "skip": 1, "fail": 0}
        app._render_counters()
        app.port_var.set("")
        app.do_batch_start()
        check("GUI 开工不清空既有计数（清空有专用按钮）",
              app.f_counts == {"ok": 3, "skip": 1, "fail": 0}
              and len(app.unit_tv.get_children()) == 2, str(app.f_counts))
    finally:
        app._closing = True
        root.destroy()

    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "state.json")
        gui.write_state({"advanced": True, "factory": False}, p)
        gui.update_state(p, factory=True)
        st = gui.read_state(p)
        check("状态：两模式键可共存（工厂+高级同时开）",
              st.get("advanced") is True and st.get("factory") is True)
        gui.update_state(p, advanced=False)
        st = gui.read_state(p)
        check("状态：翻一个键不动另一个",
              st.get("advanced") is False and st.get("factory") is True, str(st))
        bad = os.path.join(td, "bad.json")
        Path(bad).write_text("nope{{", encoding="utf-8")
        gui.update_state(bad, factory=True)
        check("状态：损坏文件可恢复写入", gui.read_state(bad).get("factory") is True)


def t_drill_params():
    """钻具 pyocd 参数化（1.5.0）：pack_id → 本地 pack 目录推导 + argv 组装（无 shell）。"""
    import bl_powerloss_drill as bpd
    check("钻具：pack 目录不存在时返回 None（不误指路径）",
          bpd.pack_path_for("Keil.STM32F1xx_DFP.2.4.1", "Z:/no/such/root") is None)
    check("钻具：pack_id 形状非法返回 None",
          bpd.pack_path_for("bad", "Z:/x") is None
          and bpd.pack_path_for("", "Z:/x") is None)
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "STM32F1xx_DFP", "2.4.1"))
        got = bpd.pack_path_for("Keil.STM32F1xx_DFP.2.4.1", td)
        check("钻具：pack_id → <root>/<name>/<version> 推导",
              got is not None
              and got.endswith(os.path.join("STM32F1xx_DFP", "2.4.1")), str(got))
    cmd = bpd.pyocd_cmd_for("stm32f411ce", None)
    check("钻具：argv 组装（无 pack 时省略 --pack）",
          cmd[-1] == "stm32f411ce" and "--pack" not in cmd, " ".join(cmd))
    cmd = bpd.pyocd_cmd_for("stm32f103c8", "C:/packs/x")
    check("钻具：argv 组装（带 pack 且走 argv 无 shell）",
          cmd[-2:] == ["--pack", "C:/packs/x"] and cmd[cmd.index("-t") + 1]
          == "stm32f103c8", " ".join(cmd))
    check("钻具：F103 档案目标名与旧硬编码一致（无行为回归）",
          bc.load_profiles(firmware_root=_no_fw_root(),
                           cfg={})["f103c8t6"].pyocd_target == "stm32f103c8")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    t_crc16()
    t_build_frame()
    t_recv_frame()
    t_parse_info()
    t_info_fields()
    t_parse_meta()
    t_parse_ota()
    t_cmd_seq()
    t_misc()
    t_signing()
    t_cli()
    t_cli_extra()
    t_gui_state()
    t_chip_profiles()
    t_detect()
    t_image_check()
    t_upgrade_bounds()
    t_factory_engine()
    t_drill_params()
    t_gui_modes()
    n = sum(RESULTS)
    print(f"== 主机侧单测：{n}/{len(RESULTS)} 通过 ==")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
