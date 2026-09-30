#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bl_chip.py — 芯片档案 + 主机侧芯片探查 + 镜像体检（工厂模式与 --chip 参数化的地基）

档案（ChipProfile）只取固件仓 `chips/<id>.json`（ADR-015 构建侧事实源）里上位机需要的
字段，**不新造 schema**。来源按序合并，先到先得、后者补缺：

  1. `--profiles <文件>`（显式指定；单芯片清单或档案包皆可）
  2. 工厂配置的 `chips` 数组（工具旁 `factory/local.json` → `~/.litebootupgrader_factory.json`）
  3. 兄弟仓 `../LiteBootLoader/chips/*.json`（开发机实时事实源）
  4. 内置 `bl_chip_profiles.json`（入库，`chips sync` 生成；打包 exe 单机可用）

探查（协议 VER 0x01 不变、固件无需改动）：BL 协议没有芯片标识字段——GET_INFO 只回
Flash 容量与 96 位 UID，而 UID 是**每片序列号**不是型号。故按「Flash 容量指纹」命中；
容量撞车时用 VERIFY 做**只读**边界探查消歧：`bl_storage_check_app()` 先判
`size > BL_APP_SIZE` 回 RANGE_ERROR、通过才去算 CRC，因此 `size=候选 app_size` 配一个
垃圾 CRC 一次往返即可判定该候选装不装得下，不写 Flash。
（WRITE_CHUNK 不能当探针：固件对 `len == 0` 直接回 RANGE_ERROR。）
代价：2^-32 概率垃圾 CRC 恰好等于该区段真值 → 会被持久化；工厂流程里该设备随即整片
重刷，故可接受，文档已写明。

镜像体检：大小 ≤ 档案 APP 分区、4 字节补齐、向量表首字（初始 MSP）落在档案 SRAM 区间、
次字（Reset Handler）带 Thumb 位且落在档案 APP 区间——挡住「烧了别家芯片的镜像」
与「误把 BootLoader 镜像塞进 APP 区」。
"""
import glob
import hashlib
import json
import os
import struct
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import bl_upgrade as blp

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLE_NAME = "bl_chip_profiles.json"


def app_dir() -> str:
    """工具所在目录：源码运行 = 本文件目录；PyInstaller 打包 = **exe 所在目录**
    （onefile 下 __file__ 指向临时解包目录，不能拿它放本地配置/记录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return _THIS_DIR


def resource_dir() -> str:
    """只读捆绑资源目录：onefile 下是 PyInstaller 的 _MEIPASS 解包目录，否则同 app_dir()。"""
    return getattr(sys, "_MEIPASS", _THIS_DIR)


def bundle_paths() -> list:
    """内置档案包候选路径：先 app_dir（便于在 exe 旁放新版覆盖），再捆绑资源目录。"""
    out = []
    for d in (app_dir(), resource_dir()):
        p = os.path.join(d, BUNDLE_NAME)
        if p not in out:
            out.append(p)
    return out


BUNDLE_PATH = os.path.join(app_dir(), BUNDLE_NAME)
LOCAL_PATH = os.path.join(app_dir(), "factory", "local.json")
USER_PATH = os.path.join(os.path.expanduser("~"), ".litebootupgrader_factory.json")
FIRMWARE_ROOT_SIBLING = os.path.join(os.path.dirname(app_dir()), "LiteBootLoader")

PROBE_CRC = 0xDEADBEEF      # 探查用垃圾 CRC（见模块说明的 2^-32 代价）
ST_OK = 0x00
ST_RANGE_ERROR = 0x03


class ChipError(Exception):
    """档案加载/校验失败——消息带文件与键名，直接面向使用者。"""


def _hex(v, where: str, default=None) -> int:
    if isinstance(v, bool):
        raise ChipError(f"{where}: 布尔不是合法数值：{v!r}")
    if isinstance(v, int):
        return v
    if v is None or v == "":
        if default is None:
            raise ChipError(f"{where}: 缺键或为空")
        return default
    try:
        return int(str(v), 0)
    except ValueError:
        raise ChipError(f"{where}: 非十六进制数值：{v!r}")


def _int(v, where: str, default=None) -> int:
    if v is None:
        if default is None:
            raise ChipError(f"{where}: 缺键")
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ChipError(f"{where}: 非整数：{v!r}")


# ---- 档案 ----

@dataclass
class ChipProfile:
    """一片芯片支持包在上位机侧的几何事实（源自 chips/<id>.json）。"""

    id: str
    name: str                       # device.name（构建侧器件名，如 STM32F103C8）
    family: str = ""
    flash_base: int = 0
    flash_kib: int = 0              # Flash 容量 KiB：GET_INFO 指纹字段口径
    sram_base: int = 0
    sram_size: int = 0
    app_base: int = 0
    app_size: int = 0
    bootloader_size: int = 0
    erase_uniform: bool = True
    erase_entries: list = field(default_factory=list)   # [{size,count,typical_erase_ms}]
    uid_addr: int = 0
    flsize_addr: int = 0
    pack_id: str = ""
    cputype: str = ""
    pyocd_target: str = ""
    example_image: str = ""         # 邻居仓示例镜像（存在才算）
    example_image_rel: str = ""
    source: str = ""                # 档案来源（诊断用）

    @property
    def app_end(self) -> int:
        return self.app_base + self.app_size

    @property
    def flash_size(self) -> int:
        return self.flash_kib * 1024

    def erase_ms(self) -> int:
        """整片擦除的典型耗时合计（进度估算用）。"""
        return sum(e["count"] * e["typical_erase_ms"] for e in self.erase_entries)

    def describe(self) -> str:
        return (f"{self.id:<10} {self.name:<12} "
                f"Flash {self.flash_kib}K @ {self.flash_base:#010x}；"
                f"APP {self.app_size // 1024}K @ {self.app_base:#010x}")

    def to_chip_dict(self) -> dict:
        """回写为 chips/<id>.json 同构字典（档案包格式，供 chips sync 生成）。"""
        if self.erase_uniform and len(self.erase_entries) == 1:
            e = self.erase_entries[0]
            erase = {"uniform": True, "base": f"{self.flash_base:#010x}",
                     "unit_size": f"{e['size']:#x}", "count": e["count"],
                     "typical_erase_ms": e["typical_erase_ms"]}
        else:
            erase = {"uniform": False, "base": f"{self.flash_base:#010x}",
                     "units": [{"size": f"{e['size']:#x}", "count": e["count"],
                                "typical_erase_ms": e["typical_erase_ms"]}
                               for e in self.erase_entries]}
        return {
            "id": self.id,
            "family": self.family,
            "device": {"name": self.name, "pack_id": self.pack_id,
                       "cputype": self.cputype},
            "memory": {"flash_base": f"{self.flash_base:#010x}",
                       "flash_size": f"{self.flash_size:#010x}",
                       "sram_base": f"{self.sram_base:#010x}",
                       "sram_size": f"{self.sram_size:#010x}"},
            "partitions": {"bootloader": {"base": f"{self.flash_base:#010x}",
                                          "size": f"{self.bootloader_size:#x}"},
                           "app": {"base": f"{self.app_base:#010x}",
                                   "size": f"{self.app_size:#x}"}},
            "erase_units": erase,
            "sysmem": {"uid_addr": f"{self.uid_addr:#x}",
                       "flsize_addr": f"{self.flsize_addr:#x}"},
            "build": {"app_example_dir": self.example_image_rel},
            "pyocd_target": self.pyocd_target,
        }


def profile_from_chip_dict(d: dict, source: str = "") -> ChipProfile:
    """chips/<id>.json（或档案包条目）→ ChipProfile；缺键/非法值抛 ChipError。"""
    if not isinstance(d, dict) or not d.get("id"):
        raise ChipError(f"{source or '?'}: 缺少 id 字段，不是芯片清单")
    src = source or f"{d['id']}.json"
    wid = f"{src} [{d['id']}]"
    mem = d.get("memory") or {}
    parts = d.get("partitions") or {}
    app = parts.get("app") or {}
    boot = parts.get("bootloader") or {}
    dev = d.get("device") or {}
    build = d.get("build") or {}
    sysmem = d.get("sysmem") or {}
    erase = d.get("erase_units") or {}

    entries = []
    if erase:
        if erase.get("uniform"):
            entries = [{"size": _hex(erase.get("unit_size"), f"{wid}:erase_units.unit_size"),
                        "count": _int(erase.get("count"), f"{wid}:erase_units.count"),
                        "typical_erase_ms": _int(erase.get("typical_erase_ms"),
                                                 f"{wid}:erase_units.typical_erase_ms", 0)}]
        else:
            for i, u in enumerate(erase.get("units") or []):
                entries.append({"size": _hex(u.get("size"), f"{wid}:erase_units.units[{i}].size"),
                                "count": _int(u.get("count"), f"{wid}:erase_units.units[{i}].count"),
                                "typical_erase_ms": _int(u.get("typical_erase_ms"),
                                                         f"{wid}:erase_units.units[{i}].typical_erase_ms", 0)})

    flash_size = _hex(mem.get("flash_size"), f"{wid}:memory.flash_size")
    name = str(dev.get("name") or d["id"])
    return ChipProfile(
        id=str(d["id"]),
        name=name,
        family=str(d.get("family") or ""),
        flash_base=_hex(mem.get("flash_base"), f"{wid}:memory.flash_base"),
        flash_kib=flash_size // 1024,
        sram_base=_hex(mem.get("sram_base"), f"{wid}:memory.sram_base"),
        sram_size=_hex(mem.get("sram_size"), f"{wid}:memory.sram_size"),
        app_base=_hex(app.get("base"), f"{wid}:partitions.app.base"),
        app_size=_hex(app.get("size"), f"{wid}:partitions.app.size"),
        bootloader_size=_hex(boot.get("size"), f"{wid}:partitions.bootloader.size", 0),
        erase_uniform=bool(erase.get("uniform", True)),
        erase_entries=entries,
        uid_addr=_hex(sysmem.get("uid_addr"), f"{wid}:sysmem.uid_addr", 0),
        flsize_addr=_hex(sysmem.get("flsize_addr"), f"{wid}:sysmem.flsize_addr", 0),
        pack_id=str(dev.get("pack_id") or ""),
        cputype=str(dev.get("cputype") or ""),
        pyocd_target=str(d.get("pyocd_target") or name.lower()),
        example_image_rel=str(build.get("app_example_dir") or ""),
        source=src,
    )


def validate_profile(p: ChipProfile) -> None:
    """几何自检：非法档案直接拒绝加载（宁可报错，不可带病烧录）。"""
    w = p.source or p.id
    if p.flash_kib <= 0 or p.flash_kib > 0xFFFF:
        raise ChipError(f"{w}: Flash 容量 {p.flash_kib} KiB 非法（GET_INFO 为 LE16）")
    if p.app_base < p.flash_base or p.app_end > p.flash_base + p.flash_size:
        raise ChipError(f"{w}: APP 区 [{p.app_base:#x},{p.app_end:#x}) 超出 Flash "
                        f"[{p.flash_base:#x},{p.flash_base + p.flash_size:#x})")
    if p.app_size < 8 or p.app_size % 4:
        raise ChipError(f"{w}: APP 区大小 {p.app_size} 需 ≥8 且 4 字节对齐"
                        "（VERIFY 下界，protocol.md §5.5）")
    if p.sram_size <= 0 or p.sram_base <= 0:
        raise ChipError(f"{w}: SRAM 区间非法（base={p.sram_base:#x} size={p.sram_size}）")
    for e in p.erase_entries:
        if e["size"] <= 0 or e["count"] <= 0:
            raise ChipError(f"{w}: 擦除单元表含非正项 {e}")


# ---- 档案来源与工厂配置 ----

def _read_json(path: str) -> dict:
    try:
        txt = Path(path).read_text(encoding="utf-8")
    except OSError:
        raise ChipError(f"{path}: 读取失败")
    try:
        d = json.loads(txt)
    except ValueError as e:
        raise ChipError(f"{path}: JSON 解析失败：{e}")
    if not isinstance(d, dict):
        raise ChipError(f"{path}: 顶层不是对象")
    return d


def _chip_items(doc: dict) -> list:
    """档案包 {"chips": [...]} 或单芯片清单 {"id": ...} 皆可。"""
    if isinstance(doc.get("chips"), list):
        return doc["chips"]
    if doc.get("id"):
        return [doc]
    return []


def profiles_from_chip_dicts(items, source: str) -> dict:
    out = {}
    for it in items:
        p = profile_from_chip_dict(it, source)
        validate_profile(p)
        out[p.id] = p
    return out


def load_manifest_dir(firmware_root: str) -> dict:
    """固件仓 chips/*.json（只取顶层 *.json：跳过 templates/ 与 per-chip 产物目录）。"""
    out = {}
    wdir = os.path.join(firmware_root, "chips")
    for path in sorted(glob.glob(os.path.join(wdir, "*.json"))):
        doc = _read_json(path)
        if not doc.get("id"):
            continue                     # 非芯片清单（索引/模板等）跳过
        p = profile_from_chip_dict(doc, path)
        validate_profile(p)
        out[p.id] = p
    return out


def load_factory_config(path: str | None = None) -> dict:
    """工厂配置：chips 覆盖 / images 预设 / records / options。
    path=None 时取第一个存在的候选（工具旁 factory/local.json → ~ 用户文件），
    都没有则返回空配置（_path=None）。"""
    for c in ([path] if path else [LOCAL_PATH, USER_PATH]):
        if c and os.path.isfile(c):
            cfg = _read_json(c)
            cfg["_path"] = c
            return cfg
    return {"_path": None}


def save_factory_config(cfg: dict, path: str | None = None) -> bool:
    p = path or cfg.get("_path") or USER_PATH
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        Path(p).write_text(
            json.dumps({k: v for k, v in cfg.items() if not k.startswith("_")},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")
        return True
    except OSError:
        return False


def load_profiles(explicit: str | None = None, firmware_root: str | None = None,
                  local: str | None = None, cfg: dict | None = None,
                  log=None) -> dict:
    """按序（显式 → 工厂配置 → 固件仓 → 内置包）合并档案；先到先得、后者补缺。

    同一 id 在两层里几何不一致时提示一次（先者生效）——这是配置漂移的早期信号。"""
    out = {}

    def _merge(profiles: dict, src: str):
        for k, v in profiles.items():
            if k in out:
                prev = out[k]
                if log and (prev.app_base, prev.app_size, prev.flash_kib) != \
                        (v.app_base, v.app_size, v.flash_kib):
                    log(f"[!] 档案 {k}：{src} 与 {prev.source} 几何不一致，采用先者")
                continue
            out[k] = v

    if explicit:
        _merge(profiles_from_chip_dicts(_chip_items(_read_json(explicit)), explicit),
               explicit)

    cfg = cfg if cfg is not None else load_factory_config(local)
    if isinstance(cfg.get("chips"), list) and cfg["chips"]:
        _merge(profiles_from_chip_dicts(cfg["chips"], cfg.get("_path") or "工厂配置"),
               cfg.get("_path") or "工厂配置")

    root = firmware_root or FIRMWARE_ROOT_SIBLING
    if os.path.isdir(os.path.join(root, "chips")):
        _merge(load_manifest_dir(root), os.path.join(root, "chips"))

    for bp in bundle_paths():
        if os.path.isfile(bp):
            _merge(profiles_from_chip_dicts(_chip_items(_read_json(bp)), bp), bp)
            break

    for p in out.values():
        if p.example_image_rel:
            cand = os.path.normpath(os.path.join(root, p.example_image_rel, "app.bin"))
            p.example_image = cand if os.path.isfile(cand) else ""
    return out


def bundle_dict(profiles: dict) -> dict:
    return {
        "_note": ("内置芯片档案包（生成物，勿手改）：由 `chips sync` 从固件仓 "
                  "chips/*.json 抽取上位机所需字段生成。运行时搜索顺序见 bl_chip.py "
                  "模块说明；本文件保证打包 exe 无固件仓时仍可工作。"),
        "chips": [p.to_chip_dict() for p in sorted(profiles.values(), key=lambda x: x.id)],
    }


def bundle_text(profiles: dict) -> str:
    return json.dumps(bundle_dict(profiles), ensure_ascii=False, indent=2) + "\n"


def sync_bundle(firmware_root: str | None = None, path: str | None = None,
                log=print) -> tuple:
    """从固件仓清单重建内置档案包；返回 (是否变化, 芯片数)。
    缺省写到 app_dir()（源码运行=仓库根；打包 exe=exe 旁，便于随 exe 分发覆盖）。"""
    root = firmware_root or FIRMWARE_ROOT_SIBLING
    path = path or os.path.join(app_dir(), BUNDLE_NAME)
    if not os.path.isdir(os.path.join(root, "chips")):
        raise ChipError(f"固件仓 chips/ 目录不存在：{os.path.join(root, 'chips')}"
                        "（用 --firmware-root 指定）")
    profiles = load_manifest_dir(root)
    if not profiles:
        raise ChipError(f"{os.path.join(root, 'chips')}: 未发现芯片清单")
    text = bundle_text(profiles)
    old = Path(path).read_text(encoding="utf-8") if os.path.isfile(path) else None
    if old == text:
        log(f"内置档案包已是最新（{len(profiles)} 片）：{path}")
        return False, len(profiles)
    Path(path).write_text(text, encoding="utf-8", newline="\n")
    log(f"内置档案包已更新（{len(profiles)} 片）：{path}")
    return True, len(profiles)


# ---- 主机侧探查 ----

@dataclass
class DetectResult:
    profile: object = None          # ChipProfile 或 None
    method: str = "none"            # flash | probe | manual | none | ambiguous
    reason: str = ""
    info: dict = None
    raw: bytes = b""                # GET_INFO 原始响应 DATA（供 parse_info 复显）

    @property
    def ok(self) -> bool:
        return self.profile is not None


def probe_app_boundary(bl, candidates: list, log=print):
    """VERIFY 只读边界探查：从最大候选往下试，第一个「装得下」的即为命中。
    返回 (ChipProfile|None, 说明)。不写 Flash（见模块说明的 2^-32 代价）。"""
    for p in sorted(candidates, key=lambda x: x.app_size, reverse=True):
        r = bl.cmd("verify", struct.pack("<II", p.app_size, PROBE_CRC), timeout=blp.T_VERIFY)
        st = blp._resp_status(r, "verify") if r is not None else None
        if st is None:
            return None, "探查无响应"
        if st == ST_RANGE_ERROR:
            log(f"    排除 {p.id}：APP 区 {p.app_size} B 装不下")
            continue
        log(f"    命中 {p.id}：APP 区 {p.app_size} B 装得下（状态 {blp.st_name(st)}）")
        return p, f"APP 边界探查（VERIFY 只读，{blp.st_name(st)}）"
    return None, "所有候选的 APP 区都不匹配该设备"


def detect_chip(bl, profiles: dict, allow_probe: bool = True, timeout: float = 2.0,
                log=print) -> DetectResult:
    """GET_INFO → Flash 容量指纹 → （撞车时）VERIFY 只读边界探查。"""
    r = bl.cmd("info", timeout=timeout)
    if r is None:
        return DetectResult(reason="GET_INFO 无响应（对端可能在跑 APP 或已拔出）")
    info = blp.parse_info_fields(r["data"])
    raw = bytes(r["data"])
    if info.get("short"):
        return DetectResult(reason=f"短响应（{info['raw_len']}B）——对端疑似 APP 而非 BL",
                            info=info, raw=raw)
    flsz = info["flash_kib"]
    cands = [p for p in profiles.values() if p.flash_kib == flsz]
    if not cands:
        return DetectResult(reason=f"Flash {flsz} KiB 无匹配档案（未收录的支持包？）",
                            info=info, raw=raw)
    if len(cands) == 1:
        return DetectResult(cands[0], "flash",
                            f"Flash 容量指纹 {flsz} KiB", info, raw)
    log(f"Flash {flsz} KiB 命中 {len(cands)} 片候选，转 APP 边界探查…")
    if not allow_probe:
        return DetectResult(method="ambiguous",
                            reason=f"Flash {flsz} KiB 撞车 {len(cands)} 片，且探查已关闭",
                            info=info, raw=raw)
    hit, why = probe_app_boundary(bl, cands, log=log)
    if hit is None:
        return DetectResult(method="ambiguous", reason=why, info=info, raw=raw)
    return DetectResult(hit, "probe", why, info, raw)


def resolve_chip_arg(chip: str | None, bl, profiles: dict, allow_probe: bool = True,
                     log=print):
    """CLI/GUI 的芯片选择 → (profile, 说明)。
    None/"" → (None, "") 按旧行为（模块常量 APP_SIZE）；"auto" → 连线后探查；
    其它 → 按 id 取档案（不连线，人工指定）。"""
    if not chip:
        return None, ""
    if chip == "auto":
        d = detect_chip(bl, profiles, allow_probe=allow_probe, log=log)
        if not d.ok:
            return None, f"自动探查失败：{d.reason}"
        return d.profile, f"自动探查命中 {d.profile.id}（{d.method}：{d.reason}）"
    p = profiles.get(chip)
    if p is None:
        raise ChipError(f"未知芯片 id：{chip}（可选：{', '.join(sorted(profiles)) or '无档案'}）")
    return p, f"人工指定 {p.id}"


# ---- 镜像体检 ----

@dataclass
class ImageCheck:
    ok: bool = False
    msg: str = ""
    path: str = ""
    size: int = 0               # 4 字节补齐后大小（与 run_upgrade 同口径）
    crc32: int = 0
    sha256: str = ""
    data: bytes = b""

    def describe(self) -> str:
        return f"{self.size} B crc32={self.crc32:#010x} sha256={self.sha256[:16]}…"


def check_image(path: str, profile: ChipProfile, force: bool = False) -> ImageCheck:
    """镜像体检：大小上限、4 字节补齐、向量表（MSP/Reset Handler）落在本芯片区间。
    force=True 只跳过向量表与大小判定（工厂应急口），CRC/SHA 仍照算。"""
    c = ImageCheck(path=path)
    try:
        with open(path, "rb") as f:
            img = f.read()
    except OSError as e:
        c.msg = f"读取失败：{e}"
        return c
    if not img:
        c.msg = "空镜像"
        return c
    if len(img) % 4:
        img += b"\xFF" * (4 - len(img) % 4)      # VERIFY 要求 4 字节对齐
    c.data = img
    c.size = len(img)
    c.crc32 = zlib.crc32(img) & 0xFFFFFFFF
    c.sha256 = hashlib.sha256(img).hexdigest()

    size_bad = (len(img) > profile.app_size or len(img) < 8)
    vec_bad, vec_msg = _vector_check(img, profile)
    if force and (size_bad or vec_bad):
        c.ok = True
        c.msg = (f"[!] 已跳过镜像体检（--force-image）："
                 f"{'大小超出 ' + str(profile.app_size) + ' B；' if size_bad else ''}"
                 f"{vec_msg if vec_bad else ''}").rstrip("；")
        return c
    if size_bad:
        c.msg = (f"镜像 {len(img)} B 超出 {profile.id} 的 APP 区 {profile.app_size} B"
                 if len(img) > profile.app_size else f"镜像过小（{len(img)} B < 8 B）")
        return c
    if vec_bad:
        c.msg = vec_msg
        return c
    c.ok = True
    c.msg = "体检通过"
    return c


def _vector_check(img: bytes, profile: ChipProfile):
    """向量表体检：返回 (是否异常, 说明)。"""
    msp, rh = struct.unpack_from("<II", img, 0)
    sram_end = profile.sram_base + profile.sram_size
    if not (profile.sram_base <= msp <= sram_end):
        return True, (f"初始 MSP {msp:#010x} 不在本芯片 SRAM "
                      f"[{profile.sram_base:#010x},{sram_end:#010x}]——疑似他芯片镜像")
    if not (rh & 1):
        return True, f"Reset Handler {rh:#010x} 缺 Thumb 位（bit0=0）——不是合法 Cortex-M 镜像"
    if not (profile.app_base <= (rh & ~1) < profile.app_end):
        return True, (f"Reset Handler {rh:#010x} 不在本芯片 APP 区 "
                      f"[{profile.app_base:#010x},{profile.app_end:#010x})"
                      "——疑似他芯片/他分区镜像（如 BootLoader 自身）")
    return False, ""
