#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bl_factory.py — 工厂批量刷写引擎（GUI 工厂模式与 CLI `factory` 子命令共用）

一次「开工」= 一个会话：等设备 → 探查芯片 → 选预设镜像 → 体检 → （跳过判定）→
烧录校验 → 收尾（写 APP 版本 / 跳转）→ 记录 → 等换板 → ……直到停止或达到台数上限。

换板触发（两种，界面/命令行可切换）：
  poll    —— 同端口轮询（默认）：适配器共用一根线、COM 口常驻的夹具。靠 PING 应答判断
             设备在否，靠 **UID 变化** 或「失联后重现」识别下一块板。
  newport —— 新串口出现：每板一个 USB 转换器/板载 CDC 的夹具。端口集合差分；`port`
             参数在此模式下是**白名单**（只接受该串口名出现时开工，避免误刷无关 CDC）。

依赖全部可注入（open_fn / list_ports_fn / sleep / now），主机侧单测用假串口即可驱动
整机状态机，不需要硬件。

安全护栏：预设镜像按**探查出的芯片 id** 取——没配该芯片的预设镜像时停住并提示，
绝不猜镜像；镜像体检（向量表）不通过不烧；「自动开始」等于「端口上有应答就烧」，
所以 CLI 侧开工前有一次确认（--yes 跳过）。
"""
import csv
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import bl_chip
import bl_upgrade as blp

RESULT_OK = "OK"
RESULT_FAIL = "FAIL"
RESULT_SKIP = "SKIP"

STAGE_DETECT = "detect"
STAGE_IMAGE = "image"
STAGE_FLASH = "flash"
STAGE_POST = "post"

CSV_FIELDS = ["seq", "started", "finished", "elapsed_s", "port", "chip_id", "chip_name",
              "uid", "image", "image_size", "image_crc32", "image_sha256",
              "result", "stage", "reason", "bl_version"]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _list_ports() -> list:
    import serial.tools.list_ports
    return [p.device for p in serial.tools.list_ports.comports()]


def _default_open(port: str):
    return blp.BootLoader(port, 115200, 0, "serial")


@dataclass
class Options:
    """一次会话的批量行为。"""

    chip_id: str = "auto"           # "auto" 或档案 id（人工指定）
    trigger: str = "poll"           # poll | newport
    image_map: dict = field(default_factory=dict)     # chip_id -> 镜像路径
    skip_uptodate: bool = True      # 设备 APP 有效且 CRC32 一致 → 跳过写入
    auto_jump: bool = True          # 校验后自动跳转 APP
    app_version: tuple = None       # (maj,min,pat)：烧录后写 SET_META 0x02
    key_path: str | None = None     # 签名私钥非 None 走 VERIFY_SIGNED，否则 legacy VERIFY
    force_image: bool = False       # 跳过镜像体检（应急口）
    allow_probe: bool = True        # 容量撞车时允许 VERIFY 只读探查
    count: int = 0                  # 台数上限（0=不限）
    resume: bool = False            # 按记录文件跳过已成功烧录的 UID
    poll_interval: float = 0.4      # 轮询间隔（秒）
    ping_timeout: float = 0.3       # PING 等待（秒）
    swap_log_every: float = 15.0    # 「仍在等待换板」提示间隔（秒）
    halt_on_missing_image: bool = True   # 探查到的芯片没配预设镜像 → 停批


@dataclass
class Summary:
    ok: int = 0
    skip: int = 0
    fail: int = 0
    total: int = 0
    elapsed_s: float = 0.0
    halted: str = ""                # 非空 = 会话因配置/环境问题中止

    @property
    def passed(self) -> bool:
        return self.fail == 0 and not self.halted


class RecordWriter:
    """结果落盘：CSV（utf-8-sig，Excel 友好）+ 可选 JSONL（机器可读）。追加写。"""

    def __init__(self, csv_path: str | None = None, jsonl_path: str | None = None):
        self.csv_path = csv_path
        self.jsonl_path = jsonl_path

    def _mkdir(self, path: str):
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)

    def write(self, rec: dict) -> None:
        if self.csv_path:
            new = (not os.path.isfile(self.csv_path)
                   or os.path.getsize(self.csv_path) == 0)
            self._mkdir(self.csv_path)
            with open(self.csv_path, "a", newline="",
                      encoding="utf-8-sig" if new else "utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow(rec)
        if self.jsonl_path:
            self._mkdir(self.jsonl_path)
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def done_uids(self) -> set:
        """既有记录里成功烧录过的 UID（断点续烧用）。"""
        out = set()
        if self.csv_path and os.path.isfile(self.csv_path):
            try:
                with open(self.csv_path, newline="", encoding="utf-8-sig") as f:
                    for row in csv.DictReader(f):
                        if (row.get("result") or "").strip() == RESULT_OK:
                            uid = (row.get("uid") or "").strip().upper()
                            if uid:
                                out.add(uid)
            except OSError:
                pass
        return out

    def close(self) -> None:
        pass


class BatchSession:
    """批量会话。回调：log(msg) / on_state(code, text) / on_progress(done,total)
    / on_unit_start(rec) / on_unit(rec)。"""

    def __init__(self, profiles: dict, options: Options, open_fn=None,
                 list_ports_fn=None, records: RecordWriter | None = None,
                 log=None, on_state=None, on_progress=None,
                 on_unit_start=None, on_unit=None,
                 sleep=time.sleep, now=time.time):
        self.profiles = profiles
        self.opt = options
        self.records = records
        self._open = open_fn or _default_open
        self._list_ports = list_ports_fn or _list_ports
        self._log = log or (lambda m: None)
        self._state_cb = on_state or (lambda code, text: None)
        self._on_progress = on_progress
        self._on_unit_start = on_unit_start or (lambda rec: None)
        self._on_unit = on_unit or (lambda rec: None)
        self._sleep = sleep
        self._now = now
        self._stop = False
        self._immediate = False
        self.summary = Summary()
        self.done_uids = set()
        self.seq = 0
        self._last_uid = ""

    # ---- 控制 ----

    @property
    def stop_requested(self) -> bool:
        return self._stop

    def request_stop(self, immediate: bool = False):
        """软停：当前台烧完即停；immediate=True 连正在进行的升级也中止
        （时延上限 = 当前命令超时 ≤5 s，串口读不可中断）。"""
        if self._stop and not immediate:
            return
        self._stop = True
        self._immediate = self._immediate or immediate
        self._log("[!] 已请求停止" + ("（立即）" if immediate else "（当前台完成后）"))

    def _abort_now(self) -> bool:
        """传给 run_upgrade 的 should_stop：只有「立即停止」才打断进行中的升级。"""
        return self._immediate

    def _state(self, code: str, text: str):
        self._state_cb(code, text)

    # ---- 主循环 ----

    def run(self, port: str) -> Summary:
        t0 = self._now()
        if self.opt.resume and self.records is not None:
            loaded = self.records.done_uids()
            self.done_uids |= loaded
            if loaded:
                self._log(f"断点续烧：既有记录里 {len(loaded)} 片已成功，UID 命中即跳过")
        try:
            if self.opt.trigger == "newport":
                self._run_newport(port)
            else:
                self._run_same_port(port)
        finally:
            self.summary.elapsed_s = max(0.0, self._now() - t0)
            if self.summary.halted:
                self._state("halted", self.summary.halted)
            elif self._stop:
                self._state("stopped", "已停止")
            else:
                self._state("done", f"完成：成功 {self.summary.ok} / "
                                    f"跳过 {self.summary.skip} / 失败 {self.summary.fail}")
        return self.summary

    def _reached_limit(self) -> bool:
        return bool(self.opt.count) and self.summary.total >= self.opt.count

    def _run_same_port(self, port: str):
        self._log(f"同端口轮询模式：端口 {port} 常开，拔插的是板子"
                  f"（换板靠 UID 变化或失联后重现识别）")
        self._state("wait", "等待设备插入…")
        try:
            bl = self._open(port)
        except (Exception, SystemExit) as e:
            self.summary.halted = f"打开 {port} 失败：{e}"
            self._log(f"[X] {self.summary.halted}")
            return
        try:
            first = True
            while not self._stop and not self._reached_limit() \
                    and not self.summary.halted:
                if first:
                    if not self._alive(bl):
                        self._sleep(self.opt.poll_interval)
                        continue
                    self._run_unit(bl, port)
                    first = False
                    continue
                if not self._wait_new_unit(bl):
                    break
                self._run_unit(bl, port)
        finally:
            self._close(bl)

    def _run_newport(self, port: str):
        known = self._list_ports()
        self._log(f"新串口模式：起始集合 {known or '（空）'}；"
                  f"只接受 {port} 出现时开工（其余新口忽略）")
        self._state("wait", f"等待串口 {port} 出现…")
        while not self._stop and not self._reached_limit() and not self.summary.halted:
            cur = self._list_ports()
            known = [p for p in known if p in cur]     # 消失的口重新出现时能再次触发
            for p in [x for x in cur if x not in known]:
                known.append(p)
                if p != port:
                    self._log(f"新串口 {p} 出现，但不在白名单（{port}），忽略")
                    continue
                if self._stop or self._reached_limit():
                    break
                self._log(f"新串口 {p} 出现，开工…")
                try:
                    bl = self._open(p)
                except (Exception, SystemExit) as e:
                    self._log(f"[X] 打开 {p} 失败：{e}（跳过）")
                    continue
                try:
                    self._run_unit(bl, p)
                finally:
                    self._close(bl)
            self._sleep(self.opt.poll_interval)

    def _close(self, bl):
        try:
            bl.s.close()
        except Exception:
            pass

    # ---- 设备探测 ----

    def _alive(self, bl) -> bool:
        return bl.cmd("ping", timeout=self.opt.ping_timeout) is not None

    def _info(self, bl):
        r = bl.cmd("info", timeout=2.0)
        return None if r is None else blp.parse_info_fields(r["data"])

    def _wait_new_unit(self, bl) -> bool:
        """等下一块板：UID 变化，或「失联（连续 2 次无应答）后重现」。
        同一块板原地重启也会造成失联→重现，此时由 done_uids 去重拦下。"""
        seen_gap = False
        misses = 0
        last_uid = self._last_uid
        t_log = self._now()
        self._state("wait_swap", "等待换板…（拔下已烧板，插上下一块）")
        while not self._stop:
            if not self._alive(bl):
                misses += 1
                if misses >= 2 and not seen_gap:
                    seen_gap = True
                    self._log("    设备已离开（连续无应答）")
                if self._now() - t_log >= self.opt.swap_log_every:
                    t_log = self._now()
                    self._log(f"    仍在等待下一块板…（已 {self.summary.total} 台）")
                self._sleep(self.opt.poll_interval)
                continue
            misses = 0
            if seen_gap:
                self._log("    检测到新设备（失联后重现）")
                return True
            info = self._info(bl)
            uid = ""
            if info and not info.get("short"):
                uid = info["uid"].hex().upper()
            if uid and uid != last_uid:
                self._log(f"    检测到新设备（UID {uid[:8]}…）")
                return True
            self._sleep(self.opt.poll_interval)
        return False

    # ---- 单台流程 ----

    def _resolve_chip(self, bl):
        """→ (profile, 命中说明, info)。any 失败抛 ChipError。"""
        if not blp.ensure_bl(bl, log=self._log, should_stop=self._abort_now):
            raise bl_chip.ChipError("无法确认对端为 BL（探测失败）")
        manual = self.opt.chip_id and self.opt.chip_id != "auto"
        if manual:
            p = self.profiles.get(self.opt.chip_id)
            if p is None:
                raise bl_chip.ChipError(
                    f"人工指定的芯片 {self.opt.chip_id} 不在档案里"
                    f"（已有：{', '.join(sorted(self.profiles)) or '无'}）")
            info = self._info(bl) or {}
            return p, f"人工指定 {p.id}", info
        d = bl_chip.detect_chip(bl, self.profiles, allow_probe=self.opt.allow_probe,
                                log=self._log)
        if not d.ok:
            raise bl_chip.ChipError(f"芯片探查失败：{d.reason}")
        return d.profile, d.reason, (d.info or {})

    def _run_unit(self, bl, port: str) -> dict:
        self.summary.total += 1
        self.seq += 1
        rec = {f: "" for f in CSV_FIELDS}
        rec.update(seq=self.seq, started=_iso(self._now()), port=port,
                   result=RESULT_FAIL)
        t0 = self._now()
        stage = STAGE_DETECT
        self._last_uid = ""
        self._log(f"--- 第 {self.seq} 台（{port}）---")
        self._on_unit_start(rec)
        try:
            self._state("detect", "探查芯片…")
            prof, why, info = self._resolve_chip(bl)
            rec.update(chip_id=prof.id, chip_name=prof.name)
            if info and not info.get("short"):
                rec["uid"] = info["uid"].hex().upper()
                rec["bl_version"] = ".".join(str(x) for x in info["bl_version"])
                self._last_uid = rec["uid"]
            self._log(f"芯片 {prof.id}（{prof.name}）—— {why}")
            if rec["uid"]:
                self._log(f"UID {rec['uid']}  APP "
                          f"{'有效' if info['app_valid'] else '无效'} "
                          f"size={info['app_size']} crc={info['app_crc32']:#010x}")

            stage = STAGE_IMAGE
            path = self.opt.image_map.get(prof.id)
            if not path:
                raise bl_chip.ChipError(
                    f"芯片 {prof.id} 没有配预设镜像（工厂模式按芯片取镜像，不猜）")
            if not os.path.isfile(path):
                raise bl_chip.ChipError(f"预设镜像不存在：{path}")
            chk = bl_chip.check_image(path, prof, force=self.opt.force_image)
            rec.update(image=path, image_size=chk.size, image_crc32=f"{chk.crc32:08X}",
                       image_sha256=chk.sha256)
            self._log(f"镜像 {os.path.basename(path)}：{chk.describe()}")
            if not chk.ok:
                raise bl_chip.ChipError(f"镜像体检不通过：{chk.msg}")

            if rec["uid"] and rec["uid"] in self.done_uids:
                src = "断点续烧" if self.opt.resume else "本次会话"
                return self._finish(rec, t0, RESULT_SKIP, stage,
                                    f"UID 已烧录过（{src}既有记录）")
            if (self.opt.skip_uptodate and info and not info.get("short")
                    and info["app_valid"] and info["app_crc32"] == chk.crc32):
                return self._finish(rec, t0, RESULT_SKIP, stage,
                                    "设备已是本镜像（APP 有效且 CRC32 一致）")

            stage = STAGE_FLASH
            self._state("flash", f"烧录 {os.path.basename(path)} → {prof.id}")
            rc = blp.run_upgrade(bl, path, log=self._log, progress=self._on_progress,
                                 key_path=self.opt.key_path, app_size=prof.app_size,
                                 should_stop=self._abort_now)
            if rc != 0:
                if self._abort_now():
                    self.summary.halted = "已请求立即停止"
                raise bl_chip.ChipError("升级流程返回失败（见上方日志）")

            stage = STAGE_POST
            post = []
            if self.opt.app_version:
                v = self.opt.app_version
                r = bl.cmd("set_meta", bytes([0x02, v[0], v[1], v[2]]), timeout=2.0)
                st = blp._resp_status(r, "set_meta")
                if st != 0:
                    raise bl_chip.ChipError(
                        f"写 APP 版本失败："
                        f"{blp.st_name(st) if st is not None else '无响应'}")
                post.append(f"APP 版本 {v[0]}.{v[1]}.{v[2]}")
            if self.opt.auto_jump:
                r = bl.cmd("jump", timeout=2.0)
                st = blp._resp_status(r, "jump")
                if st != 0:
                    raise bl_chip.ChipError(
                        f"跳转被拒绝：{blp.st_name(st) if st is not None else '无响应'}")
                post.append("已跳转 APP")
            if post:
                self._log("收尾：" + "；".join(post))
            return self._finish(rec, t0, RESULT_OK, stage, "；".join(post) or "已烧录校验")
        except bl_chip.ChipError as e:
            return self._finish(rec, t0, RESULT_FAIL, stage, str(e))
        except (RuntimeError, ValueError, OSError) as e:
            return self._finish(rec, t0, RESULT_FAIL, stage, f"{type(e).__name__}: {e}")

    def _finish(self, rec: dict, t0: float, result: str, stage: str,
                reason: str) -> dict:
        rec["result"] = result
        rec["stage"] = stage
        rec["reason"] = reason
        rec["finished"] = _iso(self._now())
        rec["elapsed_s"] = f"{max(0.0, self._now() - t0):.1f}"
        if result == RESULT_OK:
            self.summary.ok += 1
            if rec.get("uid"):
                self.done_uids.add(rec["uid"])
        elif result == RESULT_SKIP:
            self.summary.skip += 1
        else:
            self.summary.fail += 1
        icon = {RESULT_OK: "✓", RESULT_SKIP: "→", RESULT_FAIL: "✗"}[result]
        self._log(f"[{icon} {result}] 第 {rec['seq']} 台 "
                  f"{rec.get('chip_id') or '?'} {rec.get('uid') or ''} "
                  f"（{rec['elapsed_s']}s）—— {reason}")
        if self.records is not None:
            try:
                self.records.write(rec)
            except OSError as e:
                self._log(f"[!] 记录写入失败：{e}")
        if not self.summary.halted:
            note = ("（已是最新）" if result == RESULT_SKIP
                    else ("（本次已记录）" if result == RESULT_FAIL else ""))
            self._state(result.lower(), f"第 {rec['seq']} 台 {result}{note}")
        self._on_unit(rec)
        if result == RESULT_FAIL and self.opt.halt_on_missing_image \
                and "没有配预设镜像" in reason:
            self.summary.halted = reason
            self._log("[!] 未配置该芯片的预设镜像，会话停止（补齐后可重新开工）")
        return rec
