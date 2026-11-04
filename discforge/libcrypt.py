"""Removing LibCrypt protection from PAL PlayStation games, so burned copies keep working.

LibCrypt discs carry deliberately damaged subchannel data that a CD-R can't reproduce; the game
checks it and stops working (or misbehaves) on a copy.  The patches come from LibCrypt Patcher by
Alex Free (BSD 3-Clause, see data/libcrypt/LICENSE): 221 PPF patches for 229 boot files.

On top of the original tool, DiscForge:
  * never touches the user's file: it patches a copy (a reflink copy where the filesystem allows);
  * checks the revision bytes strictly (an unknown revision is left alone instead of patched with
    the wrong patch), the PPF 2.0/3.0 validation block and PPF 3.0 undo data;
  * checks every patched sector's EDC/ECC: the patches were made from whole raw sectors, so a patch
    meant for another revision leaves checksums that don't fit, and it's refused; checksums a
    patch didn't include are recomputed, so every sector on the disc is valid;
  * recognises images that are already patched.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources

import numpy as np

from . import sectors as S
from .util import DcError

RAW = 2352
SYNC = b"\0" + b"\xff" * 10 + b"\0"


class LibCryptError(DcError):
    pass


@lru_cache(maxsize=1)
def _index() -> dict:
    with resources.files("discforge").joinpath("data/libcrypt/index.json").open("rb") as f:
        return json.load(f)


def _ppf_bytes(name: str) -> bytes:
    with resources.files("discforge").joinpath("data/libcrypt/patches.zip").open("rb") as f:
        with zipfile.ZipFile(f) as z:
            return z.read(name)


def lookup(boot_file: str) -> dict | None:
    """Index entry for a boot file such as 'SCES_003.11', or None."""
    return _index()["games"].get(boot_file.upper())


def known_count() -> int:
    return len(_index()["games"])


# --------------------------------------------------------------------------
# PPF 1.0 / 2.0 / 3.0

@dataclass
class Ppf:
    version: int
    description: str
    records: list[tuple[int, bytes, bytes | None]]          # (offset, new bytes, original bytes)
    block: bytes | None = None                               # 1024-byte validation block
    block_offset: int = 0x9320
    file_size: int | None = None


def parse_ppf(data: bytes) -> Ppf:
    magic = data[:5]
    end = data.find(b"@BEGIN_FILE_ID.DIZ")
    end = len(data) if end < 0 else end
    desc = data[6:56].decode("latin-1", "replace").strip()
    recs: list[tuple[int, bytes, bytes | None]] = []
    if magic == b"PPF10":
        pos = 56
        while pos + 5 <= end:
            off, n = struct.unpack_from("<IB", data, pos)
            recs.append((off, data[pos + 5:pos + 5 + n], None))
            pos += 5 + n
        return Ppf(1, desc, recs)
    if magic == b"PPF20":
        size = struct.unpack_from("<I", data, 56)[0]
        block = data[60:1084]
        pos = 1084
        while pos + 5 <= end:
            off, n = struct.unpack_from("<IB", data, pos)
            recs.append((off, data[pos + 5:pos + 5 + n], None))
            pos += 5 + n
        return Ppf(2, desc, recs, block, 0x9320, size)
    if magic == b"PPF30":
        image_type, has_block, has_undo = data[56], data[57], data[58]
        pos = 60
        block = None
        if has_block:
            block = data[60:1084]
            pos = 1084
        while pos + 9 <= end:
            off, n = struct.unpack_from("<QB", data, pos)
            new = data[pos + 9:pos + 9 + n]
            pos += 9 + n
            undo = None
            if has_undo:
                undo = data[pos:pos + n]
                pos += n
            recs.append((off, new, undo))
        return Ppf(3, desc, recs, block, 0x80A0 if image_type == 1 else 0x9320)
    raise LibCryptError("Unknown patch format")


# --------------------------------------------------------------------------
# Sector checks

def _form1_ok(raw: bytes) -> bool:
    return S.encode_mode2_form1(raw[24:2072], raw[16:24]) == raw[16:RAW]


def _fix_sector(raw: bytes) -> bytes:
    """Recompute a raw sector's EDC/ECC (Mode 2 Form 1 or Form 2)."""
    if raw[15] != 2:
        return raw                                       # PlayStation data is always Mode 2
    sub = raw[16:24]
    if not sub[2] & 0x20:                                # Form 1
        return raw[:16] + S.encode_mode2_form1(raw[24:2072], sub)
    old = raw[2348:RAW]
    if old == b"\0\0\0\0":                               # Form 2 without EDC stays that way
        return raw
    arr = np.frombuffer(raw[16:2348], dtype=np.uint8).reshape(1, -1)
    edc = S.edc(arr).astype("<u4").tobytes()
    return raw[:2348] + edc


def _sector_ok(raw: bytes) -> bool:
    if raw[15] != 2:
        return True
    if not raw[18] & 0x20:
        return _form1_ok(raw)
    return _fix_sector(raw) == raw


# --------------------------------------------------------------------------

@dataclass
class PatchResult:
    status: str                       # patched | already | unprotected | unknown-revision | mismatch | not-libcrypt
    title: str = ""
    patch: str = ""
    sectors: int = 0
    fixed: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """The output file can be burned (patched, already patched or no protection)."""
        return self.status in ("patched", "already", "unprotected")


def _read(f, off: int, n: int) -> bytes:
    f.seek(off)
    return f.read(n)


def choose(boot_file: str, bin_path: str) -> tuple[dict | None, dict | None, str]:
    """(index entry, variant, reason) for this image."""
    entry = lookup(boot_file)
    if not entry:
        return None, None, "not-libcrypt"
    variants = entry["variants"]
    if len(variants) == 1 and "check" not in variants[0]:
        return entry, variants[0], ""
    with open(bin_path, "rb") as f:
        for v in variants:
            off, want = v["check"]
            got = _read(f, off, 1)
            if got and got[0] == want:
                return entry, v, ""
    return entry, None, "unknown-revision"


def reflink_copy(src: str, dst: str) -> None:
    """Copy a (large) file, instantly as a reflink on filesystems that can (btrfs, XFS, APFS); a real
    copy needs the free space, which is checked first."""
    if sys.platform.startswith("linux"):
        try:
            r = subprocess.run(["cp", "--reflink=always", "--", src, dst], capture_output=True, timeout=1800)
            if r.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif sys.platform == "darwin":
        try:
            r = subprocess.run(["cp", "-c", src, dst], capture_output=True, timeout=1800)
            if r.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    need = os.path.getsize(src)
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(dst))).free
    if free < need + (128 << 20):
        raise LibCryptError(f"Not enough free disk space for the patched copy ({need / 1e9:.1f} GB needed, "
                            f"{free / 1e9:.1f} GB free).")
    shutil.copyfile(src, dst)


def patch_image(bin_path: str, out_path: str, boot_file: str, progress=None) -> PatchResult:
    """Write a LibCrypt-free copy of a PlayStation data track (raw 2352-byte sectors) to out_path.
    On anything but a successful patch, out_path is not left behind."""
    entry, variant, why = choose(boot_file, bin_path)
    if entry is None:
        return PatchResult("not-libcrypt")
    title = entry.get("title", boot_file)
    if variant is None:
        return PatchResult(why, title, notes=["This copy is a revision LibCrypt Patcher doesn't know, so it "
                                              "was left unpatched."])
    if variant.get("unprotected") or not variant.get("ppf"):
        return PatchResult("unprotected", variant.get("title") or title)
    name = variant["ppf"]
    ppf = parse_ppf(_ppf_bytes(name))
    size = os.path.getsize(bin_path)
    with open(bin_path, "rb") as f:
        if ppf.block is not None:
            have = _read(f, ppf.block_offset, len(ppf.block))
            if have != ppf.block:
                return PatchResult("mismatch", title, name, notes=["The patch's check block doesn't match this "
                                                                   "copy of the game."])
        if any(off + len(new) > size for off, new, _u in ppf.records):
            return PatchResult("mismatch", title, name, notes=["The patch reaches past the end of this image."])
        same = all(_read(f, off, len(new)) == new for off, new, _u in ppf.records)
        if same:
            return PatchResult("already", title, name)
        for off, new, undo in ppf.records:
            if undo is not None and _read(f, off, len(undo)) not in (undo, new):
                return PatchResult("mismatch", title, name, notes=["This copy's original bytes differ from what "
                                                                   "the patch expects."])
    if progress:
        progress(0.05)
    tmp = out_path + ".part"
    touched: dict[int, set[int]] = {}
    try:
        reflink_copy(bin_path, tmp)
        if progress:
            progress(0.6)
        with open(tmp, "r+b") as f:
            for off, new, _u in ppf.records:
                f.seek(off)
                f.write(new)
                for o in range(off, off + len(new)):
                    touched.setdefault(o // RAW, set()).add(o % RAW)
            fixed = 0
            for sec, offs in sorted(touched.items()):
                raw = _read(f, sec * RAW, RAW)
                if len(raw) != RAW or raw[:12] != SYNC:
                    continue
                if _sector_ok(raw):
                    continue
                wrote_checks = any(o >= 2072 for o in offs) if not raw[18] & 0x20 else any(o >= 2348 for o in offs)
                if wrote_checks:
                    raise LibCryptError("mismatch")
                f.seek(sec * RAW)
                f.write(_fix_sector(raw))
                fixed += 1
    except LibCryptError:
        os.unlink(tmp)
        return PatchResult("mismatch", title, name, notes=["The patch doesn't fit this copy of the game (its "
                                                           "checksums don't match), so it was left unpatched."])
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    os.replace(tmp, out_path)
    if progress:
        progress(1.0)
    return PatchResult("patched", variant.get("title") or title, name, len(touched), fixed)
