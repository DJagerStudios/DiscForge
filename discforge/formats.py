"""Disc-image containers -> plain images.

Native, random-access readers (so a game can be identified without unpacking it):
  WBFS (+ .wbf1.. splits)  Wii backup-loader format, 2 MiB blocks with a block map
  CSO / ZSO                compressed ISO (deflate / LZ4 blocks), mostly PS2 (OPL) images
  GCZ                      Dolphin's zlib-compressed GameCube/Wii images
  ECM                      Error Code Modeler (sectors rebuilt with EDC/ECC; whole-file checksum checked)
Tool-backed:
  RVZ / WIA                dolphin-tool convert (header parsed natively for identification)
  CHD                      chdman extractcd / extractdvd (MAME tools)
"""
from __future__ import annotations

import os
import re
import struct
import subprocess
import threading
import zlib
from typing import Callable

from .util import DcError
from . import tools

Progress = Callable[[float], None]


class FormatError(DcError):
    pass


class Cancel:
    """Cooperative cancel flag shared with long conversions."""

    def __init__(self):
        self._ev = threading.Event()

    def set(self):
        self._ev.set()

    def __bool__(self):
        return self._ev.is_set()


def _check(cancel) -> None:
    if cancel:
        raise FormatError("Cancelled.")


# --------------------------------------------------------------------------
# Random-access readers

class Reader:
    size: int = 0
    path: str = ""

    def read(self, off: int, n: int) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def copy_to(self, out: str, progress: Progress | None = None, cancel=None, size: int | None = None,
                chunk: int = 4 << 20) -> str:
        """Write the plain image to `out` (zero blocks become sparse holes)."""
        total = self.size if size is None else size
        zero = bytes(chunk)
        with open(out, "wb") as f:
            f.truncate(total)
            pos = 0
            while pos < total:
                _check(cancel)
                n = min(chunk, total - pos)
                data = self.read(pos, n)
                if data != zero[:len(data)]:
                    f.seek(pos)
                    f.write(data)
                pos += n
                if progress:
                    progress(pos / total)
        return out


class FileReader(Reader):
    def __init__(self, path: str, offset: int = 0, size: int | None = None):
        self.path = path
        self.f = open(path, "rb")
        self.offset = offset
        self.size = (os.path.getsize(path) - offset) if size is None else size

    def read(self, off: int, n: int) -> bytes:
        if off >= self.size:
            return b""
        n = min(n, self.size - off)
        self.f.seek(self.offset + off)
        return self.f.read(n)

    def close(self):
        self.f.close()


class ConcatReader(Reader):
    """Several split files read as one (game.wbfs + game.wbf1 + ...)."""

    def __init__(self, paths: list[str]):
        self.path = paths[0]
        self.files = [open(p, "rb") for p in paths]
        self.starts = []
        pos = 0
        for p in paths:
            self.starts.append(pos)
            pos += os.path.getsize(p)
        self.size = pos

    def read(self, off: int, n: int) -> bytes:
        out = bytearray()
        while n > 0 and off < self.size:
            i = max(k for k, s in enumerate(self.starts) if s <= off)
            f = self.files[i]
            f.seek(off - self.starts[i])
            end = self.starts[i + 1] if i + 1 < len(self.starts) else self.size
            data = f.read(min(n, end - off))
            if not data:
                break
            out += data
            off += len(data)
            n -= len(data)
        return bytes(out)

    def close(self):
        for f in self.files:
            f.close()


# --------------------------------------------------------------------------
# WBFS

WII_SECTOR = 0x8000
NUM_WII_SECTORS = 143432 * 2
WII_SL_SIZE = 4699979776
WII_DL_SIZE = 8511160320


def wbfs_parts(path: str) -> list[str]:
    parts = [path]
    base = path[:-1] if path.lower().endswith(".wbfs") else path
    for i in range(1, 10):
        p = f"{base}{i}"                     # game.wbf1, game.wbf2 ...
        if not os.path.exists(p):
            break
        parts.append(p)
    return parts


class WbfsReader(Reader):
    """The Wii disc inside a .wbfs file (first disc slot that's in use)."""

    def __init__(self, path: str):
        self.path = path
        self.src = ConcatReader(wbfs_parts(path))
        head = self.src.read(0, 512)
        if head[:4] != b"WBFS":
            raise FormatError("Not a WBFS file")
        hd_shift, blk_shift = head[8], head[9]
        if not (9 <= hd_shift <= 12 and 15 <= blk_shift <= 30):
            raise FormatError("This WBFS file's header is damaged")
        self.hd_sec = 1 << hd_shift
        self.blk = 1 << blk_shift
        nblocks = NUM_WII_SECTORS >> (blk_shift - 15)
        info_sz = -(-(0x100 + nblocks * 2) // self.hd_sec) * self.hd_sec
        table = head[12:self.hd_sec] if self.hd_sec <= 512 else self.src.read(12, self.hd_sec - 12)
        slot = next((i for i, b in enumerate(table) if b), None)
        if slot is None:
            raise FormatError("This WBFS file contains no game")
        info = self.src.read(self.hd_sec * (1 + slot * (info_sz // self.hd_sec)), info_sz)
        self.header = info[:0x100]
        self.map = [struct.unpack_from(">H", info, 0x100 + 2 * i)[0] for i in range(nblocks)]
        last = max((i for i, b in enumerate(self.map) if b), default=0)
        self.size = WII_SL_SIZE if (last + 1) * self.blk <= WII_SL_SIZE else WII_DL_SIZE
        if self.header[0x18:0x1C] != b"\x5d\x1c\x9e\xa3":
            raise FormatError("The WBFS file doesn't contain a Wii game")

    def read(self, off: int, n: int) -> bytes:
        out = bytearray()
        end = min(off + n, self.size)
        while off < end:
            b, inner = divmod(off, self.blk)
            take = min(end - off, self.blk - inner)
            phys = self.map[b] if b < len(self.map) else 0
            out += self.src.read(phys * self.blk + inner, take) if phys else bytes(take)
            off += take
        return bytes(out)

    def copy_to(self, out: str, progress: Progress | None = None, cancel=None, size: int | None = None,
                chunk: int = 0) -> str:
        used = [i for i, b in enumerate(self.map) if b and i * self.blk < self.size]
        with open(out, "wb") as f:
            f.truncate(self.size)
            for k, i in enumerate(used):
                _check(cancel)
                f.seek(i * self.blk)
                f.write(self.src.read(self.map[i] * self.blk, min(self.blk, self.size - i * self.blk)))
                if progress:
                    progress((k + 1) / len(used))
        return out

    def close(self):
        self.src.close()


# --------------------------------------------------------------------------
# CSO / ZSO

class CsoReader(Reader):
    def __init__(self, path: str):
        self.path = path
        self.f = open(path, "rb")
        h = self.f.read(24)
        self.magic = h[:4]
        if self.magic not in (b"CISO", b"ZISO"):
            raise FormatError("Not a CSO/ZSO file")
        _hsize, self.size, self.block = struct.unpack_from("<IQI", h, 4)
        self.ver, self.align = h[20], h[21]
        if not self.block or self.block & (self.block - 1):
            raise FormatError("This CSO file's header is damaged")
        n = -(-self.size // self.block)
        self.index = struct.unpack(f"<{n + 1}I", self.f.read(4 * (n + 1)))
        self._cache: tuple[int, bytes] | None = None

    def _block(self, i: int) -> bytes:
        if self._cache and self._cache[0] == i:
            return self._cache[1]
        a, b = self.index[i], self.index[i + 1]
        flag = a & 0x80000000
        pos, end = (a & 0x7FFFFFFF) << self.align, (b & 0x7FFFFFFF) << self.align
        self.f.seek(pos)
        raw = self.f.read(end - pos)
        want = min(self.block, self.size - i * self.block)
        if self.magic == b"CISO" and self.ver < 2:
            data = raw[:want] if flag else zlib.decompressobj(-15).decompress(raw)
        elif self.magic == b"ZISO":
            data = raw[:want] if flag else _lz4(raw, self.block)
        else:                                   # CSO v2: flag = LZ4, full-size block = stored
            if len(raw) >= self.block:
                data = raw[:want]
            else:
                data = _lz4(raw, self.block) if flag else zlib.decompressobj(-15).decompress(raw)
        data = data[:want]
        self._cache = (i, data)
        return data

    def read(self, off: int, n: int) -> bytes:
        out = bytearray()
        end = min(off + n, self.size)
        while off < end:
            i, inner = divmod(off, self.block)
            blk = self._block(i)
            take = min(end - off, len(blk) - inner)
            if take <= 0:
                raise FormatError("This CSO file is damaged (short block)")
            out += blk[inner:inner + take]
            off += take
        return bytes(out)

    def close(self):
        self.f.close()


def _lz4(raw: bytes, size: int) -> bytes:
    try:
        import lz4.block
    except ImportError:
        raise FormatError("LZ4 support is missing (sudo pacman -S python-lz4)") from None
    return lz4.block.decompress(raw, uncompressed_size=size)


# --------------------------------------------------------------------------
# GCZ (Dolphin compressed GameCube/Wii)

class GczReader(Reader):
    def __init__(self, path: str):
        self.path = path
        self.f = open(path, "rb")
        h = self.f.read(32)
        magic, _sub, self.csize, self.size, self.block, n = struct.unpack("<IIQQII", h)
        if magic != 0xB10BC001:
            raise FormatError("Not a GCZ file")
        self.ptrs = struct.unpack(f"<{n}Q", self.f.read(8 * n))
        self.f.seek(4 * n, 1)                  # adler32 hashes
        self.data_off = 32 + 12 * n
        self._cache: tuple[int, bytes] | None = None

    def _block(self, i: int) -> bytes:
        if self._cache and self._cache[0] == i:
            return self._cache[1]
        p = self.ptrs[i]
        raw_flag = p >> 63
        start = p & ~(1 << 63)
        end = (self.ptrs[i + 1] & ~(1 << 63)) if i + 1 < len(self.ptrs) else self.csize
        self.f.seek(self.data_off + start)
        raw = self.f.read(end - start)
        data = raw if raw_flag else zlib.decompress(raw)
        self._cache = (i, data)
        return data

    def read(self, off: int, n: int) -> bytes:
        out = bytearray()
        end = min(off + n, self.size)
        while off < end:
            i, inner = divmod(off, self.block)
            blk = self._block(i)
            take = min(end - off, len(blk) - inner)
            if take <= 0:
                raise FormatError("This GCZ file is damaged (short block)")
            out += blk[inner:inner + take]
            off += take
        return bytes(out)

    def close(self):
        self.f.close()


# --------------------------------------------------------------------------
# RVZ / WIA (identification here, conversion through dolphin-tool)

def wia_header(path: str) -> dict:
    with open(path, "rb") as f:
        h = f.read(0x48 + 16 + 0x80)
    if h[:4] not in (b"WIA\x01", b"RVZ\x01"):
        raise FormatError("Not a WIA/RVZ file")
    iso_size = struct.unpack_from(">Q", h, 0x24)[0]
    disc_type = struct.unpack_from(">I", h, 0x48)[0]
    dhead = h[0x58:0x58 + 0x80]
    return {"format": h[:3].decode(), "platform": "wii" if disc_type == 2 else "gamecube",
            "iso_size": iso_size, "header": dhead}


def dolphin_tool() -> list[str] | None:
    cmd = tools.dolphin_tool()
    return list(cmd) if cmd else None


def rvz_to_iso(path: str, out: str, progress: Progress | None = None, cancel=None) -> str:
    cmd = dolphin_tool()
    if not cmd:
        raise FormatError("Converting RVZ/WIA images needs the Dolphin emulator installed (package dolphin-emu).")
    total = wia_header(path)["iso_size"] or 1
    proc = subprocess.Popen(cmd + ["convert", "-i", path, "-o", out, "-f", "iso"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                            env=tools.env())
    try:
        while proc.poll() is None:
            if cancel:
                proc.terminate()
                proc.wait()
                raise FormatError("Cancelled.")
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            if progress and os.path.exists(out):
                progress(min(0.99, os.path.getsize(out) / total))
        msg = proc.stdout.read() if proc.stdout else ""
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0 or not os.path.exists(out):
        raise FormatError("dolphin-tool couldn't convert the image: " + (msg.strip().splitlines() or ["?"])[-1])
    if progress:
        progress(1.0)
    return out


# --------------------------------------------------------------------------
# CHD

def chd_info(path: str) -> dict:
    tool = tools.find("chdman")
    if not tool:
        raise FormatError("CHD images need chdman (sudo pacman -S mame-tools)")
    r = subprocess.run([tool, "info", "-i", path], capture_output=True, text=True, errors="replace",
                       stdin=subprocess.DEVNULL, timeout=60, env=tools.env())
    out = r.stdout + r.stderr
    if r.returncode != 0:
        raise FormatError("This CHD file is damaged or incomplete (maybe the download didn't finish). chdman said: "
                          + (out.strip().splitlines() or ["?"])[-1])
    tags = re.findall(r"Tag='(.{4})'", out)
    kind = ("gd" if "CHGD" in tags else "dvd" if "DVD " in tags
            else "cd" if {"CHT2", "CHTR", "CHCD"} & set(tags) else "hd")
    size = re.search(r"Logical size:\s*([\d,]+)", out)
    return {"kind": kind, "logical_size": int(size.group(1).replace(",", "")) if size else 0, "raw": out}


def _run_progress(args: list[str], progress: Progress | None, cancel, rx: str = r"(\d+(?:\.\d+)?)%") -> str:
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            env=tools.env())
    buf = b""
    tail = []
    assert proc.stdout
    try:
        while True:
            if cancel:
                proc.terminate()
                proc.wait()
                raise FormatError("Cancelled.")
            ch = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(256)
            if not ch:
                break
            buf += ch
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for p in parts:
                line = p.decode("utf-8", "replace").strip()
                if not line:
                    continue
                tail = (tail + [line])[-8:]
                m = re.search(rx, line)
                if m and progress:
                    progress(min(1.0, float(m.group(1)) / 100))
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
    if proc.returncode != 0:
        raise FormatError(f"{os.path.basename(args[0])} failed: " + (tail[-1] if tail else f"exit {proc.returncode}"))
    return "\n".join(tail)


def chd_extract(path: str, outdir: str, progress: Progress | None = None, cancel=None) -> str:
    """Unpack a CD or DVD CHD; returns the .cue or .iso path."""
    info = chd_info(path)
    stem = os.path.join(outdir, re.sub(r"[^\w.-]+", "_", os.path.splitext(os.path.basename(path))[0])[:60] or "disc")
    tool = tools.find("chdman")
    if info["kind"] == "dvd":
        out = stem + ".iso"
        try:
            _run_progress([tool, "extractdvd", "-f", "-i", path, "-o", out], progress, cancel)
        except FormatError as e:            # chdman before 0.255 has no extractdvd: the raw data is the ISO
            if str(e) == "Cancelled.":
                raise
            _run_progress([tool, "extractraw", "-f", "-i", path, "-o", out], progress, cancel)
        return out
    if info["kind"] == "hd" and info["logical_size"] and info["logical_size"] % 2048 == 0:
        # DVD images packed with createhd (common for PS2 before chdman had createdvd)
        out = stem + ".iso"
        _run_progress([tool, "extracthd", "-f", "-i", path, "-o", out], progress, cancel)
        return out
    if info["kind"] == "cd":
        out = stem + ".cue"
        _run_progress([tool, "extractcd", "-f", "-i", path, "-o", out, "-ob", stem + ".bin"], progress, cancel)
        return out
    raise FormatError("This CHD isn't a CD or DVD image")


# --------------------------------------------------------------------------
# ECM

CD_SYNC = b"\x00" + b"\xff" * 10 + b"\x00"


_ECM_IN = {1: 3 + 2048, 2: 4 + 2048, 3: 4 + 2324}       # stored bytes per sector, by record type
_ECM_OUT = {1: 2352, 2: 2336, 3: 2336}                  # bytes each sector expands to
_ECM_SHORT = "This ECM file ends early (the download may be incomplete)."
_ECM_BAD = "This ECM file is damaged. Download it again."
_ECM_LIT_STEP, _ECM_SEC_STEP, _ECM_BATCH = 4 << 20, 4096, 16 << 20     # tests shrink these


def _ecm_records(m, end: int):
    """Walk the record stream: yields (type, offset, count) pieces (big records are split) and finally
    (None, offset of the trailing checksum, 0)."""
    pos = 4
    while True:
        if pos >= end:
            raise FormatError(_ECM_SHORT)
        c = m[pos]
        pos += 1
        kind, num, bits = c & 3, (c >> 2) & 0x1F, 5
        while c & 0x80:
            if pos >= end:
                raise FormatError(_ECM_SHORT)
            if bits > 26:
                raise FormatError(_ECM_BAD)
            c = m[pos]
            pos += 1
            num |= (c & 0x7F) << bits
            bits += 7
        if num == 0xFFFFFFFF:
            yield None, pos, 0
            return
        num += 1
        if num >= 0x80000000:
            raise FormatError(_ECM_BAD)
        unit = 1 if kind == 0 else _ECM_IN[kind]
        if pos + num * unit > end:
            raise FormatError(_ECM_SHORT)
        step = _ECM_LIT_STEP if kind == 0 else _ECM_SEC_STEP
        while num:
            k = min(num, step)
            yield kind, pos, k
            pos += k * unit
            num -= k


def _ecm_sectors(kind: int, data):
    """Rebuild whole sectors (sync, header, EDC, ECC) from their stored parts, many at once."""
    import numpy as np
    from . import sectors as S
    n = data.shape[0]
    raw = np.zeros((n, 2352), dtype=np.uint8)
    if kind == 1:                                       # Mode 1: the full 2352-byte sector
        raw[:, :12] = np.frombuffer(CD_SYNC, dtype=np.uint8)
        raw[:, 12:15] = data[:, :3]
        raw[:, 15] = 1
        raw[:, 16:2064] = data[:, 3:]
        raw[:, 2064:2068] = S.edc(raw[:, :2064]).astype("<u4").view(np.uint8).reshape(n, 4)
    else:                                               # Mode 2: 2336 bytes from the subheader on
        raw[:, 16:20] = data[:, :4]
        raw[:, 20:24] = data[:, :4]
        if kind == 2:
            raw[:, 24:2072] = data[:, 4:]
            raw[:, 2072:2076] = S.edc(raw[:, 16:2072]).astype("<u4").view(np.uint8).reshape(n, 4)
        else:
            raw[:, 24:2348] = data[:, 4:]
            raw[:, 2348:2352] = S.edc(raw[:, 16:2348]).astype("<u4").view(np.uint8).reshape(n, 4)
            return raw[:, 16:]
    raw[:, 2076:2248] = S._ecc_block(raw[:, 12:2076], S._P_IDX, 86)   # Mode 2 ECC: header counted as zero
    raw[:, 2248:2352] = S._ecc_block(raw[:, 12:2248], S._Q_IDX, 52)
    return raw if kind == 1 else raw[:, 16:]


class _EdcStream:
    """The CD EDC (a CRC-32) of a long byte stream, fed in big numpy chunks: rows of ROW bytes get
    their CRCs in one vectorised pass and are then chained with a precomputed 'skip ROW zero bytes'
    table, so a 700 MB image takes a second or two instead of minutes."""
    ROW = 2352

    def __init__(self):
        import numpy as np
        from . import sectors as S
        lut = S._EDC_LUT
        c = np.array([b << (8 * j) for j in range(4) for b in range(256)], dtype=np.uint32)
        for _ in range(self.ROW):
            c = (c >> np.uint32(8)) ^ lut[c & np.uint32(0xFF)]
        self._skip = [c[j * 256:(j + 1) * 256].tolist() for j in range(4)]
        self._lut = lut.tolist()
        self._tail = np.zeros(0, dtype=np.uint8)
        self.crc = 0

    def update(self, arr) -> None:
        import numpy as np
        from . import sectors as S
        if self._tail.size:
            arr = np.concatenate([self._tail, arr])
        n = arr.size // self.ROW
        if n:
            rows = S.edc(arr[:n * self.ROW].reshape(n, self.ROW).T.copy().T)
            t0, t1, t2, t3 = self._skip
            acc = self.crc
            for r in rows.tolist():
                acc = t0[acc & 255] ^ t1[(acc >> 8) & 255] ^ t2[(acc >> 16) & 255] ^ t3[acc >> 24] ^ r
            self.crc = acc
        self._tail = arr[n * self.ROW:].copy()

    def value(self) -> int:
        acc, lut = self.crc, self._lut
        for b in self._tail.tolist():
            acc = (acc >> 8) ^ lut[(acc ^ b) & 255]
        return acc


def ecm_decode_native(path: str, out: str, progress: Progress | None = None, cancel=None) -> str:
    """Decode an ECM file (Neill Corlett's Error Code Modeler) without any external tool.  Literal runs
    are copied; Mode 1 / Mode 2 Form 1 / Form 2 sectors get their sync, EDC and ECC rebuilt; the
    checksum of the whole result is compared with the one stored at the end, like unecm does."""
    import mmap
    import numpy as np
    size = os.path.getsize(path)
    if size < 9:
        with open(path, "rb") as f:
            raise FormatError(_ECM_SHORT if f.read(4) == b"ECM\0" else "This isn't an ECM file.")
    with open(path, "rb") as fi:
        m = mmap.mmap(fi.fileno(), 0, access=mmap.ACCESS_READ)
    src = None
    try:
        if m[:4] != b"ECM\0":
            raise FormatError("This isn't an ECM file.")
        src = np.frombuffer(m, dtype=np.uint8)
        edc = _EdcStream()
        records = _ecm_records(m, size)
        with open(out, "wb") as fo:
            end = None
            while end is None:
                _check(cancel)
                pieces, total = [], 0
                for kind, pos, k in records:
                    if kind is None:
                        end = pos
                        break
                    pieces.append((kind, pos, k))
                    total += k if kind == 0 else k * _ECM_OUT[kind]
                    if total >= _ECM_BATCH:
                        break
                ob = np.empty(total, dtype=np.uint8)
                typed: dict[int, list] = {}
                o = 0
                for kind, pos, k in pieces:
                    if kind == 0:
                        ob[o:o + k] = src[pos:pos + k]
                        o += k
                    else:
                        typed.setdefault(kind, []).append((pos, k, o))
                        o += k * _ECM_OUT[kind]
                for kind, runs in typed.items():
                    unit, width = _ECM_IN[kind], _ECM_OUT[kind]
                    data = np.concatenate([src[p:p + k * unit] for p, k, _ in runs]).reshape(-1, unit)
                    secs = _ecm_sectors(kind, data)
                    i = 0
                    for _, k, o2 in runs:
                        ob[o2:o2 + k * width] = secs[i:i + k].reshape(-1)
                        i += k
                edc.update(ob)
                fo.write(ob.data)
                if progress:
                    progress(min(0.999, (pieces[-1][1] if pieces else size) / size))
            stored = m[end:end + 4]
            if len(stored) != 4:
                raise FormatError(_ECM_SHORT)
            if int.from_bytes(stored, "little") != edc.value():
                raise FormatError(_ECM_BAD + " (Its checksum doesn't match.)")
    finally:
        src = None
        m.close()
    if progress:
        progress(1.0)
    return out


ecm_decode = ecm_decode_native


# --------------------------------------------------------------------------

def open_reader(path: str) -> Reader:
    """Random-access view of the plain disc image inside `path`."""
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic == b"WBFS":
        return WbfsReader(path)
    if magic in (b"CISO", b"ZISO"):
        return CsoReader(path)
    if magic == b"\x01\xc0\x0b\xb1":
        return GczReader(path)
    return FileReader(path)
