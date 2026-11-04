"""DiscJuggler (.cdi) images: reader and writer.

Reader: follows the parsing used by cdirip and Flycast (versions 2, 3 and 3.5,
including the DiscJuggler 3.00.780+ and DJ4 extra fields).

Writer: produces version 3.5 images with the same header structure as
SiZiOUS' cdi4dc (which is accepted by DiscJuggler, emulators and ODEs),
generalised to any number of sessions/tracks.  Data tracks are written as
2336-byte Mode 2 Form 1 sectors with valid EDC/ECC, audio as 2352-byte PCM.

LBA convention: the header's "start LBA" of a track is the frame address of
the track's pregap start, so the first user sector (index 1) is at
    lba = start_lba + pregap - 150
which is what Flycast uses (StartFAD = start_lba + pregap).
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field

from . import sectors as S
from .util import DcError

CDI_V2 = 0x80000004
CDI_V3 = 0x80000005
CDI_V35 = 0x80000006
VERSIONS = {CDI_V2: "2", CDI_V3: "3", CDI_V35: "3.5"}

TRACK_START_MARK = bytes([0, 0, 0x01, 0, 0, 0, 0xFF, 0xFF, 0xFF, 0xFF])
SECTOR_SIZES = {0: 2048, 1: 2336, 2: 2352, 4: 2448}
MODES = {0: S.AUDIO, 1: S.MODE1, 2: S.MODE2}


class CdiError(DcError):
    pass


@dataclass
class CdiTrack:
    session: int
    number: int          # 1-based over the whole disc
    mode: str
    sector_size: int
    pregap: int
    length: int
    start_lba: int
    total_length: int
    position: int = 0    # file offset of the track's first stored (pregap) sector

    @property
    def lba(self) -> int:
        return self.start_lba + self.pregap - 150

    @property
    def data_offset(self) -> int:
        return self.position + self.pregap * self.sector_size

    def source(self, path: str) -> S.FileSectorSource:
        return S.FileSectorSource(path, self.data_offset, self.length, self.sector_size, self.mode)


@dataclass
class CdiImage:
    path: str
    version: int
    sessions: list[list[CdiTrack]] = field(default_factory=list)

    @property
    def version_name(self) -> str:
        return VERSIONS.get(self.version, hex(self.version))


def _u16(f) -> int:
    b = f.read(2)
    if len(b) != 2:
        raise CdiError("unexpected end of CDI header")
    return struct.unpack("<H", b)[0]


def _u32(f) -> int:
    b = f.read(4)
    if len(b) != 4:
        raise CdiError("unexpected end of CDI header")
    return struct.unpack("<I", b)[0]


def _read_track(f, version: int, session: int, number: int) -> CdiTrack:
    if _u32(f) != 0:
        f.seek(8, 1)  # extra data (DJ 3.00.780 and up)
    for _ in range(2):
        if f.read(10) != TRACK_START_MARK:
            raise CdiError("unsupported CDI: track start mark not found")
    f.seek(4, 1)
    (fnlen,) = f.read(1)
    f.seek(fnlen + 11 + 4 + 4, 1)
    if _u32(f) == 0x80000000:
        f.seek(8, 1)  # DJ4
    f.seek(2, 1)
    pregap = _u32(f)
    length = _u32(f)
    f.seek(6, 1)
    mode = _u32(f)
    f.seek(12, 1)
    start_lba = _u32(f)
    total_length = _u32(f)
    f.seek(16, 1)
    ssv = _u32(f)
    if ssv not in SECTOR_SIZES:
        raise CdiError(f"unsupported CDI sector size value {ssv}")
    if mode not in MODES:
        raise CdiError(f"unsupported CDI track mode {mode}")
    f.seek(29, 1)
    if version != CDI_V2:
        f.seek(5, 1)
        if _u32(f) == 0xFFFFFFFF:
            f.seek(78, 1)  # extra data (DJ 3.00.780 and up)
    return CdiTrack(session, number, MODES[mode], SECTOR_SIZES[ssv], pregap, length,
                    start_lba, total_length)


def read_cdi(path: str | os.PathLike) -> CdiImage:
    path = os.fspath(path)
    size = os.path.getsize(path)
    if size < 8:
        raise CdiError("file too small to be a CDI image")
    with open(path, "rb") as f:
        f.seek(size - 8)
        version, hoff = struct.unpack("<II", f.read(8))
        if version not in VERSIONS or hoff == 0:
            raise CdiError("not a DiscJuggler CDI image (bad version marker)")
        hpos = size - hoff if version == CDI_V35 else hoff
        if not 0 < hpos < size:
            raise CdiError("CDI header offset is out of range")
        f.seek(hpos)
        nsess = _u16(f)
        if not 0 < nsess < 100:
            raise CdiError(f"implausible CDI session count {nsess}")
        img = CdiImage(path, version)
        position, number = 0, 0
        for s in range(nsess):
            ntr = _u16(f)
            if ntr > 99:
                raise CdiError(f"implausible CDI track count {ntr}")
            tracks = []
            for _ in range(ntr):
                number += 1
                t = _read_track(f, version, s, number)
                if t.total_length < t.pregap + t.length:
                    raise CdiError(f"track {number} is truncated in the image")
                t.position = position
                position += t.total_length * t.sector_size
                if position > hpos:
                    raise CdiError(f"track {number} extends past the end of the image data")
                tracks.append(t)
            f.seek(4 + 8 + (0 if version == CDI_V2 else 1), 1)
            if tracks:  # a session without tracks is an open session: ignore
                img.sessions.append(tracks)
    if not img.sessions:
        raise CdiError("CDI image contains no tracks")
    return img


# --------------------------------------------------------------------------
# Writer


@dataclass
class WriterTrack:
    mode: str
    lba: int                   # LBA of the first user sector
    source: S.SectorSource     # user data (2048 or 2352 bytes per sector)
    pregap: int = 150
    runout: int = 0            # TAO run-out blocks appended (XA Form 2 / silence), part of the length

    @property
    def length(self) -> int:
        return self.source.count + self.runout


def _set(buf: bytearray, off: int, value: int, size: int = 4) -> None:
    buf[off:off + size] = value.to_bytes(size, "little")


def _track_header_start(filename: bytes) -> bytes:
    # 2 x start mark, 2 unknown words (constant in cdi4dc), filename, then the
    # 31-byte block carrying the DJ4 marker and the 80 min capacity (360000).
    nxt = bytearray(31)
    nxt[0x0B] = 0x02
    nxt[0x13:0x17] = (0x80000000).to_bytes(4, "little")
    nxt[0x17:0x1B] = (360000).to_bytes(4, "little")
    nxt[0x1D] = 0x98
    return (TRACK_START_MARK * 2 + struct.pack("<HH", 0x00AB, 0x0210)
            + bytes([len(filename)]) + filename + bytes(nxt))


def _track_block(t: WriterTrack, length: int, session: int, index_in_session: int) -> bytes:
    b = bytearray(176)
    b[0] = 0x02
    total = t.pregap + length
    mode_val = {S.AUDIO: 0, S.MODE1: 1, S.MODE2: 2}[t.mode]
    ssv = 2 if t.mode == S.AUDIO else (1 if t.mode == S.MODE2 else 0)
    _set(b, 2, t.pregap)
    _set(b, 6, length)
    _set(b, 16, mode_val)
    _set(b, 20, index_in_session)
    _set(b, 24, session)
    _set(b, 32, t.lba + 150 - t.pregap)   # start LBA (see module docstring)
    _set(b, 36, total)
    _set(b, 56, ssv)
    if t.mode != S.AUDIO:
        b[60] = 0x04
    _set(b, 65, total)
    b[90:98] = b"\xff" * 8
    b[98] = 0x01
    b[102] = 0x80
    b[106] = 0x02
    b[110] = 0x10
    _set(b, 114, 44100, 2)
    b[160:164] = b"\xff" * 4
    return bytes(b)


def _session_trailer(tracks: list[WriterTrack], last: bool) -> bytes:
    b = bytearray(13)
    if any(t.mode == S.MODE2 for t in tracks):
        b[0] = 0x02
    elif any(t.mode == S.MODE1 for t in tracks):
        b[0] = 0x01
    if last:
        _set(b, 8, tracks[0].lba)
    return bytes(b)


def write_cdi(path: str | os.PathLike, sessions: list[list[WriterTrack]],
              volume_name: str = "DREAMCAST", progress=None) -> None:
    """Write a CDI.  `progress(done_sectors, total_sectors)` is called while writing."""
    path = os.fspath(path)
    filename = os.path.basename(path).encode("utf-8", "replace")[:255]
    total = sum(t.source.count for s in sessions for t in s)
    gap2336 = S.mode2_form2_gap_sector()
    done = 0
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        for s in sessions:
            for t in s:
                if t.source.sector_bytes != S.user_bytes(t.mode):
                    raise ValueError("track source sector size does not match its mode")
                if t.mode == S.AUDIO:
                    f.write(bytes(S.RAW_SIZE * t.pregap))
                elif t.mode == S.MODE2:
                    f.write(gap2336 * t.pregap)
                else:
                    f.write(bytes(S.USER_SIZE * t.pregap))
                for chunk in t.source.iter_chunks(4096):
                    f.write(S.encode_mode2_form1(chunk) if t.mode == S.MODE2 else chunk)
                    done += len(chunk) // t.source.sector_bytes
                    if progress:
                        progress(done, total)
                if t.runout:
                    if t.mode == S.MODE2:
                        f.write(gap2336 * t.runout)
                    else:
                        f.write(bytes(t.source.sector_bytes * t.runout))
        out = bytearray(struct.pack("<H", len(sessions)))
        for si, s in enumerate(sessions):
            out += struct.pack("<H", len(s))
            for ti, t in enumerate(s):
                out += struct.pack("<I", 0) + _track_header_start(filename)
                out += _track_block(t, t.length, si, ti)
            out += _session_trailer(s, si == len(sessions) - 1)
        out += struct.pack("<HI", 0, 0)
        out += _track_header_start(filename)
        last = sessions[-1][-1]
        out += struct.pack("<I", last.lba + last.length)
        vol = volume_name.encode("ascii", "replace")[:32]
        out += bytes([len(vol)]) + vol
        end = bytearray(42)
        end[1] = 0x01
        end[5] = 0x01
        _set(end, 38, CDI_V35)
        out += end
        out += struct.pack("<I", len(out) + 4)
        f.write(out)
    os.replace(tmp, path)
