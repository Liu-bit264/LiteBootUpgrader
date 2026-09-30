#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bl_upgrade_gui.py — LiteBootLoader 上位机图形界面（版本随 bl_upgrade.VERSION）

封装 bl_upgrade.py（协议流程的唯一实现；协议规范见 LiteBootLoader 仓库 docs/protocol.md）。

两种界面模式（状态记录在 ~/.litebootupgrader_gui.json，勾选"高级模式"→ 弹窗确认 →
窗口级重启（销毁旧窗口、按新模式重建）后生效；取消勾选同样确认后重启返回基础模式）：
  连接类型：串口框内下拉选择「有线串口 / 蓝牙 HC-05」，经 blp.BootLoader(conn=...) 传导；
  基础模式：一键升级（对端在跑 APP 时自动"请求回 BL"→ 擦除 → 写入 → 校验，带进度条）、
            跳转 APP、复位、PING，操作日志实时滚动；
  高级模式：额外暴露 CLI 全量功能——INFO / META / OTA 查询 / ERASE / SELFTEST / VERIFY /
            LISTEN / RAW / SETMETA，以及波特率与 pace(ms) 参数（影响本面板全部操作）。

运行（依赖隔离，勿直接 pip install）：
  uv run --python 3.12 --with pyserial bl_upgrade_gui.py
  或双击 bl_upgrade_gui.bat

线程模型：Tk 主线程只做 UI；每个操作开一个工作线程，经 queue 回传
日志/进度，主线程 root.after 轮询刷 UI（Tkinter 非线程安全，
工作线程禁止触碰控件；串口参数在主线程取好，线程间只传纯 Python 值）。
selftest 的 print 经 redirect_stdout 桥接进日志队列（busy 互斥保证
同一时刻仅一个工作线程在跑，stdout 重换向不串扰）。
"""
import contextlib
import io
import json
import os
import queue
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import bl_upgrade as blp

APP_TITLE = f"LiteBootLoader 升级工具 v{blp.VERSION}"
DEFAULT_BAUD = 115200
FOLLOW = {"jump": 2.5, "reset": 6.0, "ping": 0.3}
STATE_FILE = os.path.join(os.path.expanduser("~"), ".litebootupgrader_gui.json")

# 邻居主仓的示例镜像（存在则预填，纯便利不考虑强依赖）
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_FAMILY_DIR = os.path.dirname(_THIS_DIR)          # 三仓共同父目录
SIBLING_APP = os.path.join(_FAMILY_DIR, "LiteBootLoader",
                           "app", "examples", "f103c8t6_app", "app.bin")


def read_state(path: str = STATE_FILE) -> dict:
    """读取界面模式状态；文件缺失/损坏一律回落空 dict（= 基础模式）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state: dict, path: str = STATE_FILE) -> bool:
    try:
        Path(path).write_text(json.dumps(state), encoding="utf-8")
        return True
    except OSError:
        return False


class _QueueWriter:
    """stdout 桥：print 文本转日志队列（供 selftest 在 GUI 内运行）。"""

    def __init__(self, put):
        self._put = put

    def write(self, s):
        if s and s.strip():
            self._put(s.rstrip("\n"))
        return len(s)

    def flush(self):
        pass


class App:
    def __init__(self, root: tk.Tk, advanced: bool = False):
        self.root = root
        self.advanced = advanced
        self.restart_pending = False
        root.title(APP_TITLE)
        if advanced:
            root.geometry("680x800")
            root.minsize(560, 700)
        else:
            root.geometry("680x540")
            root.minsize(560, 460)

        self.q = queue.Queue()
        self.busy = False
        self.worker = None
        self.lock_btns = []
        self.baud_var = tk.StringVar(value=str(DEFAULT_BAUD))
        self.pace_var = tk.StringVar(value="0")
        self.key_var = tk.StringVar()   # 签名私钥 PEM（可选，ADR-020）
        self.cur_baud = DEFAULT_BAUD   # 主线程 start() 时取好，工作线程只读纯值
        self.cur_pace = 0

        top = ttk.LabelFrame(root, text="串口（8N1）")
        top.pack(fill="x", padx=8, pady=(8, 4))
        self.port_var = tk.StringVar()
        self.port_cb = ttk.Combobox(top, textvariable=self.port_var,
                                    width=10, state="readonly")
        self.port_cb.pack(side="left", padx=6, pady=6)
        ttk.Button(top, text="刷新", command=self.refresh_ports)\
            .pack(side="left", padx=2)
        ttk.Label(top, text="连接").pack(side="left", padx=(10, 2))
        self.conn_var = tk.StringVar(value="有线串口")
        ttk.Combobox(top, textvariable=self.conn_var, width=9, state="readonly",
                     values=["有线串口", "蓝牙 HC-05"]).pack(side="left")
        ttk.Label(top, text="串口与 VOFA+/串口助手互斥")\
            .pack(side="left", padx=12)

        img = ttk.LabelFrame(root, text="APP 镜像（.bin，≤46 KiB，自动补齐 4 字节对齐）")
        img.pack(fill="x", padx=8, pady=4)
        self.img_var = tk.StringVar()
        ttk.Entry(img, textvariable=self.img_var, state="readonly")\
            .pack(side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(img, text="浏览…", command=self.pick_image)\
            .pack(side="left", padx=2)
        self.img_info = tk.StringVar(value="未选择镜像")
        ttk.Label(img, textvariable=self.img_info, width=34)\
            .pack(side="left", padx=6)

        ops = ttk.LabelFrame(root, text="操作")
        ops.pack(fill="x", padx=8, pady=4)
        self.btn_upgrade = ttk.Button(ops, text="一键升级", command=self.do_upgrade)
        self.btn_jump = ttk.Button(ops, text="跳转 APP", command=lambda: self.do_quick("jump"))
        self.btn_reset = ttk.Button(ops, text="复位", command=lambda: self.do_quick("reset"))
        self.btn_ping = ttk.Button(ops, text="PING", command=lambda: self.do_quick("ping"))
        for i, b in enumerate((self.btn_upgrade, self.btn_jump,
                               self.btn_reset, self.btn_ping)):
            b.pack(side="left", padx=6, pady=6)
        ttk.Label(ops, text="对端在跑 APP 会自动请求回 BL")\
            .pack(side="left", padx=12)
        self.lock_btns += [self.btn_upgrade, self.btn_jump,
                           self.btn_reset, self.btn_ping]

        if advanced:
            self._build_advanced(root)

        self.adv_var = tk.BooleanVar(value=advanced)
        ttk.Checkbutton(root, text="高级模式（全量 CLI 功能；切换将弹窗确认并重启界面）",
                        variable=self.adv_var,
                        command=self.toggle_advanced)\
            .pack(anchor="w", padx=10, pady=(2, 0))

        self.progress = ttk.Progressbar(root, maximum=100)
        self.progress.pack(fill="x", padx=8, pady=2)
        self.status = tk.StringVar(value="空闲")
        ttk.Label(root, textvariable=self.status, anchor="w")\
            .pack(fill="x", padx=10)

        logf = ttk.LabelFrame(root, text="日志")
        logf.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.log_text = tk.Text(logf, height=14, state="disabled",
                                font=("Consolas", 9), wrap="none")
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

        self.refresh_ports()
        if os.path.isfile(SIBLING_APP):
            self.img_var.set(os.path.normpath(SIBLING_APP))
            self.update_img_info()
        self.root.after(80, self.pump)

    # ---- 高级面板（仅高级模式构建） ----
    def _build_advanced(self, root):
        adv = ttk.LabelFrame(root, text="高级操作（CLI 全量功能）")
        adv.pack(fill="x", padx=8, pady=4)

        r0 = ttk.Frame(adv)
        r0.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(r0, text="波特率").pack(side="left", padx=(4, 2))
        ttk.Entry(r0, textvariable=self.baud_var, width=8).pack(side="left")
        ttk.Label(r0, text="pace(ms)").pack(side="left", padx=(12, 2))
        ttk.Entry(r0, textvariable=self.pace_var, width=6).pack(side="left")
        ttk.Label(r0, text="影响本面板全部操作与串口打开").pack(side="left", padx=10)

        r1 = ttk.Frame(adv)
        r1.pack(fill="x", padx=4, pady=2)
        b_info = ttk.Button(r1, text="INFO", command=self.do_info)
        b_meta = ttk.Button(r1, text="META", command=self.do_meta)
        b_ota = ttk.Button(r1, text="OTA 查询", command=self.do_ota)
        b_erase = ttk.Button(r1, text="ERASE（危险）", command=self.do_erase)
        b_selftest = ttk.Button(r1, text="SELFTEST", command=self.do_selftest)
        for b in (b_info, b_meta, b_ota, b_erase, b_selftest):
            b.pack(side="left", padx=4, pady=2)
        self.lock_btns += [b_info, b_meta, b_ota, b_erase, b_selftest]

        rsign = ttk.Frame(adv)
        rsign.pack(fill="x", padx=4, pady=2)
        ttk.Label(rsign, text="签名私钥（可选）").pack(side="left", padx=(4, 2))
        ttk.Entry(rsign, textvariable=self.key_var, width=36).pack(side="left")
        ttk.Button(rsign, text="浏览…", command=self.browse_key).pack(side="left", padx=2)
        ttk.Button(rsign, text="生成密钥对", command=self.do_keygen).pack(side="left", padx=6)
        ttk.Label(rsign, text="留空 = legacy VERIFY；填私钥 = VERIFY_SIGNED（固件需 BL_SIGN_EN=1）",
                  foreground="#888").pack(side="left", padx=6)

        r2 = ttk.Frame(adv)
        r2.pack(fill="x", padx=4, pady=2)
        ttk.Label(r2, text="VERIFY size").pack(side="left", padx=(4, 2))
        self.v_size = tk.StringVar()
        ttk.Entry(r2, textvariable=self.v_size, width=9).pack(side="left")
        ttk.Label(r2, text="crc32(hex)").pack(side="left", padx=(8, 2))
        self.v_crc = tk.StringVar()
        ttk.Entry(r2, textvariable=self.v_crc, width=11).pack(side="left")
        b_verify = ttk.Button(r2, text="VERIFY", command=self.do_verify)
        b_verify.pack(side="left", padx=6)
        self.lock_btns.append(b_verify)

        r3 = ttk.Frame(adv)
        r3.pack(fill="x", padx=4, pady=2)
        ttk.Label(r3, text="LISTEN 秒").pack(side="left", padx=(4, 2))
        self.v_listen = tk.StringVar(value="3")
        ttk.Entry(r3, textvariable=self.v_listen, width=5).pack(side="left")
        b_listen = ttk.Button(r3, text="LISTEN", command=self.do_listen)
        b_listen.pack(side="left", padx=6)
        ttk.Label(r3, text="RAW hex").pack(side="left", padx=(14, 2))
        self.v_raw = tk.StringVar()
        ttk.Entry(r3, textvariable=self.v_raw, width=14).pack(side="left")
        b_raw = ttk.Button(r3, text="RAW", command=self.do_raw)
        b_raw.pack(side="left", padx=6)
        self.lock_btns += [b_listen, b_raw]

        r4 = ttk.Frame(adv)
        r4.pack(fill="x", padx=4, pady=(2, 6))
        ttk.Label(r4, text="SETMETA field(hex)").pack(side="left", padx=(4, 2))
        self.v_meta_f = tk.StringVar()
        ttk.Entry(r4, textvariable=self.v_meta_f, width=5).pack(side="left")
        ttk.Label(r4, text="value(hex)").pack(side="left", padx=(8, 2))
        self.v_meta_v = tk.StringVar()
        ttk.Entry(r4, textvariable=self.v_meta_v, width=5).pack(side="left")
        b_setmeta = ttk.Button(r4, text="SETMETA", command=self.do_setmeta)
        b_setmeta.pack(side="left", padx=6)
        self.lock_btns.append(b_setmeta)

    # ---- UI 辅助（主线程） ----
    def refresh_ports(self):
        import serial.tools.list_ports
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_cb["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def pick_image(self):
        p = filedialog.askopenfilename(
            title="选择 APP 镜像",
            filetypes=[("BIN 镜像", "*.bin"), ("所有文件", "*.*")])
        if p:
            self.img_var.set(p)
            self.update_img_info()

    def update_img_info(self):
        try:
            with open(self.img_var.get(), "rb") as f:
                img = f.read()
            if not 0 < len(img) <= blp.APP_SIZE:
                self.img_info.set(f"大小 {len(img)}B 超出 1B~{blp.APP_SIZE}B！")
                return
            if len(img) % 4:
                img += b"\xFF" * (4 - len(img) % 4)
            self.img_info.set(f"{len(img)} B · CRC32 {zlib.crc32(img) & 0xFFFFFFFF:#010x}")
        except OSError as e:
            self.img_info.set(f"读取失败：{e}")

    def set_busy(self, b: bool):
        self.busy = b
        for w in self.lock_btns:
            w.state(["disabled" if b else "!disabled"])

    def log_line(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip("\n") + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def pump(self):
        try:
            while True:
                kind, *payload = self.q.get_nowait()
                if kind == "log":
                    self.log_line(payload[0])
                elif kind == "status":
                    self.status.set(payload[0])
                elif kind == "progress":
                    done, total = payload
                    self.progress["value"] = done * 100 // max(total, 1)
                    self.status.set(f"写入 {done}/{total} B")
                elif kind == "done":
                    rc = payload[0]
                    self.progress["value"] = 100 if rc == 0 else self.progress["value"]
                    self.status.set("完成 ✓" if rc == 0 else f"失败（退出码 {rc}）")
                    self.set_busy(False)
        except queue.Empty:
            pass
        self.root.after(80, self.pump)

    # ---- 模式切换与界面重启（主线程） ----
    def toggle_advanced(self):
        want = self.adv_var.get()
        if want == self.advanced:
            return
        if want:
            msg = ("高级模式将显示全部 CLI 功能，含 ERASE（整片擦除 APP）、SETMETA"
                   "（写参数区）、RAW（发原始字节）等危险操作。\n\n确认重启界面进入高级模式？")
            title = "进入高级模式"
        else:
            msg = "返回基础模式（仅日常四操作），界面将重启。\n\n确认继续？"
            title = "返回基础模式"
        if messagebox.askyesno(title, msg):
            if not write_state({"advanced": want}):
                messagebox.showerror("切换失败",
                                     f"状态文件写入失败（{STATE_FILE}），未重启。")
                self.adv_var.set(self.advanced)
                return
            self.restart_pending = True
            self.root.destroy()          # main() 循环将按新模式重建窗口
        else:
            self.adv_var.set(self.advanced)   # 用户取消，勾选框回弹

    # ---- 操作入口（主线程，启线程） ----
    def _check_ready(self):
        if self.busy:
            return False
        if not self.port_var.get():
            self.log_line("[X] 请先选择串口（点“刷新”枚举）")
            return False
        return True

    def browse_key(self):
        p = filedialog.askopenfilename(title="选择 ECDSA P-256 私钥 PEM",
                                       filetypes=[("PEM", "*.pem"), ("全部", "*.*")])
        if p:
            self.key_var.set(os.path.normpath(p))

    def do_keygen(self):
        pem = filedialog.asksaveasfilename(
            title="保存私钥 PEM（本地妥善保管，任何形态不入库）",
            defaultextension=".pem", initialfile="sign_test_key.pem")
        if not pem:
            return
        header = filedialog.asksaveasfilename(
            title="保存公钥本地头（写入固件仓芯片端口目录，须在 .gitignore）",
            defaultextension=".h", initialfile="bl_sign_pubkey_local.h")
        if not header:
            return

        def _work():
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = blp.cmd_keygen(pem, header)
                self._logcb(buf.getvalue().rstrip())
                self.q.put(("done", rc))
            except SystemExit as e:      # cmd_keygen 缺 cryptography 走 sys.exit
                self._logcb(str(e))
                self.q.put(("done", 1))
        self.start(_work)

    def do_upgrade(self):
        if not self._check_ready():
            return
        path = self.img_var.get()
        if not path or not os.path.isfile(path):
            self.log_line("[X] 请先选择镜像文件")
            return
        self.start(self._upgrade_worker, self.port_var.get(), path)

    def do_quick(self, op: str):
        if not self._check_ready():
            return
        self.start(self._quick_worker, self.port_var.get(), op)

    def _start_adv(self, fn):
        """高级操作统一入口：fn(bl) 在工作线程执行，Tk 变量已在此取好为纯值。"""
        if not self._check_ready():
            return
        self.start(self._adv_worker, self.port_var.get(), fn)

    def do_info(self):
        self._start_adv(self._op_info)

    def do_meta(self):
        self._start_adv(self._op_meta)

    def do_ota(self):
        self._start_adv(self._op_ota)

    def do_erase(self):
        if not self._check_ready():
            return
        if not messagebox.askyesno("确认擦除",
                                   "将整片擦除 APP 区（升级后需重新写入镜像）。\n\n确认继续？"):
            return
        self._start_adv(self._op_erase)

    def do_selftest(self):
        self._start_adv(self._op_selftest)

    def do_verify(self):
        if not self._check_ready():
            return
        size_s, crc_s = self.v_size.get().strip(), self.v_crc.get().strip()
        self._start_adv(lambda bl: self._op_verify(bl, size_s, crc_s))

    def do_listen(self):
        if not self._check_ready():
            return
        secs_s = self.v_listen.get().strip()
        self._start_adv(lambda bl: self._op_listen(bl, secs_s))

    def do_raw(self):
        if not self._check_ready():
            return
        hex_s = self.v_raw.get().strip()
        self._start_adv(lambda bl: self._op_raw(bl, hex_s))

    def do_setmeta(self):
        if not self._check_ready():
            return
        f_s, v_s = self.v_meta_f.get().strip(), self.v_meta_v.get().strip()
        self._start_adv(lambda bl: self._op_setmeta(bl, f_s, v_s))

    def start(self, fn, *args):
        try:
            self.cur_baud = int(self.baud_var.get())
        except ValueError:
            self.cur_baud = DEFAULT_BAUD
        try:
            self.cur_pace = int(self.pace_var.get())
        except ValueError:
            self.cur_pace = 0
        self.set_busy(True)
        self.progress["value"] = 0
        self.status.set("运行中…")
        self.worker = threading.Thread(target=fn, args=args, daemon=True)
        self.worker.start()

    # ---- 工作线程（只经 queue 与 UI 通信） ----
    def _logcb(self, *args):
        self.q.put(("log", " ".join(str(a) for a in args)))

    def _progresscb(self, done, total):
        self.q.put(("progress", done, total))

    def _open_and_close(self, port):
        """返回 BootLoader；调用方负责 close。波特率/pace/连接类型用主线程取好的纯值。"""
        conn = "bt" if "蓝牙" in self.conn_var.get() else "serial"
        return blp.BootLoader(port, self.cur_baud, self.cur_pace, conn=conn)

    def _drain_banner(self, bl: blp.BootLoader, secs: float):
        """收尾读取若干秒原始字节（横幅/日志），转文本进日志窗。"""
        deadline, text = time.time() + secs, ""
        while time.time() < deadline:
            chunk = bl.s.read(256)
            if chunk:
                text += chunk.decode("utf-8", "replace")
        if text.strip():
            self._logcb("--- 串口输出 ---\n" + text.strip()[:400])

    def _upgrade_worker(self, port: str, path: str):
        try:
            bl = self._open_and_close(port)
            try:
                rc = blp.run_upgrade(bl, path, log=self._logcb,
                                     progress=self._progresscb,
                                     key_path=self.key_var.get().strip() or None)
            finally:
                bl.s.close()
            self.q.put(("done", rc))
        except (Exception, SystemExit) as e:
            self._logcb(f"[X] {e}")
            self.q.put(("done", 1))

    def _quick_worker(self, port: str, op: str):
        try:
            bl = self._open_and_close(port)
            try:
                r = bl.cmd(op, timeout=2.0)
                if r is None:
                    self._logcb(f"{op}: 无响应（对端可能在跑 APP？PING 先探一下）")
                    self.q.put(("done", 1))
                    return
                st = r["data"][0]
                extra = ""
                if op == "ping" and st == 0:
                    extra = f" 协议版本 {r['data'][1]:#04x}"
                self._logcb(f"{op}: {blp.st_name(st)}{extra}")
                if st != 0:
                    self.q.put(("done", 1))
                    return
                self.q.put(("status", f"{op} OK，监听输出…"))
                self._drain_banner(bl, FOLLOW.get(op, 1.0))
                self.q.put(("done", 0))
            finally:
                bl.s.close()
        except (Exception, SystemExit) as e:
            self._logcb(f"[X] {e}")
            self.q.put(("done", 1))

    def _adv_worker(self, port: str, fn):
        """高级操作通用工作线程：fn(bl) 返回退出码（0=成功）。"""
        try:
            bl = self._open_and_close(port)
            try:
                rc = fn(bl)
                self.q.put(("done", rc if isinstance(rc, int) else 0))
            finally:
                bl.s.close()
        except (Exception, SystemExit) as e:
            self._logcb(f"[X] {e}")
            self.q.put(("done", 1))

    # ---- 高级操作实现（工作线程内；行为与 CLI main() 逐一对齐） ----
    def _op_info(self, bl):
        r = bl.cmd("info")
        self._logcb("info: 无响应" if r is None else blp.parse_info(r["data"]))
        return 0 if r is not None else 1

    def _op_meta(self, bl):
        r = bl.cmd("get_meta")
        if r is None:
            self._logcb("meta: 无响应")
            return 1
        self._logcb("meta: " + blp.meta_str(blp.parse_meta(r["data"])))
        return 0

    def _op_ota(self, bl):
        r = bl.cmd("ota", timeout=2.0)
        self._logcb("ota: 无响应" if r is None else blp.parse_ota(r["data"]))
        return 0 if r is not None else 1

    def _op_erase(self, bl):
        t0 = time.time()
        r = bl.cmd("erase", timeout=8.0)
        st = blp._resp_status(r, "erase")
        if st is None:
            self._logcb("erase: 无响应")
            return 1
        self._logcb(f"erase: {blp.st_name(st)} 耗时={time.time() - t0:.2f}s")
        return 0 if st == 0 else 1

    def _op_verify(self, bl, size_s: str, crc_s: str):
        try:
            size = int(size_s, 0)
            crc = int(crc_s, 16)
        except ValueError:
            self._logcb("[X] VERIFY 参数无效：size 十进制（或 0x 前缀），crc32 十六进制")
            return 1
        r = bl.cmd("verify", struct.pack("<II", size, crc), timeout=3.0)
        st = blp._resp_status(r, "verify")
        if st is None:
            self._logcb("verify: 无响应")
            return 1
        self._logcb(f"verify: {blp.st_name(st)}")
        return 0 if st == 0 else 1

    def _op_listen(self, bl, secs_s: str):
        try:
            secs = float(secs_s)
        except ValueError:
            secs = 3.0
        secs = max(0.0, min(secs, 60.0))
        t0, buf = time.time(), bytearray()
        while time.time() - t0 < secs:
            chunk = bl.s.read(256)
            if chunk:
                buf += chunk
        text = buf.decode("utf-8", "replace")
        if text.strip():
            self._logcb("--- 串口输出 ---\n" + text.strip()[:2000])
        self._logcb(f"({secs:.0f}s 监听结束)")
        return 0

    def _op_raw(self, bl, hex_s: str):
        try:
            data = bytes.fromhex(hex_s.replace(" ", ""))
        except ValueError:
            self._logcb("[X] RAW 参数需为偶数长度十六进制字节，如 42 或 AA 55")
            return 1
        if not data:
            self._logcb("[X] RAW：未提供字节")
            return 1
        bl.send_raw(data)
        self._logcb(f"已发送 {len(data)}B 原始字节")
        return 0

    def _op_setmeta(self, bl, f_s: str, v_s: str):
        try:
            field, value = int(f_s, 16), int(v_s, 16)
        except ValueError:
            self._logcb("[X] SETMETA 参数需为十六进制")
            return 1
        if not (0 <= field <= 0xFF and 0 <= value <= 0xFF):
            self._logcb("[X] SETMETA field/value 需为单字节（00–FF）")
            return 1
        r = bl.cmd("set_meta", bytes([field, value]))
        st = blp._resp_status(r, "set_meta")
        if st is None:
            self._logcb("set_meta: 无响应")
            return 1
        self._logcb(f"set_meta({f_s},{v_s}): {blp.st_name(st)}")
        if st != 0:
            return 1
        t0, buf = time.time(), bytearray()
        while time.time() - t0 < 3.0:
            chunk = bl.s.read(256)
            if chunk:
                buf += chunk
        text = buf.decode("utf-8", "replace")
        if text.strip():
            self._logcb("--- 后续串口输出 ---\n" + text.strip()[:2000])
        return 0

    def _op_selftest(self, bl):
        with contextlib.redirect_stdout(_QueueWriter(self._logcb)):
            rc = blp.selftest(bl)
        self._logcb(f"selftest 退出码 {rc}")
        return rc


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # 窗口级重启循环：toggle_advanced 写状态后销毁窗口，这里按新模式重建
    while True:
        advanced = bool(read_state().get("advanced", False))
        root = tk.Tk()
        app = App(root, advanced=advanced)
        root.mainloop()
        if not app.restart_pending:
            break


if __name__ == "__main__":
    main()
