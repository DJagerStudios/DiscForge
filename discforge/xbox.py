"""Original Xbox discs (XDVDFS).

* Reading the XDVDFS tree of an XISO, or of the game partition inside a full Redump image.
* Writing a fresh, trimmed XISO from that tree (what extract-xiso -r does): only the game's
  files, packed in their original order, so most games fit on a single-layer DVD.
* Writing an XISO from a folder (homebrew / extracted games with a default.xbe).

Layout: zeros up to 0x10000, the volume descriptor (magic, root table sector and size, file
time, magic again at +0x7EC), then directory tables, then file data, each starting on a
2048-byte sector.  Directory tables are binary search trees: every entry is
left/right (u16, offset/4 in the table, 0 = none), start sector, size, attributes, name length,
name, padded to 4 bytes; an entry never crosses a sector boundary and unused bytes are 0xFF.
The tree is searched case-insensitively, so entries are ordered by upper-cased name.
"""
from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field
from typing import Callable

from .formats import FormatError, Reader, _check

MAGIC = b"MICROSOFT*XBOX*MEDIA"
HEADER_OFF = 0x10000
SECTOR = 2048
REDUMP_GAME_OFF = 0x18300000          # XGD1 game partition inside a full Redump image
XGD2_GAME_OFF = 0xFD90000             # Xbox 360
XGD3_GAME_OFF = 0x2080000             # Xbox 360
ATTR_DIR = 0x10
ATTR_ARCHIVE = 0x20


@dataclass
class Entry:
    name: bytes
    is_dir: bool
    sector: int
    size: int
    attr: int
    children: list["Entry"] = field(default_factory=list)
    src: str | None = None                # file path, when building from a folder

    @property
    def text(self) -> str:
        return self.name.decode("latin-1")


def partition_offset(reader: Reader) -> int | None:
    """Byte offset of the XDVDFS volume: 0 for an XISO, 0x18300000 for a Redump image."""
    for off in (0, REDUMP_GAME_OFF):
        if off + HEADER_OFF + 20 <= reader.size and reader.read(off + HEADER_OFF, 20) == MAGIC:
            return off
    return None


def is_xbox360(reader: Reader) -> bool:
    for off in (XGD2_GAME_OFF, XGD3_GAME_OFF):
        if off + HEADER_OFF + 20 <= reader.size and reader.read(off + HEADER_OFF, 20) == MAGIC:
            return True
    return False


class XdvdfsReader:
    def __init__(self, reader: Reader, base: int):
        self.r = reader
        self.base = base
        hdr = reader.read(base + HEADER_OFF, SECTOR)
        if hdr[:20] != MAGIC:
            raise FormatError("No Xbox file system found")
        self.root_sector, self.root_size = struct.unpack_from("<II", hdr, 0x14)
        self.filetime = struct.unpack_from("<Q", hdr, 0x1C)[0]

    def list_dir(self, sector: int, size: int) -> list[Entry]:
        if size == 0:
            return []
        if size > 64 << 20:
            raise FormatError("This Xbox image has a damaged directory")
        table = self.r.read(self.base + sector * SECTOR, size)
        out, stack, seen = [], [0], set()
        while stack:
            off = stack.pop()
            if off in seen or off + 14 > len(table):
                continue
            seen.add(off)
            left, right, start, fsize, attr, nlen = struct.unpack_from("<HHIIBB", table, off)
            if left == 0xFFFF and right == 0xFFFF:
                continue                       # padding / empty directory
            name = table[off + 14:off + 14 + nlen]
            if not name:
                continue
            out.append(Entry(name, bool(attr & ATTR_DIR), start, fsize, attr))
            if left:
                stack.append(left * 4)
            if right:
                stack.append(right * 4)
        return out

    def tree(self) -> Entry:
        root = Entry(b"", True, self.root_sector, self.root_size, ATTR_DIR)
        todo, guard = [root], 0
        while todo:
            d = todo.pop()
            guard += 1
            if guard > 200000:
                raise FormatError("This Xbox image's directory tree is damaged")
            d.children = self.list_dir(d.sector, d.size)
            todo += [c for c in d.children if c.is_dir]
        return root

    def find(self, path: str) -> Entry | None:
        node = self.tree() if not hasattr(self, "_tree") else self._tree
        self._tree = node
        for part in path.strip("/\\").replace("\\", "/").split("/"):
            node = next((c for c in node.children if c.text.upper() == part.upper()), None)
            if node is None:
                return None
        return node

    def read_file(self, e: Entry, limit: int | None = None) -> bytes:
        n = e.size if limit is None else min(e.size, limit)
        return self.r.read(self.base + e.sector * SECTOR, n)


def walk(e: Entry):
    for c in e.children:
        yield c
        if c.is_dir:
            yield from walk(c)


def data_size(root: Entry) -> int:
    return sum(c.size for c in walk(root) if not c.is_dir)


# --------------------------------------------------------------------------
# Title from default.xbe

def xbe_info(data: bytes) -> dict:
    """{'title': str, 'title_id': 'XXXX-NNN'} from an XBE header (first ~64 KiB)."""
    if data[:4] != b"XBEH" or len(data) < 0x180:
        return {}
    base, cert = struct.unpack_from("<I", data, 0x104)[0], struct.unpack_from("<I", data, 0x118)[0]
    off = cert - base
    if not 0 < off < len(data) - 0x60:
        return {}
    tid = struct.unpack_from("<I", data, off + 8)[0]
    title = data[off + 0x0C:off + 0x0C + 80].decode("utf-16-le", "replace").split("\0")[0].strip()
    pub = bytes([(tid >> 24) & 0xFF, (tid >> 16) & 0xFF])
    tid_s = (f"{pub.decode('latin-1')}-{tid & 0xFFFF:03d}" if all(65 <= b <= 90 for b in pub)
             else f"{tid:08X}")
    return {"title": title, "title_id": tid_s}


# --------------------------------------------------------------------------
# Writing

def _entry_len(name: bytes) -> int:
    return (14 + len(name) + 3) & ~3


def _key(e: Entry) -> bytes:
    return e.name.upper()


def build_table(children: list[Entry]) -> bytes:
    """A directory table: balanced BST in pre-order, entries never crossing a sector."""
    if not children:
        return b"\xff" * SECTOR
    items = sorted(children, key=_key)
    for a, b in zip(items, items[1:]):
        if _key(a) == _key(b):
            raise FormatError(f"Two files differ only in upper/lower case: {a.text} / {b.text}")
    order: list[tuple[Entry, int | None, int | None]] = []   # (entry, left idx, right idx) into order

    def place(lo: int, hi: int) -> int | None:
        if lo >= hi:
            return None
        mid = (lo + hi) // 2
        idx = len(order)
        order.append((items[mid], None, None))
        left = place(lo, mid)
        right = place(mid + 1, hi)
        order[idx] = (items[mid], left, right)
        return idx

    place(0, len(items))
    offs, pos = [], 0
    for e, _l, _r in order:
        n = _entry_len(e.name)
        if pos // SECTOR != (pos + n - 1) // SECTOR:
            pos = (pos // SECTOR + 1) * SECTOR
        offs.append(pos)
        pos += n
    size = -(-pos // SECTOR) * SECTOR
    table = bytearray(b"\xff" * size)
    for (e, left, right), off in zip(order, offs):
        lo = offs[left] // 4 if left is not None else 0
        ro = offs[right] // 4 if right is not None else 0
        rec = struct.pack("<HHIIBB", lo, ro, e.sector, e.size, e.attr, len(e.name)) + e.name
        rec += b"\xff" * (_entry_len(e.name) - len(rec))
        table[off:off + len(rec)] = rec
    return bytes(table)


def _table_size(children: list[Entry]) -> int:
    """Size of the table build_table() would produce (needs only name lengths)."""
    if not children:
        return SECTOR
    pos = 0
    items = sorted(children, key=_key)
    order: list[Entry] = []

    def place(lo, hi):
        if lo >= hi:
            return
        mid = (lo + hi) // 2
        order.append(items[mid])
        place(lo, mid)
        place(mid + 1, hi)

    place(0, len(items))
    for e in order:
        n = _entry_len(e.name)
        if pos // SECTOR != (pos + n - 1) // SECTOR:
            pos = (pos // SECTOR + 1) * SECTOR
        pos += n
    return -(-pos // SECTOR) * SECTOR


def layout(root: Entry) -> tuple[int, list[Entry], list[Entry]]:
    """Assign new sectors: tables first (breadth first), then files in their original order.
    Returns (total sectors, directories, files)."""
    dirs, files = [root], []
    i = 0
    while i < len(dirs):
        for c in dirs[i].children:
            (dirs if c.is_dir else files).append(c)
        i += 1
    sector = HEADER_OFF // SECTOR + 1
    for d in dirs:
        d.size = _table_size(d.children)
        d.sector = sector
        sector += d.size // SECTOR
    files.sort(key=lambda f: (f.sector, f.text.upper()))
    for f in files:
        if f.size == 0:
            f.sector = 0
            continue
        f._src_sector = f.sector          # type: ignore[attr-defined]
        f.sector = sector
        sector += -(-f.size // SECTOR)
    total = -(-sector // 32) * 32          # end on a 64 KiB boundary like extract-xiso
    return total, dirs, files


def write_xiso(root: Entry, out: str, read_src: Callable[[Entry, int, int], bytes],
               progress: Callable[[float], None] | None = None, cancel=None, filetime: int | None = None,
               media_patch: bool = True) -> tuple[int, int]:
    """Write a new XISO for `root` (whose files keep their source sector in f.sector).
    read_src(entry, offset, n) returns file data from the source.  With media_patch, every XBE gets
    extract-xiso's media patch so it runs from burned discs.  Returns (image size, XBEs patched)."""
    from .quirks import xbe_media_patch
    patched = 0
    total, dirs, files = layout(root)
    if filetime is None:
        filetime = int((time.time() + 11644473600) * 10_000_000)
    vd = bytearray(SECTOR)
    vd[0:20] = MAGIC
    struct.pack_into("<IIQ", vd, 0x14, root.sector, root.size, filetime)
    vd[0x7EC:0x7EC + 20] = MAGIC
    todo = sum(f.size for f in files) or 1
    done = 0
    with open(out, "wb") as fo:
        fo.truncate(total * SECTOR)
        fo.seek(HEADER_OFF)
        fo.write(vd)
        for d in dirs:
            fo.seek(d.sector * SECTOR)
            fo.write(build_table(d.children))
        for f in files:
            if not f.size:
                continue
            fo.seek(f.sector * SECTOR)
            if media_patch and f.name.upper().endswith(b".XBE") and f.size <= 256 << 20:
                _check(cancel)
                data = bytearray(read_src(f, 0, f.size))
                if len(data) != f.size:
                    raise FormatError(f"Couldn't read {f.text} from the source image (it may be incomplete)")
                patched += xbe_media_patch(data) > 0
                fo.write(data)
                done += f.size
                if progress:
                    progress(done / todo)
                continue
            pos = 0
            while pos < f.size:
                _check(cancel)
                n = min(8 << 20, f.size - pos)
                data = read_src(f, pos, n)
                if len(data) != n:
                    raise FormatError(f"Couldn't read {f.text} from the source image (it may be incomplete)")
                fo.write(data)
                pos += n
                done += n
                if progress:
                    progress(done / todo)
    return total * SECTOR, patched


def xbe_needs_patch(reader: Reader) -> bool:
    """True when the image's default.xbe hasn't had the media patch yet."""
    from .quirks import XBE_MEDIA_PATTERN
    base = partition_offset(reader)
    if base is None:
        return False
    x = XdvdfsReader(reader, base)
    e = x.find("default.xbe")
    return e is not None and e.size <= 256 << 20 and XBE_MEDIA_PATTERN in x.read_file(e)


def rewrite(reader: Reader, out: str, progress=None, cancel=None) -> tuple[int, int]:
    """Redump image or XISO -> trimmed, media-patched XISO; returns (size, XBEs patched)."""
    base = partition_offset(reader)
    if base is None:
        raise FormatError("No Xbox file system found in this image")
    x = XdvdfsReader(reader, base)
    root = x.tree()

    def read_src(f: Entry, pos: int, n: int) -> bytes:
        return reader.read(base + f._src_sector * SECTOR + pos, n)   # type: ignore[attr-defined]

    return write_xiso(root, out, read_src, progress, cancel, x.filetime)


def rewritten_size(reader: Reader) -> int:
    base = partition_offset(reader)
    if base is None:
        raise FormatError("No Xbox file system found in this image")
    root = XdvdfsReader(reader, base).tree()
    return layout(root)[0] * SECTOR


def folder_tree(path: str) -> Entry:
    def scan(p: str) -> list[Entry]:
        out = []
        for de in sorted(os.scandir(p), key=lambda d: d.name.upper()):
            try:
                name = de.name.encode("ascii")
            except UnicodeEncodeError:
                raise FormatError(f"Xbox discs can't hold the file name '{de.name}' (use plain letters)") from None
            if len(name) > 255:
                raise FormatError(f"File name too long for an Xbox disc: {de.name}")
            if de.is_dir(follow_symlinks=True):
                e = Entry(name, True, 0, 0, ATTR_DIR)
                e.children = scan(de.path)
            else:
                size = de.stat().st_size
                if size >= 1 << 32:
                    raise FormatError(f"{de.name} is larger than 4 GB, which Xbox discs can't store")
                e = Entry(name, False, 0, size, ATTR_ARCHIVE, src=de.path)
            out.append(e)
        return out

    return Entry(b"", True, 0, 0, ATTR_DIR, scan(path))


def folder_to_xiso(path: str, out: str, progress=None, cancel=None) -> tuple[int, int]:
    root = folder_tree(path)
    handles: dict[str, object] = {}

    def read_src(f: Entry, pos: int, n: int) -> bytes:
        fh = handles.get(f.src)
        if fh is None:
            for h in handles.values():
                h.close()                       # type: ignore[attr-defined]
            handles.clear()
            fh = handles[f.src] = open(f.src, "rb")   # type: ignore[index]
        fh.seek(pos)                            # type: ignore[attr-defined]
        return fh.read(n)                       # type: ignore[attr-defined]

    try:
        return write_xiso(root, out, read_src, progress, cancel)
    finally:
        for h in handles.values():
            h.close()                           # type: ignore[attr-defined]
