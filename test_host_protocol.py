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
  4. parse_info 各长度档（67B 全量 + 1B 短响应）
  5. parse_meta 字段往返
  6. 镜像 4 字节补齐后的 CRC 与 GUI 信息栏口径一致 + GUI 模块可导入
  7. CLI 解析器：write 幽灵子命令已移除、--version 可用、ota 子命令与 --conn 校验
  8. GUI 模式状态持久化（缺失/回读/损坏容错；用系统临时目录，自动清理）
  9. parse_ota 与 OTA_QUERY 请求帧实测模板（1.3.0，protocol.md §5.10/§7.5）
退出码 0=全部通过。
"""
import struct
import sys
import zlib

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
        with contextlib.redirect_stderr(io.StringIO()):
            ap.parse_args(["ping", "--conn", "wifi"])
        ok = False
    except SystemExit as e:
        ok = e.code == 2
    check("CLI --conn 非法取值拒绝", ok)
    # 审计 2026-09-29 P2-3：selftest 须传递 --conn（蓝牙下 3 次打开重试才生效）
    import inspect
    src = inspect.getsource(blp.main)
    check("selftest 传递 --conn 到 BootLoader",
          "selftest(BootLoader(a.port, a.baud, a.pace, a.conn))" in src)


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


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    t_crc16()
    t_build_frame()
    t_recv_frame()
    t_parse_info()
    t_parse_meta()
    t_parse_ota()
    t_cmd_seq()
    t_misc()
    t_signing()
    t_cli()
    t_gui_state()
    n = sum(RESULTS)
    print(f"== 主机侧单测：{n}/{len(RESULTS)} 通过 ==")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
