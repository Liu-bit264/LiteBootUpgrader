#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bl_upgrade_gui.py — LiteBootLoader 上位机图形界面（版本随 bl_upgrade.VERSION）

封装 bl_upgrade.py（协议流程的唯一实现；协议规范见 LiteBootLoader 仓库 docs/protocol.md）。

三种界面形态，由**两个互相独立**的模式开关组合而成（状态记录在
~/.litebootupgrader_gui.json 的 advanced / factory 两个键，各自勾选后弹窗确认 →
窗口级重启生效）：

  基础模式：一键升级（对端在跑 APP 时自动"请求回 BL"→ 擦除 → 写入 → 校验，带进度条）、
            跳转 APP、复位、PING，操作日志实时滚动；
  高级模式：额外暴露 CLI 全量功能——INFO / META / OTA 查询 / ERASE / SELFTEST / VERIFY /
            LISTEN / RAW / SETMETA，以及波特率与 pace(ms) 参数（影响本面板全部操作）；
  工厂模式：批量刷写面板（芯片探查与预设镜像、两种换板触发、结果表与计数、记录落盘），
            接管「APP 镜像 + 操作」面板（工厂按芯片取预设镜像，不给错烧留口子）。
            串口为**多选**：选 N 个口 = N 个独立会话并行烧（状态栏逐口显示）。

模式隔离（两条开关互不夹带，四种组合都成立）：
  - 勾「工厂模式」只出工厂面板，不创建任何高级面板控件；高级开关照旧可用、可再勾上，
    工厂模式下也能再进高级模式，反之亦然；
  - 工厂侧**不读**高级面板的任何变量（波特率 / pace / 签名私钥用工厂自己的值），
    高级侧不改工厂的预设、触发方式、计数与记录文件；
  - 能力隔离但资源互斥：工厂「开工」进 lock_btns，与高级操作共享 busy 互斥（同一串口）；
    「停止」不进锁，仅批量运行中可用。

版式：窗口按屏幕尺寸收敛（150% 缩放的 1080p 逻辑高度只有 ~720 px，写死 1010 会有
一截永远看不见）；面板区放进可滚动的画布，装不下时滚动——日志、模式开关与状态行
始终留在窗口里。

运行（依赖隔离，勿直接 pip install）：
  uv run --python 3.12 --with pyserial bl_upgrade_gui.py
  或双击 bl_upgrade_gui.bat

线程模型：Tk 主线程只做 UI；每个操作开一个工作线程，经 queue 回传
日志/进度，主线程 root.after 轮询刷 UI（Tkinter 非线程安全，
工作线程禁止触碰控件；串口参数在主线程取好，线程间只传纯 Python 值）。
工厂多端口时 bl_factory.MultiSession 内部再开每端口一个线程，回调只投队列。
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

import bl_chip
import bl_factory
import bl_upgrade as blp

APP_TITLE = f"LiteBootLoader 升级工具 v{blp.VERSION}"
DEFAULT_BAUD = 115200
FOLLOW = {"jump": 2.5, "reset": 6.0, "ping": 0.3}
STATE_FILE = os.path.join(os.path.expanduser("~"), ".litebootupgrader_gui.json")
# 记录缺省写在**工具目录**下（源码运行=仓库根；打包 exe=exe 旁），而非进程 CWD
RECORDS_DEFAULT = os.path.join(bl_chip.app_dir(), "factory", "records", "records.csv")

# 期望窗口尺寸（按内容给出）与实际最小尺寸；实际尺寸还会按屏幕收敛（_fit_layout）。
# 工厂/双开的面板内容自然高度 ~818/981 px：屏幕够就整面显示，不够则由面板区滚动补
# （这正是「双开时屏幕装不下」的修法——窗口不再硬编码 1010）。
WINDOW_PREF = {
    (False, False): (720, 560),
    (True, False): (720, 790),
    (False, True): (1024, 830),
    (True, True): (1064, 900),
}
WINDOW_MIN = {
    (False, False): (560, 460),
    (True, False): (560, 620),
    (False, True): (900, 620),
    (True, True): (900, 700),
}
PAGE_MIN_H = 150       # 面板区最小高度：再小就没法用，宁可让日志被压

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


def update_state(path: str = STATE_FILE, **flags) -> bool:
    """读-改-写：只覆盖传入的模式键，其余键原样保留。
    （两个模式开关互相独立——翻一个不能把另一个抹掉。）"""
    st = read_state(path)
    st.update(flags)
    return write_state(st, path)


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
    def __init__(self, root: tk.Tk, advanced: bool = False, factory: bool = False):
        self.root = root
        self.advanced = advanced
        self.factory = factory
        # 双开时面板最多：压缩表格行数与日志窗（面板区可滚动，见 _fit_layout）
        self.tall = advanced and factory
        self.restart_pending = False
        root.title(APP_TITLE)
        # 期望尺寸 → 按屏幕收敛（小屏/高缩放：硬编码高度会让下半截够不着）
        pref_w, pref_h = WINDOW_PREF[(advanced, factory)]
        scr_w, scr_h = root.winfo_screenwidth(), root.winfo_screenheight()
        self.win_w = max(480, min(pref_w, scr_w - 40))
        self.win_h = max(340, min(pref_h, scr_h - 80))
        mw, mh = WINDOW_MIN[(advanced, factory)]
        root.minsize(min(mw, self.win_w), min(mh, self.win_h))

        self.q = queue.Queue()
        self.busy = False
        self.worker = None
        self.session = None            # 工厂批量会话（停止按钮用）
        self.lock_btns = []
        self._pump_id = None           # after 句柄：销毁窗口前取消，免 Tcl 报错
        self._closing = False
        self._page_h = PAGE_MIN_H      # 面板区当前高度（_fit_layout 定）
        self.baud_var = tk.StringVar(value=str(DEFAULT_BAUD))
        self.pace_var = tk.StringVar(value="0")
        self.key_var = tk.StringVar()   # 高级面板的签名私钥；工厂侧用自带 f_key
        self.cur_baud = DEFAULT_BAUD   # 主线程开工时取好，工作线程只读纯值
        self.cur_pace = 0
        self.cur_conn = "serial"
        self.cur_ports = []            # 本次批量选中的端口（工作线程只读快照）
        self._port_touched = False     # 用户是否动过端口选择（首次刷新默认选第一个）

        top = ttk.LabelFrame(root, text="串口（8N1）")
        top.pack(fill="x", padx=8, pady=(8, 4))
        self.port_var = tk.StringVar()
        self.port_list = []
        if factory:
            self._build_port_picker(top)     # 多选：一台机器插多个串口时并行烧
        else:
            self.port_cb = ttk.Combobox(top, textvariable=self.port_var,
                                        width=10, state="readonly")
            self.port_cb.pack(side="left", padx=6, pady=6)
            ttk.Button(top, text="刷新", command=self.refresh_ports)\
                .pack(side="left", padx=2)
        self._build_conn_picker(top)

        # 面板区：内容先建在 page_inner 里，尺寸定好后再按屏幕给画布高度
        page = ttk.Frame(root)
        page.pack(fill="both", expand=False, padx=0, pady=0)
        self.page_canvas = tk.Canvas(page, highlightthickness=0,
                                     width=max(320, self.win_w - 24),
                                     height=PAGE_MIN_H)
        self.page_sb = ttk.Scrollbar(page, orient="vertical",
                                     command=self.page_canvas.yview)
        self.page_canvas.configure(yscrollcommand=self.page_sb.set)
        self.page_canvas.pack(side="left", fill="both", expand=True)
        self.page_inner = ttk.Frame(self.page_canvas)
        self._page_win = self.page_canvas.create_window((0, 0), window=self.page_inner,
                                                       anchor="nw")
        self.page_inner.bind("<Configure>", lambda e: self._sync_scroll())
        self.page_canvas.bind("<Configure>", lambda e: self._sync_scroll())
        self._bind_wheel()

        if factory:
            self._build_factory(self.page_inner)
        else:
            self._build_base(self.page_inner)
        if advanced:
            self._build_advanced(self.page_inner)

        # 两个模式开关：始终可见、互相独立——工厂模式下可再进高级模式，反之亦然
        modes = ttk.Frame(root)
        modes.pack(fill="x", padx=10, pady=(2, 0))
        self.adv_var = tk.BooleanVar(value=advanced)
        ttk.Checkbutton(modes, text="高级模式（全量 CLI 功能；切换将弹窗确认并重启界面）",
                        variable=self.adv_var,
                        command=self.toggle_advanced)\
            .pack(side="left")
        self.factory_var = tk.BooleanVar(value=factory)
        ttk.Checkbutton(modes, text="工厂模式（批量刷写；与高级模式可同时开启）",
                        variable=self.factory_var,
                        command=self.toggle_factory)\
            .pack(side="left", padx=(16, 0))

        if not factory:
            # 工厂模式的进度条在右侧状态栏里（_build_factory），其余模式在窗口下沿
            self.progress = ttk.Progressbar(root, maximum=100)
            self.progress.pack(fill="x", padx=8, pady=2)
        self.status = tk.StringVar(value="空闲")
        self.scroll_hint = tk.StringVar()
        ttk.Label(root, textvariable=self.status, anchor="w")\
            .pack(fill="x", padx=10)
        ttk.Label(root, textvariable=self.scroll_hint, anchor="w",
                  foreground="#888").pack(fill="x", padx=10)

        logs = 4 if self.tall else (6 if factory else 14)   # 工厂面板占竖向空间
        logf = ttk.LabelFrame(root, text="日志")
        logf.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.log_text = tk.Text(logf, height=logs, state="disabled",
                                font=("Consolas", 9), wrap="none")
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

        self.refresh_ports()
        if not factory and os.path.isfile(SIBLING_APP):
            self.img_var.set(os.path.normpath(SIBLING_APP))
            self.update_img_info()
        self.root.after(80, self.pump)
        root.update_idletasks()          # 先量内容，再定窗口与面板区高度
        self._fit_layout()
        root.bind("<Configure>", self._on_root_configure)

    # ---- 版式：窗口按屏幕收敛 + 面板区可滚动（小屏/双开都不丢控件） ----
    def _set_page_height(self, h: int):
        self._page_h = max(PAGE_MIN_H, int(h))
        self.page_canvas.configure(height=self._page_h)

    def _fit_layout(self):
        """定窗口尺寸并给面板区配高度：内容装得下就完全不滚动；装不下则面板区滚动，
        「日志 + 状态行 + 模式开关」始终留在窗口内。"""
        self._set_page_height(self.page_inner.winfo_reqheight() or PAGE_MIN_H)
        self.root.update_idletasks()
        # 留几像素余量：版面按像素算，边界上宁可多给日志一行也不切控件
        over = self.root.winfo_reqheight() - self.win_h + 6
        if over > 0:
            self._set_page_height(self.page_inner.winfo_reqheight() - over)
        self.root.geometry(f"{self.win_w}x{self.win_h}")
        self._sync_scroll()

    def _sync_scroll(self):
        """面板内容超出面板区高度：显示滚动条并给出提示（无需滚动时不占地方）。"""
        if self._closing:
            return
        inner_h = self.page_inner.winfo_reqheight()
        need = inner_h > self._page_h
        if need and not self.page_sb.winfo_manager():
            self.page_sb.pack(side="right", fill="y")
        elif not need and self.page_sb.winfo_manager():
            self.page_sb.pack_forget()
        self.scroll_hint.set("面板区超出窗口：鼠标滚轮滚动查看（日志与状态行固定在下沿）"
                             if need else "")
        self.page_canvas.configure(
            scrollregion=(0, 0, max(self.page_inner.winfo_reqwidth(),
                                    self.page_canvas.winfo_reqwidth()), inner_h))

    def _on_root_configure(self, e):
        """窗口被拉大/缩小：把富余高度给面板区（而不是让日志无限长）。"""
        if e.widget is not self.root or self._closing or e.height < 200:
            return
        others = max(0, self.root.winfo_reqheight() - self._page_h)
        want = min(self.page_inner.winfo_reqheight(),
                   max(PAGE_MIN_H, e.height - others))
        if abs(want - self._page_h) > 8:
            self._set_page_height(want)
            self._sync_scroll()

    def _bind_wheel(self):
        def _wheel(e):
            if not self.page_sb.winfo_manager():
                return
            if not self._in_page(self.root.winfo_containing(e.x_root, e.y_root)):
                return
            self.page_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        self.root.bind_all("<MouseWheel>", _wheel, add="+")

    def _in_page(self, w) -> bool:
        """指针是否落在面板区（避免在日志窗上滚动时把面板也带着滚）。"""
        while w is not None:
            if w is self.page_inner:
                return True
            w = getattr(w, "master", None)
        return False

    # ---- 面板：基础 / 工厂 / 高级（各自的控件只在自己的分支里创建） ----
    def _build_base(self, root):
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
        self.btn_jump = ttk.Button(ops, text="跳转 APP",
                                   command=lambda: self.do_quick("jump"))
        self.btn_reset = ttk.Button(ops, text="复位",
                                    command=lambda: self.do_quick("reset"))
        self.btn_ping = ttk.Button(ops, text="PING",
                                   command=lambda: self.do_quick("ping"))
        for i, b in enumerate((self.btn_upgrade, self.btn_jump,
                               self.btn_reset, self.btn_ping)):
            b.pack(side="left", padx=6, pady=6)
        ttk.Label(ops, text="对端在跑 APP 会自动请求回 BL")\
            .pack(side="left", padx=12)
        self.lock_btns += [self.btn_upgrade, self.btn_jump,
                           self.btn_reset, self.btn_ping]

    def _build_port_picker(self, top):
        """工厂模式的串口选择：**多选**列表——选 N 个口就并行烧 N 台。"""
        left = ttk.Frame(top)
        left.pack(side="left", padx=6, pady=6)
        self.port_lb = tk.Listbox(left, selectmode="extended", height=3, width=12,
                                  exportselection=False, activestyle="none",
                                  font=("Consolas", 9))
        sbl = ttk.Scrollbar(left, orient="vertical", command=self.port_lb.yview)
        self.port_lb.configure(yscrollcommand=sbl.set)
        sbl.pack(side="right", fill="y")
        self.port_lb.pack(side="left")
        self.port_lb.bind("<<ListboxSelect>>", lambda e: self._port_sel_changed())
        btns = ttk.Frame(top)
        btns.pack(side="left", padx=(2, 8))
        for text, cmd in (("刷新", self.refresh_ports),
                          ("全选", lambda: self.port_select(all_=True)),
                          ("清空", lambda: self.port_select(all_=False))):
            ttk.Button(btns, text=text, width=6, command=cmd)\
                .pack(side="left", padx=2)
        self.f_sel_var = tk.StringVar(value="已选 0 个端口")
        ttk.Label(top, textvariable=self.f_sel_var, foreground="#555")\
            .pack(side="left", padx=4)

    def _build_conn_picker(self, top):
        ttk.Label(top, text="连接").pack(side="left", padx=(10, 2))
        self.conn_var = tk.StringVar(value="有线串口")
        ttk.Combobox(top, textvariable=self.conn_var, width=9, state="readonly",
                     values=["有线串口", "蓝牙 HC-05"]).pack(side="left")
        ttk.Label(top, text="串口与 VOFA+/串口助手互斥")\
            .pack(side="left", padx=12)

    # ---- 工厂面板（仅工厂模式构建；不引用任何高级面板控件/变量） ----
    def _build_factory(self, root):
        self.f_cfg = bl_chip.load_factory_config()
        self.f_profiles = {}
        self.f_images = dict(self.f_cfg.get("images") or {})
        self.f_effective_images = {}
        self.f_counts = {"ok": 0, "skip": 0, "fail": 0}
        self.cur_opt = None            # 主线程备好的批量选项（工作线程只读纯值）
        self.cur_records = ""
        self.cur_jsonl = ""
        self.cur_profiles = {}
        self.f_port_vars = {}          # 端口 → 状态文案（多端口时逐口显示）
        self.f_port_codes = {}         # 端口 → 最近一次状态码
        self._row_map = {}             # 会话内 (端口, seq) → 结果表行号（跨会话不覆盖）
        self._row_no = 0

        lf = ttk.LabelFrame(root, text="芯片与预设镜像（按探查出的芯片取镜像，不猜）")
        lf.pack(fill="x", padx=8, pady=4)
        r0 = ttk.Frame(lf)
        r0.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(r0, text="芯片").pack(side="left", padx=(4, 2))
        self.f_chip_mode = tk.StringVar(value="自动探查")
        ttk.Combobox(r0, textvariable=self.f_chip_mode, width=10, state="readonly",
                     values=["自动探查", "人工指定"]).pack(side="left")
        self.f_chip = tk.StringVar()
        self.f_chip_cb = ttk.Combobox(r0, textvariable=self.f_chip, width=14,
                                      state="readonly", values=[])
        self.f_chip_cb.pack(side="left", padx=(6, 0))
        ttk.Button(r0, text="重新扫描档案", command=self.f_scan_profiles)\
            .pack(side="left", padx=6)
        self.f_chip_now = tk.StringVar(value="未扫描")
        ttk.Label(r0, textvariable=self.f_chip_now, foreground="#555")\
            .pack(side="left", padx=8)

        self.preset_tv = ttk.Treeview(lf, columns=("id", "name", "app", "image", "state"),
                                      show="headings",
                                      height=2 if self.tall else 3)
        for c, w, t in (("id", 86, "芯片 id"), ("name", 96, "器件"),
                        ("app", 104, "APP 区"), ("image", 290, "预设镜像"),
                        ("state", 92, "状态")):
            self.preset_tv.heading(c, text=t)
            self.preset_tv.column(c, width=w, anchor="w")
        self.preset_tv.pack(fill="x", padx=6, pady=2)
        self.preset_tv.bind("<Double-1>", lambda e: self.f_pick_image())
        rb = ttk.Frame(lf)
        rb.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(rb, text="为本行指定镜像…", command=self.f_pick_image)\
            .pack(side="left", padx=4)
        ttk.Button(rb, text="用固件仓示例镜像", command=self.f_use_example)\
            .pack(side="left", padx=4)
        if not self.tall:
            ttk.Label(rb, text="「状态」列 = 该芯片现在能不能开工（缺镜像/向量表不符即拒绝）",
                      foreground="#888").pack(side="left", padx=8)

        lb = ttk.LabelFrame(root, text="批量刷写")
        lb.pack(fill="x", padx=8, pady=4)
        r1 = ttk.Frame(lb)
        r1.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(r1, text="换板触发").pack(side="left", padx=(4, 2))
        self.f_trigger = tk.StringVar(value="同端口轮询")
        ttk.Combobox(r1, textvariable=self.f_trigger, width=14, state="readonly",
                     values=["同端口轮询", "新串口出现"]).pack(side="left")
        if not self.tall:      # 双开时省掉提示行（内容够高，提示见 README）
            ttk.Label(r1, text="（同端口=拔插板子；新串口=每板一个转换器；"
                               "多选串口=各端口并行、互不影响）",
                      foreground="#888").pack(side="left", padx=6)
        r1b = ttk.Frame(lb)
        r1b.pack(fill="x", padx=4, pady=2)
        self.f_uptodate = tk.BooleanVar(value=True)
        self.f_jump = tk.BooleanVar(value=True)
        self.f_resume = tk.BooleanVar(value=False)
        for var, text in ((self.f_uptodate, "跳过已是最新（CRC 一致）"),
                          (self.f_jump, "烧完跳转 APP"),
                          (self.f_resume, "断点续烧（按记录跳过已烧 UID）")):
            ttk.Checkbutton(r1b, text=text, variable=var).pack(side="left", padx=(4, 10))
        r2 = ttk.Frame(lb)
        r2.pack(fill="x", padx=4, pady=2)
        ttk.Label(r2, text="记录 CSV").pack(side="left", padx=(4, 2))
        self.f_records = tk.StringVar(value=self.f_cfg.get("records") or RECORDS_DEFAULT)
        ttk.Entry(r2, textvariable=self.f_records, width=40).pack(side="left")
        ttk.Button(r2, text="浏览…", command=self.f_pick_records).pack(side="left", padx=2)
        ttk.Label(r2, text="JSONL").pack(side="left", padx=(10, 2))
        self.f_jsonl = tk.StringVar(value=self.f_cfg.get("jsonl") or "")
        ttk.Entry(r2, textvariable=self.f_jsonl, width=22).pack(side="left")
        r3 = ttk.Frame(lb)
        r3.pack(fill="x", padx=4, pady=2)
        ttk.Label(r3, text="签名私钥（可选）").pack(side="left", padx=(4, 2))
        self.f_key = tk.StringVar()
        ttk.Entry(r3, textvariable=self.f_key, width=34).pack(side="left")
        ttk.Button(r3, text="浏览…", command=self.f_pick_key).pack(side="left", padx=2)
        ttk.Label(r3, text="APP 版本").pack(side="left", padx=(10, 2))
        self.f_appver = tk.StringVar()
        ttk.Entry(r3, textvariable=self.f_appver, width=9).pack(side="left")
        ttk.Label(r3, text="（如 1.2.3，留空跳过；写 SET_META 0x02）",
                  foreground="#888").pack(side="left", padx=6)
        r4 = ttk.Frame(lb)
        r4.pack(fill="x", padx=4, pady=(4, 6))
        self.btn_f_start = ttk.Button(r4, text="开工", command=self.do_batch_start)
        self.btn_f_stop = ttk.Button(r4, text="停止", command=self.do_batch_stop,
                                     state="disabled")
        self.btn_f_clear = ttk.Button(r4, text="清空计数/结果表",
                                      command=self.f_clear_results)
        for b in (self.btn_f_start, self.btn_f_stop, self.btn_f_clear):
            b.pack(side="left", padx=6)
        self.lock_btns.append(self.btn_f_start)      # 与高级操作共享 busy 互斥
        if not self.tall:
            ttk.Label(r4, text="端口有应答即开工；已选多个端口则并行",
                      foreground="#888").pack(side="left", padx=8)

        lu = ttk.LabelFrame(root, text="设备结果（本会话）")
        lu.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        # 状态栏在结果表**右侧**（占满高度的一块），不再上下叠两行
        side = ttk.LabelFrame(lu, text="状态")
        side.pack(side="right", fill="y", padx=(2, 4), pady=4)
        cols = ("seq", "port", "time", "chip", "uid", "image", "result", "elapsed",
                "note")
        self.unit_tv = ttk.Treeview(lu, columns=cols, show="headings",
                                    height=3 if self.tall else 5)
        for c, w, t in (("seq", 34, "#"), ("port", 60, "端口"), ("time", 62, "时间"),
                        ("chip", 84, "芯片"), ("uid", 92, "UID"),
                        ("image", 112, "镜像"), ("result", 46, "结果"),
                        ("elapsed", 46, "耗时"), ("note", 168, "说明")):
            self.unit_tv.heading(c, text=t)
            self.unit_tv.column(c, width=w, anchor="w")
        self.unit_tv.pack(side="left", fill="both", expand=True, padx=(4, 2), pady=4)
        self._build_factory_status(side)
        self.f_scan_profiles()

    def _build_factory_status(self, side):
        """右侧状态栏：大字状态 + 进度条 + 计数 + 逐端口状态（多端口时）。"""
        self.f_state_var = tk.StringVar(value="空闲")
        ttk.Label(side, textvariable=self.f_state_var, font=("", 11, "bold"),
                  wraplength=180, justify="left", anchor="w")\
            .pack(fill="x", padx=6, pady=(6, 2))
        self.progress = ttk.Progressbar(side, maximum=100)
        self.progress.pack(fill="x", padx=6, pady=(0, 4))
        cnt = ttk.Frame(side)
        cnt.pack(fill="x", padx=6, pady=2)
        self.cnt_ok = tk.StringVar(value="成功 0")
        self.cnt_skip = tk.StringVar(value="跳过 0")
        self.cnt_fail = tk.StringVar(value="失败 0")
        self.cnt_rate = tk.StringVar(value="合格率 —")
        for i, v in enumerate((self.cnt_ok, self.cnt_skip,
                               self.cnt_fail, self.cnt_rate)):
            ttk.Label(cnt, textvariable=v, font=("", 10, "bold"))\
                .grid(row=i // 2, column=i % 2, sticky="w", padx=4, pady=1)
        self.port_stat = ttk.LabelFrame(side, text="端口")
        self._render_port_states([])

    def _render_port_states(self, ports):
        """逐端口状态：多端口并行时一个端口一行；单端口时整块不占地方。"""
        for w in self.port_stat.winfo_children():
            w.destroy()
        self.f_port_vars = {}
        self.f_port_codes = {}
        if len(ports) > 1:
            if not self.port_stat.winfo_manager():
                self.port_stat.pack(fill="x", padx=6, pady=(4, 6))
            for p in ports:
                v = tk.StringVar(value=f"{p}  等待…")
                self.f_port_vars[p] = v
                ttk.Label(self.port_stat, textvariable=v, font=("Consolas", 9),
                          anchor="w").pack(fill="x", padx=4)
        elif self.port_stat.winfo_manager():
            self.port_stat.pack_forget()

    def _agg_state(self) -> str:
        """多端口时状态栏大字位置的汇总文案。"""
        n = len(self.cur_ports)
        active = sum(1 for p in self.cur_ports
                     if self.f_port_codes.get(p) in ("detect", "flash", "post"))
        done = sum(1 for p in self.cur_ports
                   if self.f_port_codes.get(p) in ("ok", "skip", "stopped",
                                                   "halted", "done"))
        if active:
            return f"{active}/{n} 端口烧录中"
        if done == n:
            return f"{n} 端口已结束"
        return f"{n} 端口并行（等设备）"


    # ---- 工厂面板操作（主线程） ----
    def f_scan_profiles(self):
        """扫描芯片档案（搜索顺序见 bl_chip.py）并刷新预设表。"""
        try:
            self.f_profiles = bl_chip.load_profiles(log=self.log_line)
        except bl_chip.ChipError as e:
            self.f_profiles = {}
            self.log_line(f"[X] 芯片档案加载失败：{e}")
        self.f_chip_cb["values"] = sorted(self.f_profiles)
        if self.f_profiles and not self.f_chip.get():
            self.f_chip.set(sorted(self.f_profiles)[0])
        self.f_render_presets()
        self.f_chip_now.set(f"档案 {len(self.f_profiles)} 片" if self.f_profiles
                            else "未发现档案（检查固件仓路径 / factory 配置）")
        if not self.f_profiles:
            self.log_line("[!] 未发现芯片档案：本工具旁需有 bl_chip_profiles.json，"
                          "或兄弟目录存在 LiteBootLoader 仓（chips/*.json）")

    def f_render_presets(self):
        self.preset_tv.delete(*self.preset_tv.get_children())
        self.f_effective_images = {}
        for cid in sorted(self.f_profiles):
            p = self.f_profiles[cid]
            img = self.f_images.get(cid) or p.example_image or ""
            state = "未配镜像"
            if img:
                self.f_effective_images[cid] = img
                if not os.path.isfile(img):
                    state = "文件不存在"
                else:
                    chk = bl_chip.check_image(img, p)
                    state = "可开工" if chk.ok else "体检不通过"
            self.preset_tv.insert("", "end", iid=cid,
                                  values=(cid, p.name, f"{p.app_size // 1024}K "
                                                        f"@ {p.app_base:#x}",
                                          img or "（未指定）", state))

    def _preset_selected(self):
        sel = self.preset_tv.selection()
        if not sel:
            self.log_line("[X] 先在预设表里选一行（芯片）")
            return None
        return sel[0]

    def f_pick_image(self):
        cid = self._preset_selected()
        if not cid:
            return
        p = filedialog.askopenfilename(
            title=f"为 {cid} 指定预设镜像（APP .bin）",
            filetypes=[("BIN 镜像", "*.bin"), ("所有文件", "*.*")])
        if not p:
            return
        self.f_images[cid] = os.path.normpath(p)
        self._save_factory_cfg()
        self.f_render_presets()

    def f_use_example(self):
        cid = self._preset_selected()
        if not cid:
            return
        p = self.f_profiles[cid].example_image
        if not p:
            self.log_line(f"[X] {cid} 没有可用的示例镜像（固件仓 app/examples/…）")
            return
        self.f_images[cid] = p
        self._save_factory_cfg()
        self.f_render_presets()

    def f_pick_records(self):
        p = filedialog.asksaveasfilename(title="选择结果记录 CSV（存在则追加）",
                                        defaultextension=".csv",
                                        initialfile="records.csv")
        if p:
            self.f_records.set(os.path.normpath(p))

    def f_pick_key(self):
        p = filedialog.askopenfilename(title="选择 ECDSA P-256 私钥 PEM",
                                       filetypes=[("PEM", "*.pem"), ("全部", "*.*")])
        if p:
            self.f_key.set(os.path.normpath(p))

    def _save_factory_cfg(self):
        cfg = dict(self.f_cfg)
        cfg.update(images=self.f_images, records=self.f_records.get(),
                   jsonl=self.f_jsonl.get() or None)
        if bl_chip.save_factory_config(cfg):
            self.f_cfg = cfg
        else:
            self.log_line("[X] 工厂配置保存失败（权限？）")

    def _factory_options(self) -> bl_factory.Options:
        """组装批量选项：只用工厂面板的值——不读高级面板的波特率/pace/签名私钥。"""
        ver = None
        s = self.f_appver.get().strip()
        if s:
            ver = blp.parse_app_version(s)          # 非法 → ValueError，开工前拦下
        manual = not self.f_chip_mode.get().startswith("自动")
        return bl_factory.Options(
            chip_id=(self.f_chip.get() or "auto") if manual else "auto",
            trigger=("newport" if self.f_trigger.get().startswith("新串口") else "poll"),
            image_map=dict(self.f_effective_images),
            skip_uptodate=self.f_uptodate.get(),
            auto_jump=self.f_jump.get(),
            app_version=ver,
            key_path=self.f_key.get().strip() or None,
            resume=self.f_resume.get())

    def f_clear_results(self, log: bool = True):
        for iid in self.unit_tv.get_children():
            self.unit_tv.delete(iid)
        self.f_counts = {"ok": 0, "skip": 0, "fail": 0}
        self._row_map = {}
        self._row_no = 0
        self._render_port_states(self.cur_ports if self.busy else [])
        self._render_counters()
        if log:
            self.log_line("计数与结果表已清空")

    def _render_counters(self):
        ok, skip, fail = (self.f_counts["ok"], self.f_counts["skip"],
                          self.f_counts["fail"])
        total = ok + skip + fail
        self.cnt_ok.set(f"成功 {ok}")
        self.cnt_skip.set(f"跳过 {skip}")
        self.cnt_fail.set(f"失败 {fail}")
        rate = f"{100.0 * (ok + skip) / total:.0f}%" if total else "—"
        self.cnt_rate.set(f"合格率 {rate}·{total} 台")

    def do_batch_start(self):
        if self.busy:
            return
        ports = self.selected_ports()
        if not ports:
            self.log_line("[X] 请先在列表里选择串口（可多选；「刷新」枚举、「全选」批量勾）")
            return
        if not self.f_profiles:
            self.log_line("[X] 没有可用芯片档案，无法批量（先「重新扫描档案」）")
            return
        try:
            self.cur_opt = self._factory_options()
        except ValueError as e:
            self.log_line(f"[X] {e}")
            return
        missing = [c for c, f in self.f_effective_images.items() if not os.path.isfile(f)]
        if missing:
            self.log_line(f"[X] 预设镜像文件不存在：{', '.join(missing)}")
            return
        if not self.f_effective_images:
            self.log_line("[X] 至少给一片芯片指定预设镜像（双击预设表行）")
            return
        # 工厂侧固定 115200 / pace=0，连接类型在主线程取好：不读高级面板的波特率与 pace
        self.cur_baud = DEFAULT_BAUD
        self.cur_pace = 0
        self.cur_conn = "bt" if "蓝牙" in self.conn_var.get() else "serial"
        self.cur_records = self.f_records.get().strip()
        self.cur_jsonl = self.f_jsonl.get().strip()
        self.cur_profiles = dict(self.f_profiles)
        self.cur_ports = list(ports)
        self._save_factory_cfg()
        # 只重置「(端口,seq) → 行号」映射：计数与结果表跨批次保留（清空有专用按钮）
        self._row_map = {}
        self._render_port_states(self.cur_ports)
        self.f_state_var.set("等待设备…" if len(self.cur_ports) == 1
                             else f"{len(self.cur_ports)} 端口并行（等设备）")
        self.start_worker(self._batch_worker, list(self.cur_ports))

    def do_batch_stop(self):
        sess = self.session
        if sess is None:
            return
        # 再点一次 = 立即停止（打断正在进行的升级；时延 ≤ 当前命令超时）；
        # 多端口时停止会广播到所有端口
        sess.request_stop(immediate=sess.stop_requested)

    # ---- 工厂工作线程（只经 queue 与 UI 通信） ----
    def _open_factory(self, port):
        return blp.BootLoader(port, DEFAULT_BAUD, 0, conn=self.cur_conn)

    def _statecb(self, port: str, code: str, text: str):
        self.q.put(("fstate", port, code, text))

    def _unit_startcb(self, rec: dict):
        self.q.put(("unit_start", rec))

    def _unitcb(self, rec: dict):
        self.q.put(("unit_row", rec))

    def _progresscb(self, port: str, done: int, total: int):
        self.q.put(("progress", port, done, total))

    def _batch_worker(self, ports):
        """多端口批量：MultiSession 内部每端口一个会话线程，互不影响。"""
        try:
            writer = bl_factory.RecordWriter(self.cur_records or None,
                                             self.cur_jsonl or None)
            sess = bl_factory.MultiSession(
                self.cur_profiles, self.cur_opt, ports, open_fn=self._open_factory,
                records=writer, log=self._logcb, on_state=self._statecb,
                on_progress=self._progresscb, on_unit_start=self._unit_startcb,
                on_unit=self._unitcb)
            self.session = sess
            summary = sess.run()
            self.q.put(("batch_done", summary))
        except (Exception, SystemExit) as e:
            self._logcb(f"[X] {e}")
            self.q.put(("batch_done", None))

    def _unit_row(self, rec: dict, start: bool):
        # 会话内 seq 从 1 重新计数，多端口时两个端口会有同名 seq：
        # 键取 (端口, seq)，映射到全局唯一行号，跨会话也不互相覆盖
        key = (rec.get("port"), rec.get("seq"))
        iid = self._row_map.get(key)
        if iid is None:
            self._row_no += 1
            self._row_map[key] = iid = f"u{self._row_no}"
        if start:
            if not self.unit_tv.exists(iid):
                self.unit_tv.insert("", "end", iid=iid,
                                    values=(rec.get("seq"), rec.get("port") or "",
                                            (rec.get("started") or "")[11:], "", "",
                                            "", "…", "", ""))
            self.unit_tv.see(iid)
            return
        vals = (rec.get("seq"), rec.get("port") or "",
                (rec.get("started") or "")[11:],
                rec.get("chip_id") or "",
                (rec.get("uid") or "")[:16], os.path.basename(rec.get("image") or ""),
                rec.get("result") or "", rec.get("elapsed_s") or "",
                rec.get("reason") or "")
        if self.unit_tv.exists(iid):
            self.unit_tv.item(iid, values=vals)
        else:
            self.unit_tv.insert("", "end", iid=iid, values=vals)
        self.unit_tv.see(iid)

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
        # 提示单独一行：并排会让整行超出窗口宽度（1.5.0 版式修复）
        if not self.tall:
            ttk.Label(adv, text="签名私钥留空 = legacy VERIFY；填私钥 = VERIFY_SIGNED"
                                "（固件需 BL_SIGN_EN=1 且公钥配对，见 README）",
                      foreground="#888").pack(anchor="w", padx=8)

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
        self.port_list = ports
        if self.factory:
            keep = self.selected_ports()
            self.port_lb.delete(0, "end")
            for p in ports:
                self.port_lb.insert("end", p)
            for i, p in enumerate(ports):
                if p in keep or (not keep and i == 0 and not self._port_touched):
                    # 首次刷新默认选第一个（与单串口下拉一致），之后尊重用户选择
                    self.port_lb.selection_set(i)
            self._port_sel_changed()
        else:
            self.port_cb["values"] = ports
            if ports and not self.port_var.get():
                self.port_var.set(ports[0])

    def selected_ports(self) -> list:
        """当前选中的端口（工厂=多选列表；其余模式=下拉单口）。"""
        if self.factory:
            return [self.port_lb.get(i) for i in self.port_lb.curselection()]
        return [self.port_var.get()] if self.port_var.get() else []

    def port_select(self, all_: bool = True):
        if all_:
            self.port_lb.selection_set(0, "end")
        else:
            self.port_lb.selection_clear(0, "end")
        self._port_sel_changed()

    def _port_sel_changed(self):
        """列表选择变化：同步 port_var（双开时高级面板用第一个口）与已选计数。"""
        self._port_touched = True
        sel = self.selected_ports()
        self.port_var.set(sel[0] if sel else "")
        n = len(sel)
        self.f_sel_var.set(f"已选 {n} 个端口" + ("（并行烧录）" if n > 1 else ""))

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
        if self.factory and hasattr(self, "btn_f_stop"):
            # 停止按钮不参与 busy 互斥（它只在繁忙时有意义）
            self.btn_f_stop.state(["!disabled"] if b else ["disabled"])

    def log_line(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip("\n") + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def pump(self):
        if self._closing:
            return
        try:
            while True:
                kind, *payload = self.q.get_nowait()
                if kind == "log":
                    self.log_line(payload[0])
                elif kind == "status":
                    self.status.set(payload[0])
                elif kind == "progress":
                    port, done, total = payload
                    self.progress["value"] = done * 100 // max(total, 1)
                    tag = f"{port} " if len(self.cur_ports) > 1 else ""
                    self.status.set(f"{tag}写入 {done}/{total} B")
                elif kind == "done":
                    rc = payload[0]
                    self.progress["value"] = 100 if rc == 0 else self.progress["value"]
                    self.status.set("完成 ✓" if rc == 0 else f"失败（退出码 {rc}）")
                    self.set_busy(False)
                elif kind == "fstate":
                    port, code, text = payload
                    self.f_port_codes[port] = code
                    if port in self.f_port_vars:
                        self.f_port_vars[port].set(f"{port}  {text}")
                    self.f_state_var.set(text if len(self.cur_ports) <= 1
                                         else self._agg_state())
                    self.status.set(f"{port}：{text}" if len(self.cur_ports) > 1
                                    else text)
                    if code in ("wait", "wait_swap", "stopped", "halted"):
                        self.progress["value"] = 0
                elif kind == "unit_start":
                    self._unit_row(payload[0], start=True)
                elif kind == "unit_row":
                    rec = payload[0]
                    self._unit_row(rec, start=False)
                    r = rec.get("result")
                    if r in ("OK", "SKIP", "FAIL"):
                        self.f_counts[{"OK": "ok", "SKIP": "skip",
                                       "FAIL": "fail"}[r]] += 1
                        self._render_counters()
                elif kind == "batch_done":
                    s = payload[0]
                    self.session = None
                    self.progress["value"] = 0
                    if s is not None:
                        # 计数已在每条结果行上实时累计，这里只报会话小结
                        if s.halted:
                            self.f_state_var.set(f"已停止：{s.halted}")
                        else:
                            # 明示「本次」：右侧计数是跨批次的累计值
                            tagp = (f"{len(self.cur_ports)} 端口并行；"
                                    if len(self.cur_ports) > 1 else "")
                            self.f_state_var.set(
                                f"{tagp}本次：成功 {s.ok} / 跳过 {s.skip} / 失败 {s.fail}"
                                f"（{s.elapsed_s:.0f}s）")
                    else:
                        self.f_state_var.set("已中止（见日志）")
                    self.set_busy(False)
        except queue.Empty:
            pass
        self._pump_id = self.root.after(80, self.pump)

    # ---- 模式切换与界面重启（主线程；两开关互相独立） ----
    def _toggle_mode(self, key: str, var: tk.BooleanVar, on: tuple, off: tuple):
        """模式开关统一入口：确认 → 读-改-写状态（只翻自己的键）→ 窗口级重启。
        另一个模式的键原样保留——工厂/高级可任意组合，互不夹带。"""
        want = var.get()
        if want == getattr(self, key):
            return
        if self.busy:
            messagebox.showinfo("正在运行",
                                "有操作/批量在运行，请先停止再切换模式。")
            var.set(getattr(self, key))
            return
        title, msg = on if want else off
        if messagebox.askyesno(title, msg):
            if not update_state(**{key: want}):
                messagebox.showerror("切换失败",
                                     f"状态文件写入失败（{STATE_FILE}），未重启。")
                var.set(getattr(self, key))
                return
            self.restart_pending = True
            self._closing = True          # 停掉 pump 轮询，免销毁后 Tcl 回调报错
            self.root.destroy()           # main() 循环将按新组合重建窗口
        else:
            var.set(getattr(self, key))  # 用户取消，勾选框回弹

    def toggle_advanced(self):
        self._toggle_mode(
            "advanced", self.adv_var,
            on=("进入高级模式",
                "高级模式将显示全部 CLI 功能，含 ERASE（整片擦除 APP）、SETMETA"
                "（写参数区）、RAW（发原始字节）等危险操作。\n\n"
                "确认重启界面进入高级模式？（工厂模式开关不受影响，可另行开启）"),
            off=("返回基础界面",
                 "关闭高级模式，界面将重启返回基础操作。\n\n"
                 "确认继续？（工厂模式开关不受影响）"))

    def toggle_factory(self):
        self._toggle_mode(
            "factory", self.factory_var,
            on=("进入工厂模式",
                "工厂模式将显示批量刷写面板：按探查出的芯片自动取预设镜像并连续烧录，"
                "接管「APP 镜像 / 操作」面板，结果落盘可追溯。\n\n"
                "注意：「自动开工」等于“端口上有应答就烧”，请先确认预设镜像与端口。\n\n"
                "确认重启界面进入工厂模式？（高级模式开关不受影响，可另行开启）"),
            off=("退出工厂模式",
                 "关闭工厂模式，界面将重启（批量会话若在运行请先停止）。\n\n"
                 "确认继续？（高级模式开关不受影响）"))

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
        """基础/高级操作入口：波特率与 pace 取高级面板值（工厂批量不走这里）。"""
        try:
            self.cur_baud = int(self.baud_var.get())
        except ValueError:
            self.cur_baud = DEFAULT_BAUD
        try:
            self.cur_pace = int(self.pace_var.get())
        except ValueError:
            self.cur_pace = 0
        self.cur_conn = "bt" if "蓝牙" in self.conn_var.get() else "serial"
        self.start_worker(fn, *args)

    def start_worker(self, fn, *args):
        """置忙态并起工作线程。不读任何模式专属变量——工厂批量也走这里。"""
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
        """返回 BootLoader；调用方负责 close。连接参数用主线程取好的纯值。"""
        return blp.BootLoader(port, self.cur_baud, self.cur_pace, conn=self.cur_conn)

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
    # 窗口级重启循环：模式开关写状态后销毁窗口，这里按新组合（四种：基础/高级/工厂/双开）重建
    while True:
        st = read_state()
        root = tk.Tk()
        app = App(root, advanced=bool(st.get("advanced", False)),
                  factory=bool(st.get("factory", False)))
        root.mainloop()
        if not app.restart_pending:
            break


if __name__ == "__main__":
    main()
