"""Minimal ISO 9660 reader working on absolute LBAs.

Dreamcast selfboot filesystems are built with `mkisofs -C 0,<lba>`, so every
extent stored in the filesystem is an absolute disc LBA.  The reader takes a
`read_sector(lba) -> 2048 bytes` callback so it works on any disc model.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Callable, Iterator

from .util import DcError

ReadSector = Callable[[int], bytes]


class IsoError(DcError):
    pass


@dataclass
class Entry:
    name: str        # without ";1" version suffix
    extent: int      # absolute LBA
    size: int
    is_dir: bool
    path: str = ""


def _parse_dir_records(data: bytes, joliet: bool = False) -> Iterator[tuple[str, int, int, int]]:
    pos = 0
    while pos < len(data):
        rlen = data[pos]
        if rlen == 0:
            # records never span sectors: skip to the next sector boundary
            pos = (pos // 2048 + 1) * 2048
            continue
        if rlen < 34 or pos + rlen > len(data):
            break
        rec = data[pos:pos + rlen]
        extent = struct.unpack_from("<I", rec, 2)[0]
        size = struct.unpack_from("<I", rec, 10)[0]
        flags = rec[25]
        nlen = rec[32]
        raw = rec[33:33 + nlen]
        if raw == b"\x00":
            name = "."
        elif raw == b"\x01":
            name = ".."
        else:
            name = raw.decode("utf-16-be", "replace") if joliet else raw.decode("latin-1")
            if ";" in name:
                name = name.split(";", 1)[0]
            if name.endswith(".") and "." not in name[:-1]:
                name = name[:-1]
        yield name, extent, size, flags
        pos += rlen


class IsoFS:
    def __init__(self, read_sector: ReadSector, base_lba: int, joliet: bool = False,
                 read_range: Callable[[int, int], bytes] | None = None):
        """`joliet=True` uses the Joliet tree (real long/mixed-case names) when present.
        `read_range(lba, n)` (optional) reads many sectors at once, for speed."""
        self.read_sector = read_sector
        self.read_range = read_range
        self.base = base_lba
        pvd = read_sector(base_lba + 16)
        if pvd[0:6] != b"\x01CD001":
            raise IsoError(f"no ISO 9660 primary volume descriptor at LBA {base_lba + 16}")
        self.volume_id = pvd[40:72].decode("latin-1").strip()
        self.volume_space = struct.unpack_from("<I", pvd, 80)[0]
        self.joliet = False
        desc = pvd
        if joliet:
            for i in range(17, 32):
                d = read_sector(base_lba + i)
                if d[1:6] != b"CD001" or d[0] == 255:
                    break
                if d[0] == 2 and d[88:91] in (b"%/@", b"%/C", b"%/E"):
                    desc, self.joliet = d, True
                    break
        root = desc[156:156 + 34]
        self.root = Entry("", struct.unpack_from("<I", root, 2)[0],
                          struct.unpack_from("<I", root, 10)[0], True, "/")

    def read_extent(self, lba: int, size: int, limit: int = 64 << 20) -> bytes:
        size = min(size, limit)
        n = (size + 2047) // 2048
        if self.read_range is not None:
            return self.read_range(lba, n)[:size]
        return b"".join(self.read_sector(lba + i) for i in range(n))[:size]

    def listdir(self, d: Entry) -> list[Entry]:
        data = self.read_extent(d.extent, d.size, limit=4 << 20)
        out = []
        for name, extent, size, flags in _parse_dir_records(data, self.joliet):
            if name in (".", ".."):
                continue
            path = (d.path.rstrip("/") + "/" + name)
            out.append(Entry(name, extent, size, bool(flags & 2), path))
        return out

    def find(self, name: str, d: Entry | None = None) -> Entry | None:
        for e in self.listdir(d or self.root):
            if e.name.upper() == name.upper():
                return e
        return None

    def walk(self, d: Entry | None = None, depth: int = 0) -> Iterator[Entry]:
        if depth > 32:
            return
        for e in self.listdir(d or self.root):
            yield e
            if e.is_dir:
                yield from self.walk(e, depth + 1)

    def read_file(self, e: Entry, limit: int = 64 << 20) -> bytes:
        return self.read_extent(e.extent, e.size, limit)


def root_is_consistent(read_sector: ReadSector, base_lba: int) -> bool:
    """True if the PVD at base+16 has a root directory whose '.' entry points to itself."""
    try:
        pvd = read_sector(base_lba + 16)
        if pvd[0:6] != b"\x01CD001":
            return False
        root_lba = struct.unpack_from("<I", pvd, 156 + 2)[0]
        rec = read_sector(root_lba)
        return rec[0] >= 34 and rec[33] == 0 and struct.unpack_from("<I", rec, 2)[0] == root_lba
    except (IndexError, IOError, struct.error):
        return False


def infer_base(read_track_sector: Callable[[int], bytes], track_sectors: int) -> int | None:
    """Find the LBA a filesystem was built for, given sectors relative to its track.

    The root directory's '.' record stores its own absolute LBA; the sector index
    where that directory actually sits gives base = root_lba - index.
    """
    try:
        pvd = read_track_sector(16)
    except (IndexError, IOError):
        return None
    if pvd[0:6] != b"\x01CD001":
        return None
    root_lba = struct.unpack_from("<I", pvd, 156 + 2)[0]
    for idx in range(17, min(track_sectors, 4096)):
        rec = read_track_sector(idx)
        if rec[0] >= 34 and rec[33] == 0 and rec[32] == 1 and struct.unpack_from("<I", rec, 2)[0] == root_lba:
            base = root_lba - idx
            if base >= 0:
                return base
    return None
