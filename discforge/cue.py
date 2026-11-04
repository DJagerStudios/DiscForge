"""CUE sheets (any number of FILEs, Redump/EAC/single-bin styles) -> exact cdrdao TOC files.

cdrdao's own CUE support only takes a single FILE, so every CUE is parsed here and written as a
native TOC that describes each track exactly: raw 2352-byte data tracks (MODE1_RAW / MODE2_RAW,
written untouched by the raw driver), audio with its byte order, INDEX 00 pregaps stored in the
same file or at the end of the previous file (EAC "gaps appended"), PREGAP/POSTGAP gaps that
aren't stored at all, and further indexes.

Track 1 follows what cdrdao really does: its 2-second pregap is written by cdrdao as part of the
lead-in, so a CUE's PREGAP of up to 2 s on track 1 is dropped, and a stored INDEX 00 on a data
track 1 is skipped (the data starts at LBA 0); an audio track 1 keeps it (a hidden track).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .util import DcError

FRAMES_PER_SEC = 75
SECTOR_SIZES = {"AUDIO": 2352, "MODE1/2352": 2352, "MODE2/2352": 2352, "MODE1/2048": 2048,
                "MODE2/2336": 2336, "CDI/2352": 2352, "CDI/2336": 2336, "CDG": 2448}
TOC_MODES = {"AUDIO": "AUDIO", "MODE1/2352": "MODE1_RAW", "MODE2/2352": "MODE2_RAW", "MODE1/2048": "MODE1",
             "MODE2/2336": "MODE2", "CDI/2352": "MODE2_RAW", "CDI/2336": "MODE2"}
LEADIN_PREGAP = 150


class CueError(DcError):
    pass


def msf_to_frames(msf: str) -> int:
    m = re.fullmatch(r"(\d+):(\d{1,2}):(\d{1,2})", msf.strip())
    if not m:
        raise CueError(f"bad time '{msf}' in the CUE sheet")
    mm, ss, ff = (int(x) for x in m.groups())
    if ss >= 60 or ff >= 75:
        raise CueError(f"bad time '{msf}' in the CUE sheet")
    return (mm * 60 + ss) * FRAMES_PER_SEC + ff


def frames_to_msf(frames: int) -> str:
    return f"{frames // (60 * 75):02d}:{frames // 75 % 60:02d}:{frames % 75:02d}"


@dataclass
class CueTrack:
    number: int
    mode: str                       # AUDIO, MODE1/2352, MODE2/2352, ...
    file: str                       # absolute path of the file holding INDEX 01
    file_type: str                  # BINARY, MOTOROLA, WAVE
    indexes: dict[int, int] = field(default_factory=dict)   # index -> frame in `file`
    pregap: int = 0                 # PREGAP frames (not stored anywhere)
    postgap: int = 0
    flags: set[str] = field(default_factory=set)
    length: int = 0                 # frames in `file` from `start` to the end of the track
    pre_file: str | None = None     # INDEX 00 stored at the end of the previous file (EAC)
    pre_type: str = "BINARY"
    pre_start: int = 0
    pre_length: int = 0

    @property
    def is_audio(self) -> bool:
        return self.mode == "AUDIO"

    @property
    def sector_size(self) -> int:
        return SECTOR_SIZES[self.mode]

    @property
    def start(self) -> int:
        """Frame in `file` where this track's part of the file begins (INDEX 00 if it's here)."""
        return min(self.indexes.values())

    @property
    def file_pregap(self) -> int:
        """Pregap stored in `file` itself (INDEX 00 -> 01)."""
        return self.indexes[1] - self.indexes[0] if 0 in self.indexes else 0

    @property
    def body(self) -> int:
        """Frames from INDEX 01 to the end of the track."""
        return self.length - self.file_pregap


@dataclass
class CueSheet:
    path: str
    tracks: list[CueTrack]
    catalog: str = ""
    sessions: int = 1
    gd_rom: bool = False            # Redump Dreamcast CUE (REM HIGH-DENSITY AREA)

    @property
    def has_audio(self) -> bool:
        return any(t.is_audio for t in self.tracks)

    @property
    def data_tracks(self) -> list[CueTrack]:
        return [t for t in self.tracks if not t.is_audio]

    @property
    def total_frames(self) -> int:
        """Sectors of the program area as burned (track 1's lead-in pregap excluded)."""
        return plan(self)[1]

    @property
    def files(self) -> list[str]:
        out: list[str] = []
        for t in self.tracks:
            for f in (t.pre_file, t.file):
                if f and f not in out:
                    out.append(f)
        return out

    def read_user(self, track: CueTrack, sector: int, count: int = 1) -> bytes:
        """User data (2048 bytes/sector) of a data track, `sector` counted from its INDEX 01."""
        first = track.indexes[1] + sector
        ss = track.sector_size
        off = {2352: 24 if "MODE2" in track.mode or track.mode.startswith("CDI") else 16,
               2048: 0, 2336: 8}[ss]
        out = bytearray()
        with open(track.file, "rb") as f:
            f.seek(first * ss)
            raw = f.read(count * ss)
        for i in range(len(raw) // ss):
            out += raw[i * ss + off:i * ss + off + 2048]
        return bytes(out)


def _inside(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(folder)]) == os.path.realpath(folder)
    except ValueError:
        return False


def _resolve(cue_dir: str, name: str) -> str:
    """The file a CUE names, looked for next to the CUE only (never elsewhere on the computer)."""
    cand = name.replace("\\", "/")
    p = os.path.join(cue_dir, cand)
    if os.path.isfile(p) and _inside(p, cue_dir):
        return p
    base = os.path.basename(cand)
    try:  # rips made on Windows often differ in case, or carry the ripper's absolute path
        for e in os.listdir(cue_dir):
            if e.lower() == base.lower() and os.path.isfile(os.path.join(cue_dir, e)):
                return os.path.join(cue_dir, e)
    except OSError:
        pass
    raise CueError(f"The CUE sheet points at a file that's missing: {base}")


def wav_data(path: str) -> tuple[int, int]:
    """(byte offset, byte length) of a WAVE file's audio, which must be CD audio (16-bit stereo 44.1 kHz)."""
    with open(path, "rb") as f:
        hdr = f.read(12)
        if hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
            raise CueError(f"{os.path.basename(path)} isn't a WAVE file")
        fmt_ok = False
        while True:
            ck = f.read(8)
            if len(ck) < 8:
                raise CueError(f"{os.path.basename(path)} has no audio data")
            cid, size = ck[:4], int.from_bytes(ck[4:], "little")
            if cid == b"fmt ":
                fmt = f.read(size)
                fmt_ok = (int.from_bytes(fmt[0:2], "little") in (1, 0xFFFE) and int.from_bytes(fmt[2:4], "little") == 2
                          and int.from_bytes(fmt[4:8], "little") == 44100
                          and int.from_bytes(fmt[14:16], "little") == 16)
                f.seek(size & 1, 1)
            elif cid == b"data":
                if not fmt_ok:
                    raise CueError(f"{os.path.basename(path)} isn't CD audio (16-bit stereo 44.1 kHz)")
                return f.tell(), min(size, os.path.getsize(path) - f.tell())
            else:
                f.seek(size + (size & 1), 1)


def _file_frames(path: str, file_type: str, sector_size: int) -> int:
    size = wav_data(path)[1] if file_type == "WAVE" else os.path.getsize(path)
    return size // sector_size


def parse_cue(path: str) -> CueSheet:
    path = os.path.abspath(path)
    cue_dir = os.path.dirname(path)
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except UnicodeDecodeError:
        with open(path, encoding="latin-1") as f:
            text = f.read()
    tracks: list[CueTrack] = []
    raw_idx: list[dict[int, tuple[int, str, str]]] = []     # per track: index -> (frame, file, type)
    cur_file, cur_type = None, "BINARY"
    catalog = ""
    sessions = 1
    gd = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].upper()
        if cmd == "REM":
            words = " ".join(parts[1:]).upper()
            if len(parts) >= 3 and parts[1].upper() == "SESSION":
                sessions = max(sessions, int(parts[2]) if parts[2].isdigit() else 1)
            elif "HIGH-DENSITY AREA" in words:
                gd = True
            continue
        if cmd == "FILE":
            m = (re.match(r'FILE\s+"([^"]+)"\s*(\S+)?\s*$', line, re.I)
                 or re.match(r"FILE\s+(.+?)\s+(BINARY|MOTOROLA|WAVE|MP3|AIFF)\s*$", line, re.I)
                 or re.match(r"FILE\s+(\S+)()\s*$", line, re.I))
            if not m:
                raise CueError("FILE line without a file name in the CUE sheet")
            cur_type = (m.group(2) or "BINARY").upper()
            if cur_type not in ("BINARY", "MOTOROLA", "WAVE"):
                raise CueError(f"CUE file type {cur_type} isn't supported (only BINARY/MOTOROLA/WAVE)")
            cur_file = _resolve(cue_dir, m.group(1))
        elif cmd == "TRACK":
            if cur_file is None:
                raise CueError("TRACK before any FILE in the CUE sheet")
            mode = parts[2].upper() if len(parts) > 2 else ""
            if mode not in SECTOR_SIZES:
                raise CueError(f"CUE track mode {mode or '?'} isn't supported")
            tracks.append(CueTrack(int(parts[1]), mode, cur_file, cur_type))
            raw_idx.append({})
        elif cmd == "INDEX":
            if not tracks:
                raise CueError("INDEX before any TRACK in the CUE sheet")
            raw_idx[-1][int(parts[1])] = (msf_to_frames(parts[2]), cur_file, cur_type)
        elif cmd == "PREGAP" and tracks:
            tracks[-1].pregap = msf_to_frames(parts[1])
        elif cmd == "POSTGAP" and tracks:
            tracks[-1].postgap = msf_to_frames(parts[1])
        elif cmd == "FLAGS" and tracks:
            tracks[-1].flags |= {p.upper() for p in parts[1:]}
        elif cmd == "CATALOG" and len(parts) > 1:
            catalog = parts[1]
    if not tracks:
        raise CueError("The CUE sheet has no tracks")
    for t, idx in zip(tracks, raw_idx):
        if 1 not in idx:
            raise CueError(f"Track {t.number} has no INDEX 01")
        frame1, file1, type1 = idx[1]
        t.file, t.file_type = file1, type1
        for n, (frame, f, ty) in idx.items():
            if f == file1:
                t.indexes[n] = frame
            elif n == 0:                                  # EAC: gap appended to the previous file
                t.pre_file, t.pre_type, t.pre_start = f, ty, frame
            else:
                raise CueError(f"Track {t.number}'s indexes are spread over files in a way that can't be burned")
    # lengths: up to the next index stored in the same file, or to the end of the file
    for i, t in enumerate(tracks):
        end = None
        for idx in raw_idx[i + 1:]:
            here = [frame for frame, f, _ty in idx.values() if f == t.file]
            if here:
                end = min(here)
                break
        if end is None:
            end = _file_frames(t.file, t.file_type, t.sector_size)
        t.length = end - t.start
        if t.length <= 0 or t.body <= 0:
            raise CueError(f"Track {t.number} has no data (check the CUE sheet)")
        if t.pre_file:
            t.pre_length = _file_frames(t.pre_file, t.pre_type, t.sector_size) - t.pre_start
            if t.pre_length <= 0:
                raise CueError(f"Track {t.number}'s pregap is missing from its file")
    return CueSheet(path, tracks, catalog, sessions, gd)


# --------------------------------------------------------------------------
# The burned layout (shared by the TOC writer, the size check and the read-back check)

@dataclass
class TrackPlan:
    track: CueTrack
    gap: int                 # zero/silence frames written before the stored data (PREGAP)
    pre: int                 # frames taken from pre_file (EAC appended gap)
    skip: int                # stored INDEX 00 frames left out (data track 1 only)
    start: int               # frames from the track start to INDEX 01 (TOC START)
    lba: int                 # INDEX 01 LBA on the disc


def plan(cue: CueSheet) -> tuple[list[TrackPlan], int]:
    out, lba = [], 0
    for i, t in enumerate(cue.tracks):
        gap, pre, skip = t.pregap, t.pre_length, 0
        stored = t.file_pregap
        if i == 0:
            gap = max(0, gap - LEADIN_PREGAP)          # cdrdao writes the lead-in's 2 s itself
            if not t.is_audio:
                skip, stored = stored, 0               # a data track 1 starts at LBA 0
                pre = 0
        start = gap + pre + stored
        out.append(TrackPlan(t, gap, pre, skip, start, lba + start))
        lba += start + t.body + t.postgap
    return out, lba


def layout(cue: CueSheet) -> list[tuple[int, int, bool]]:
    """(track number, INDEX 01 LBA, is data) of every track as it will be on the burned disc."""
    return [(p.track.number, p.lba, not p.track.is_audio) for p in plan(cue)[0]]


def _link(work: str, src: str, name: str) -> str:
    """A plain-named link to `src` in `work` (the TOC's folder, where cdrdao runs), so TOC files only
    ever name 'file01.bin' and never have to quote odd paths: spaces, accents, Windows backslashes."""
    dst = os.path.join(work, name)
    try:
        if os.path.islink(dst) or os.path.exists(dst):
            os.unlink(dst)
    except OSError:
        pass
    src = os.path.abspath(src)
    try:
        os.symlink(src, dst)
        return name
    except (OSError, NotImplementedError):      # Windows without the symlink privilege
        pass
    try:
        os.link(src, dst)                       # a hard link works on one volume without any privilege
        return name
    except OSError:
        pass
    if '"' in src:
        raise CueError("This file's name contains a double quote, which the burning tool can't take; rename it.")
    return src.replace("\\", "/")


def _swapped_copy(work: str, src: str, name: str) -> str:
    """Copy of a big-endian (MOTOROLA) audio file with every 16-bit sample byte-swapped."""
    import numpy as np
    dst = os.path.join(work, name)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            chunk = fi.read(2352 * 1024)
            if not chunk:
                break
            if len(chunk) & 1:
                chunk += b"\0"
            fo.write(np.frombuffer(chunk, dtype=">u2").astype("<u2").tobytes())
    return name


SYNC = b"\x00" + b"\xff" * 10 + b"\x00"


def expand_2336(cue: CueSheet, work: str, progress=None) -> CueSheet:
    """MODE2/2336 and CDI/2336 tracks store each sector without its first 16 bytes: the sync pattern
    and the header, which is the sector's own disc address plus mode 2.  Both follow from where the
    sector sits on the disc, so whole 2352-byte sectors can be rebuilt exactly and the disc written
    raw like any other image.  Returns a copy of the sheet that points at the rebuilt files."""
    import copy
    import numpy as np
    if not any(t.sector_size == 2336 for t in cue.tracks):
        return cue
    users: dict[str, int] = {}
    for t in cue.tracks:
        for f in {t.file, t.pre_file} - {None}:
            users[f] = users.get(f, 0) + 1
    steps = {p.track.number: p for p in plan(cue)[0]}
    new = copy.deepcopy(cue)
    sync = np.frombuffer(SYNC, dtype=np.uint8)
    todo = [t for t in new.tracks if t.sector_size == 2336]
    for i, t in enumerate(todo):
        if t.pre_file or users[t.file] != 1:
            raise CueError(f"Track {t.number} shares its 2336-byte file with other tracks, so its sectors "
                           "can't be rebuilt exactly.")
        base = steps[t.number].lba - t.indexes[1]          # disc address of the file's first sector
        count = os.path.getsize(t.file) // 2336
        out = os.path.join(work, f"track{t.number:02d}-2352.bin")
        with open(t.file, "rb") as fi, open(out, "wb") as fo:
            done = 0
            while done < count:
                k = min(4096, count - done)
                data = np.frombuffer(fi.read(k * 2336), dtype=np.uint8).reshape(k, 2336)
                raw = np.empty((k, 2352), dtype=np.uint8)
                raw[:, :12] = sync
                # a sector before the disc's start (a skipped track-1 pregap) is never written
                a = np.maximum(base + done + np.arange(k), -150) + 150
                for col, v in ((12, a // 4500), (13, a // 75 % 60), (14, a % 75)):
                    raw[:, col] = (v // 10) * 16 + v % 10
                raw[:, 15] = 2
                raw[:, 16:] = data
                fo.write(raw.tobytes())
                done += k
                if progress:
                    progress((i + done / count) / len(todo))
        t.file, t.file_type = out, "BINARY"
        t.mode = "CDI/2352" if t.mode.startswith("CDI") else "MODE2/2352"
    return new


def write_toc(cue: CueSheet, out_path: str) -> bool:
    """Write a cdrdao TOC file that reproduces the CUE sheet; returns whether cdrdao needs --swap.

    Raw (BINARY) audio in rips is little-endian, but cdrdao expects raw audio big-endian, so --swap
    is used whenever a BINARY file holds audio.  --swap flips WAVE and MOTOROLA files as well, so in
    that case WAVE audio is handed over as raw data (a '.raw' link at the data offset, so the header
    isn't parsed) and MOTOROLA audio as a byte-swapped copy.  Every file is referenced through a
    plain-named link in the TOC's folder.
    """
    work = os.path.dirname(os.path.abspath(out_path))
    swap = any(t.is_audio and t.file_type == "BINARY" for t in cue.tracks) or \
        any(t.is_audio and t.pre_file and t.pre_type == "BINARY" for t in cue.tracks)
    modes = {t.mode for t in cue.tracks}
    if any(m.startswith("CDI") for m in modes):
        header = "CD_I"
    elif any(m.startswith("MODE2") for m in modes):
        header = "CD_ROM_XA"
    elif modes == {"AUDIO"}:
        header = "CD_DA"
    else:
        header = "CD_ROM"
    lines = [header]
    if cue.catalog and re.fullmatch(r"\d{13}", cue.catalog):
        lines.append(f'CATALOG "{cue.catalog}"')
    links: dict[tuple[str, str], str] = {}

    def ref(path: str, file_type: str, audio: bool) -> tuple[str, int]:
        """(file name for the TOC, byte offset of the samples/sectors in it)."""
        if not audio:
            kind = "bin"
        elif file_type == "WAVE":
            kind = "rawwave" if swap else "wave"
        elif file_type == "MOTOROLA" and swap:
            kind = "swapped"
        else:
            kind = "bin"
        key = (path, kind)
        if key not in links:
            n = len(links) + 1
            if kind == "swapped":
                links[key] = _swapped_copy(work, path, f"file{n:02d}.swapped.raw")
            else:
                ext = ".wav" if kind == "wave" else ".raw" if kind == "rawwave" else ".bin"
                links[key] = _link(work, path, f"file{n:02d}{ext}")
        offset = wav_data(path)[0] if kind == "rawwave" else 0
        return links[key], offset

    def piece(path: str, file_type: str, t: CueTrack, first: int, count: int) -> str:
        name, base = ref(path, file_type, t.is_audio)
        if not t.is_audio:
            if file_type == "WAVE":
                raise CueError(f"Track {t.number} is a data track inside a WAVE file")
            return f'DATAFILE "{name}" #{base + first * t.sector_size} {frames_to_msf(count)}'
        if file_type == "WAVE" and not swap:
            return f'FILE "{name}" {frames_to_msf(first)} {frames_to_msf(count)}'
        return f'FILE "{name}" #{base + first * 2352} 0 {frames_to_msf(count)}'

    for p in plan(cue)[0]:
        t = p.track
        if t.mode not in TOC_MODES:
            raise CueError(f"Track {t.number} ({t.mode}) can't be burned")
        lines += ["", f"// track {t.number}", f"TRACK {TOC_MODES[t.mode]}"]
        if t.is_audio:
            lines.append("COPY" if "DCP" in t.flags else "NO COPY")
            lines.append("PRE_EMPHASIS" if "PRE" in t.flags else "NO PRE_EMPHASIS")
            lines.append("FOUR_CHANNEL_AUDIO" if "4CH" in t.flags else "TWO_CHANNEL_AUDIO")
        if p.gap:
            lines.append(f"{'SILENCE' if t.is_audio else 'ZERO'} {frames_to_msf(p.gap)}")
        if p.pre:
            lines.append(piece(t.pre_file, t.pre_type, t, t.pre_start, p.pre))
        first = t.start + p.skip
        lines.append(piece(t.file, t.file_type, t, first, t.length - p.skip))
        if p.start:
            lines.append(f"START {frames_to_msf(p.start)}")
        for idx in sorted(k for k in t.indexes if k >= 2):
            lines.append(f"INDEX {frames_to_msf(t.indexes[idx] - t.indexes[1])}")
        if t.postgap:
            lines.append(f"{'SILENCE' if t.is_audio else 'ZERO'} {frames_to_msf(t.postgap)}")
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return swap


# --------------------------------------------------------------------------
# What system is this CD for?

def _sysid(sector16: bytes) -> str:
    if sector16[1:6] == b"CD001":
        return sector16[8:40].decode("latin-1").strip()
    return ""


def detect_cd_system(cue: CueSheet) -> tuple[str, str]:
    """(platform id, readable system name) from the disc's data tracks."""
    data = cue.data_tracks
    if not data:
        return "audio", "Audio CD"
    t = data[0]
    try:
        s0 = cue.read_user(t, 0)
        s16 = cue.read_user(t, 16)
    except OSError:
        return "cd", "CD image"
    if s0.startswith(b"SEGA SEGASATURN"):
        return "saturn", "Sega Saturn"
    if s0.startswith((b"SEGADISCSYSTEM", b"SEGABOOTDISC")):
        return "segacd", "Sega CD / Mega-CD"
    if s0.startswith(b"SEGA SEGAKATANA"):
        return "dreamcast", "Dreamcast"
    if s0[0:1] == b"\x01" and s0[1:6] == b"\x5a\x5a\x5a\x5a\x5a":
        return "3do", "3DO"
    if s0.startswith(b"PC-FX:Hu_CD-ROM"):
        return "pcfx", "PC-FX"
    if s16[1:6] == b"CD-I ":
        return "cdi", "Philips CD-i"
    if _sysid(s16) == "PLAYSTATION":
        cnf = _find_file(cue, t, "SYSTEM.CNF")
        if cnf is not None and re.search(rb"BOOT2\s*=", cnf, re.I):
            return "ps2", "PlayStation 2"
        return "ps1", "PlayStation"
    for tr in data:                           # PC Engine CD keeps its data on track 2 (and a copy at the end)
        try:
            if b"PC Engine CD-ROM SYSTEM" in cue.read_user(tr, 1):
                return "pce", "PC Engine CD"
        except OSError:
            pass
    if _find_file(cue, t, "IPL.TXT") is not None:
        return "neogeocd", "Neo Geo CD"
    if _find_file(cue, t, "CD32.TM") is not None or _find_file(cue, t, "CDTV.TM") is not None:
        return "cd32", "Amiga CD32"
    return "cd", "CD image"


def _find_file(cue: CueSheet, t: CueTrack, name: str, limit: int = 4096) -> bytes | None:
    from . import iso9660
    try:
        fs = iso9660.IsoFS(lambda lba: cue.read_user(t, lba), 0,
                           read_range=lambda lba, n: cue.read_user(t, lba, n))
        e = fs.find(name)
        return fs.read_file(e, limit=limit) if e else None
    except Exception:  # noqa: BLE001 - no readable file system is simply "not found"
        return None


def _find_system_cnf(cue: CueSheet, t: CueTrack) -> bytes | None:
    return _find_file(cue, t, "SYSTEM.CNF")


def ps_boot_file(cue: CueSheet) -> str:
    """The boot executable named in SYSTEM.CNF, e.g. 'SCES_003.11' (what LibCrypt patches key on)."""
    data = cue.data_tracks
    if not data:
        return ""
    cnf = _find_system_cnf(cue, data[0]) or b""
    m = re.search(rb"BOOT\s*=\s*cdrom0?:\\?([^\r\n;]+)", cnf, re.I)
    if not m:
        return ""
    name = m.group(1).decode("latin-1").strip().replace("/", "\\").split("\\")[-1]
    return name.upper()


def ps_game_id(cue: CueSheet) -> str:
    """'SLUS-01234' style id from SYSTEM.CNF, if any."""
    data = cue.data_tracks
    if not data:
        return ""
    cnf = _find_system_cnf(cue, data[0]) or b""
    m = re.search(rb"cdrom0?:\\?([A-Z]{4})[_-]?(\d{3})\.?(\d{2})", cnf, re.I)
    return f"{m.group(1).decode().upper()}-{m.group(2).decode()}{m.group(3).decode()}" if m else ""
