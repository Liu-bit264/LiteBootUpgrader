#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bl_upgrade_gui.py — LiteBootLoader 上位机图形界面 v1.1.0

封装 bl_upgrade.py（协议流程的唯一实现；协议规范见主仓 LiteBootLoader/docs/protocol.md）。
功能：一键升级（自动"请求回 BL"→ 擦除 → 写入 → 校验，带进度条）、跳转 APP、复位、
PING 探测，操作日志实时滚动。

运行（依赖隔离，勿直接 pip install）：
  uv run --python 3.12 --with pyserial bl_upgrade_gui.py
  或双击 bl_upgrade_gui.bat

线程模型：Tk 主线程只做 UI；每个操作开一个工作线程，经 queue 回传
日志/进度，主线程 root.after 轮询刷 UI（Tkinter 非线程安全，
工作线程禁止直接触碰控件）。
"""
import os
import queue
import sys
import threading
import time
import zlib

import tkinter as tk
from tkinter import filedialog, ttk

import bl_upgrade as blp

APP_TITLE = "LiteBootLoader 升级工具 v1.1.0"
DEFAULT_BAUD = 115200
FOLLOW = {"jump": 2.5, "reset": 6.0, "ping": 0.3}
# 邻居主仓的示例镜像（存在则预填，纯便利不考虑强依赖）
SIBLING_APP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "LiteBootLoader",
                           "app", "examples", "f103c8t6_app", "app.bin")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("680x540")
        root.minsize(560, 460)

        self.q = queue.Queue()
        self.busy = False
        self.worker = None

        top = ttk.LabelFrame(root, text="串口（115200 8N1）")
        top.pack(fill="x", padx=8, pady=(8, 4))
        self.port_var = tk.StringVar()
        self.port_cb = ttk.Combobox(top, textvariable=self.port_var,
                                    width=10, state="readonly")
        self.port_cb.pack(side="left", padx=6, pady=6)
        ttk.Button(top, text="刷新", command=self.refresh_ports)\
            .pack(side="left", padx=2)
        ttk.Label(top, text="串口与 VOFA+/串口助手互斥：使用前请关闭它们")\
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
        ttk.Label(ops, text="升级含自动“请求回 BL”：对端在跑 APP 也会自动回 BL 再升")\
            .pack(side="left", padx=12)

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
        for w in (self.btn_upgrade, self.btn_jump, self.btn_reset, self.btn_ping):
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

    # ---- 操作入口（主线程，启线程） ----
    def _check_ready(self):
        if self.busy:
            return False
        if not self.port_var.get():
            self.log_line("[X] 请先选择串口（点“刷新”枚举）")
            return False
        return True

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

    def start(self, fn, *args):
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
        """返回 BootLoader；调用方负责 close。"""
        bl = blp.BootLoader(port, DEFAULT_BAUD)
        return bl

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
                                     progress=self._progresscb)
            finally:
                bl.s.close()
            self.q.put(("done", rc))
        except Exception as e:      # 串口打开失败等
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
        except Exception as e:
            self._logcb(f"[X] {e}")
            self.q.put(("done", 1))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
