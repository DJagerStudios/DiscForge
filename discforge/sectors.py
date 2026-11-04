"""CD sector formats, sector sources and Mode 2 EDC/ECC generation.

A *sector source* hands out the user data of a run of sectors:
2048 bytes per sector for data tracks, 2352 bytes (raw PCM) for audio tracks.
Everything above this layer (planning, burning, CDI writing) only ever sees
user data, so the on-disk storage format of the input image does not matter.
"""
from __future__ import annotations

import os
from typing import Iterator

import numpy as np

AUDIO = "audio"
MODE1 = "mode1"
MODE2 = "mode2"  # CD-ROM XA Mode 2 Form 1 (2048 bytes of user data)

RAW_SIZE = 2352
USER_SIZE = 2048
MODE2_SIZE = 2336  # Mode 2 sector without sync/header (subheader + data + EDC/ECC)

# Subheader used for data sectors we generate (file 0, channel 0, submode DATA).
DATA_SUBHEADER = bytes([0x00, 0x00, 0x08, 0x00] * 2)
FORM2_SUBHEADER = bytes([0x00, 0x00, 0x20, 0x00] * 2)

MODE_NAMES = {AUDIO: "Audio", MODE1: "Mode 1", MODE2: "Mode 2 XA"}


def user_bytes(mode: str) -> int:
    return RAW_SIZE if mode == AUDIO else USER_SIZE


def user_layout(mode: str, stored_size: int) -> tuple[int, int, int | None]:
    """Return (user offset, user size, subheader offset or None) for a stored sector."""
    if mode == AUDIO:
        if stored_size not in (2352, 2448):
            raise ValueError(f"audio track with {stored_size}-byte sectors")
        return 0, RAW_SIZE, None
    if stored_size == 2048:
        return 0, USER_SIZE, None
    if mode == MODE1:
        if stored_size in (2352, 2448):
            return 16, USER_SIZE, None
        raise ValueError(f"Mode 1 track with {stored_size}-byte sectors")
    if mode == MODE2:
        if stored_size == 2336:
            return 8, USER_SIZE, 0
        if stored_size in (2352, 2448):
            return 24, USER_SIZE, 16
        raise ValueError(f"Mode 2 track with {stored_size}-byte sectors")
    raise ValueError(f"unknown track mode {mode!r}")


# --------------------------------------------------------------------------
# Sector sources


class SectorSource:
    """Base class: `count` sectors of `sector_bytes` user data each."""

    count: int = 0
    sector_bytes: int = USER_SIZE

    def read(self, start: int, n: int) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def iter_chunks(self, chunk: int = 2048) -> Iterator[bytes]:
        pos = 0
        while pos < self.count:
            n = min(chunk, self.count - pos)
            yield self.read(pos, n)
            pos += n

    def form2_flags(self, start: int, n: int) -> list[bool]:
        """True for sectors stored as XA Form 2 (only meaningful for Mode 2)."""
        return [False] * n

    def _check(self, start: int, n: int) -> None:
        if start < 0 or n < 0 or start + n > self.count:
            raise IndexError(f"sector range {start}+{n} outside 0..{self.count}")


class FileSectorSource(SectorSource):
    """Sectors stored back to back in a file (CDI track, GDI track, plain ISO...)."""

    def __init__(self, path: str | os.PathLike, offset: int, count: int,
                 stored_size: int, mode: str):
        self.path = os.fspath(path)
        self.offset = offset
        self.count = count
        self.stored_size = stored_size
        self.mode = mode
        self.sector_bytes = user_bytes(mode)
        self.user_off, self.user_size, self.sub_off = user_layout(mode, stored_size)

    def _raw(self, start: int, n: int) -> bytes:
        self._check(start, n)
        with open(self.path, "rb") as f:
            f.seek(self.offset + start * self.stored_size)
            data = f.read(n * self.stored_size)
        if len(data) != n * self.stored_size:
            raise IOError(f"{self.path}: image is truncated (wanted sector {start + n - 1})")
        return data

    def read(self, start: int, n: int) -> bytes:
        if n == 0:
            return b""
        data = self._raw(start, n)
        if self.user_off == 0 and self.user_size == self.stored_size:
            return data
        arr = np.frombuffer(data, dtype=np.uint8).reshape(n, self.stored_size)
        return arr[:, self.user_off:self.user_off + self.user_size].tobytes()

    def form2_flags(self, start: int, n: int) -> list[bool]:
        if self.sub_off is None or n == 0:
            return [False] * n
        arr = np.frombuffer(self._raw(start, n), dtype=np.uint8).reshape(n, self.stored_size)
        return [bool(x) for x in (arr[:, self.sub_off + 2] & 0x20)]


class ZeroSource(SectorSource):
    def __init__(self, count: int, sector_bytes: int = USER_SIZE):
        self.count = count
        self.sector_bytes = sector_bytes

    def read(self, start: int, n: int) -> bytes:
        self._check(start, n)
        return bytes(n * self.sector_bytes)


class BytesSource(SectorSource):
    def __init__(self, data: bytes, sector_bytes: int = USER_SIZE):
        if len(data) % sector_bytes:
            data = data + bytes(sector_bytes - len(data) % sector_bytes)
        self.data = data
        self.sector_bytes = sector_bytes
        self.count = len(data) // sector_bytes

    def read(self, start: int, n: int) -> bytes:
        self._check(start, n)
        b = self.sector_bytes
        return self.data[start * b:(start + n) * b]


class SliceSource(SectorSource):
    """A window of another source (used to trim run-out blocks)."""

    def __init__(self, src: SectorSource, start: int, count: int):
        if start < 0 or count < 0 or start + count > src.count:
            raise ValueError("slice outside source")
        self.src, self.start, self.count = src, start, count
        self.sector_bytes = src.sector_bytes

    def read(self, start: int, n: int) -> bytes:
        self._check(start, n)
        return self.src.read(self.start + start, n)

    def form2_flags(self, start: int, n: int) -> list[bool]:
        self._check(start, n)
        return self.src.form2_flags(self.start + start, n)


class ConcatSource(SectorSource):
    def __init__(self, parts: list[SectorSource]):
        parts = [p for p in parts if p.count]
        sizes = {p.sector_bytes for p in parts}
        if len(sizes) > 1:
            raise ValueError("cannot concatenate audio and data sectors")
        self.parts = parts
        self.sector_bytes = sizes.pop() if sizes else USER_SIZE
        self.count = sum(p.count for p in parts)

    def _spans(self, start: int, n: int):
        self._check(start, n)
        pos = 0
        for p in self.parts:
            lo, hi = max(start, pos), min(start + n, pos + p.count)
            if lo < hi:
                yield p, lo - pos, hi - lo
            pos += p.count

    def read(self, start: int, n: int) -> bytes:
        return b"".join(p.read(s, k) for p, s, k in self._spans(start, n))

    def form2_flags(self, start: int, n: int) -> list[bool]:
        out: list[bool] = []
        for p, s, k in self._spans(start, n):
            out += p.form2_flags(s, k)
        return out


class OverlaySource(SectorSource):
    """Replace some sectors of another source (IP.BIN region/VGA patches)."""

    def __init__(self, src: SectorSource, start: int, data: bytes):
        if len(data) % src.sector_bytes:
            raise ValueError("overlay must be whole sectors")
        self.src, self.ostart, self.odata = src, start, data
        self.ocount = len(data) // src.sector_bytes
        self.count, self.sector_bytes = src.count, src.sector_bytes
        if start + self.ocount > src.count:
            raise ValueError("overlay outside source")

    def read(self, start: int, n: int) -> bytes:
        self._check(start, n)
        buf = bytearray(self.src.read(start, n))
        b = self.sector_bytes
        lo, hi = max(start, self.ostart), min(start + n, self.ostart + self.ocount)
        if lo < hi:
            buf[(lo - start) * b:(hi - start) * b] = self.odata[(lo - self.ostart) * b:(hi - self.ostart) * b]
        return bytes(buf)

    def form2_flags(self, start: int, n: int) -> list[bool]:
        return self.src.form2_flags(start, n)


def fit(src: SectorSource, count: int) -> SectorSource:
    """Trim or zero-pad a source to exactly `count` sectors."""
    if count == src.count:
        return src
    if count < src.count:
        return SliceSource(src, 0, count)
    return ConcatSource([src, ZeroSource(count - src.count, src.sector_bytes)])


def trailing_form2(src: SectorSource, limit: int = 16) -> int:
    """Number of XA Form 2 sectors at the end of a Mode 2 track (run-out/link blocks)."""
    n = min(limit, src.count)
    if n == 0:
        return 0
    flags = src.form2_flags(src.count - n, n)
    k = 0
    for f in reversed(flags):
        if not f:
            break
        k += 1
    return k


# --------------------------------------------------------------------------
# EDC / ECC (ECMA-130), vectorised across many sectors at once


def _make_tables():
    ecc_f = np.zeros(256, dtype=np.uint8)
    ecc_b = np.zeros(256, dtype=np.uint8)
    edc = np.zeros(256, dtype=np.uint32)
    for i in range(256):
        j = ((i << 1) ^ (0x11D if i & 0x80 else 0)) & 0xFF
        ecc_f[i] = j
        ecc_b[i ^ j] = i
        e = i
        for _ in range(8):
            e = (e >> 1) ^ (0xD8018001 if e & 1 else 0)
        edc[i] = e
    return ecc_f, ecc_b, edc


_ECC_F, _ECC_B, _EDC_LUT = _make_tables()


def _ecc_index(major_count: int, minor_count: int, major_mult: int, minor_inc: int) -> np.ndarray:
    size = major_count * minor_count
    idx = np.empty((minor_count, major_count), dtype=np.intp)
    for major in range(major_count):
        index = (major >> 1) * major_mult + (major & 1)
        for minor in range(minor_count):
            idx[minor, major] = index
            index += minor_inc
            if index >= size:
                index -= size
    return idx


_P_IDX = _ecc_index(86, 24, 2, 86)
_Q_IDX = _ecc_index(52, 43, 86, 88)


def _ecc_block(src: np.ndarray, idx: np.ndarray, major_count: int) -> np.ndarray:
    n = src.shape[0]
    a = np.zeros((n, major_count), dtype=np.uint8)
    b = np.zeros((n, major_count), dtype=np.uint8)
    for minor in range(idx.shape[0]):
        t = src[:, idx[minor]]
        a ^= t
        b ^= t
        a = _ECC_F[a]
    a = _ECC_B[_ECC_F[a] ^ b]
    return np.concatenate([a, a ^ b], axis=1)


def edc(data: np.ndarray) -> np.ndarray:
    """CD-ROM EDC (CRC32, poly 0xD8018001 reflected) of each row of a uint8 matrix."""
    crc = np.zeros(data.shape[0], dtype=np.uint32)
    for col in range(data.shape[1]):
        crc = (crc >> np.uint32(8)) ^ _EDC_LUT[(crc ^ data[:, col]) & np.uint32(0xFF)]
    return crc


def encode_mode2_form1(user: bytes, subheader: bytes = DATA_SUBHEADER) -> bytes:
    """2048-byte user sectors -> 2336-byte Mode 2 Form 1 sectors with valid EDC/ECC."""
    n = len(user) // USER_SIZE
    if n * USER_SIZE != len(user):
        raise ValueError("user data is not a whole number of sectors")
    if n == 0:
        return b""
    raw = np.zeros((n, RAW_SIZE), dtype=np.uint8)
    raw[:, 16:24] = np.frombuffer(subheader, dtype=np.uint8)
    raw[:, 24:2072] = np.frombuffer(user, dtype=np.uint8).reshape(n, USER_SIZE)
    raw[:, 2072:2076] = edc(raw[:, 16:2072]).astype("<u4").view(np.uint8).reshape(n, 4)
    # Mode 2 ECC is computed with the 4 header bytes (12..15) zeroed; they are zero here.
    raw[:, 2076:2248] = _ecc_block(raw[:, 12:12 + 2064], _P_IDX, 86)
    raw[:, 2248:2352] = _ecc_block(raw[:, 12:12 + 2236], _Q_IDX, 52)
    return raw[:, 16:].tobytes()


def mode2_form2_gap_sector() -> bytes:
    """A zero-filled XA Form 2 sector (2336 bytes), as used in pregaps and run-out."""
    raw = np.zeros((1, RAW_SIZE), dtype=np.uint8)
    raw[0, 16:24] = np.frombuffer(FORM2_SUBHEADER, dtype=np.uint8)
    raw[0, 2348:2352] = edc(raw[:, 16:2348]).astype("<u4").view(np.uint8)
    return raw[0, 16:].tobytes()
