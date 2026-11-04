"""What is this file?  Platform, title, game ID, disc type and size - quickly, without unpacking.

identify(path) never converts anything; jobs.open_job() does the heavy lifting afterwards.
"""
from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field

from . import cue as CUE
from . import formats as F
from . import platforms as P
from . import quirks as Q
from . import xbox as X

GC_MAGIC = b"\xc2\x33\x9f\x3d"
WII_MAGIC = b"\x5d\x1c\x9e\xa3"
SYNC = b"\x00" + b"\xff" * 10 + b"\x00"

DC_EXTS = {".cdi", ".gdi"}
CD_EXTS = {".cue", ".ccd", ".toc"}
ISO_EXTS = {".iso", ".gcm", ".xiso", ".img", ".wbfs", ".cso", ".zso", ".gcz", ".dvd"}
TOOL_EXTS = {".rvz", ".wia", ".chd", ".ecm"}
PROGRAM_EXTS = {".elf", ".bin", ".xbe"}
ARCHIVE_EXTS = {".zip", ".7z", ".rar"}
GAME_EXTS = DC_EXTS | CD_EXTS | ISO_EXTS | TOOL_EXTS | {".bin"}
ALL_EXTS = GAME_EXTS | PROGRAM_EXTS | ARCHIVE_EXTS


@dataclass
class Identity:
    platform: str                     # platforms.PLATFORMS key, or "unknown"
    title: str
    method: str                       # "dc" | "cd" (cdrdao) | "cd-iso" (2048 ISO -> CD) | "dvd" | "folder"
    source: str
    container: str = ""               # wbfs, cso, gcz, rvz, chd, ecm, xbox-redump, xbox-folder, archive ...
    game_id: str = ""
    size: int = 0                     # bytes of the plain image that gets burned (estimate if converted)
    media: str = "cd"                 # cd | dvd | dvd-dl
    cd_mode: str = ""                 # for cd-iso: MODE1 or MODE2_FORM1
    layer_break: int | None = None    # 2048-byte sectors in layer 0 (dual-layer DVDs)
    cue: CUE.CueSheet | None = None
    raw_only: bool = False            # CD: never fall back to cooked writing (EDC-protected games)
    plus_dl_only: bool = False        # DVD: the layer break needs DVD+R DL (growisofs can't set it on DVD-R DL)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: list[tuple[str, str]] = field(default_factory=list)

    @property
    def plat(self) -> P.Platform:
        return P.get(self.platform)

    @property
    def sectors(self) -> int:
        if self.method == "cd" and self.cue is not None:
            return self.cue.total_frames
        return -(-self.size // 2048)


# --------------------------------------------------------------------------
# Titles

def title_from_name(path: str) -> str:
    from .library import title_from_name as t
    return t(path)[0]


def named_well(path: str) -> bool:
    """True when the file name is a real title (not 'game.iso' or a file inside our cache)."""
    from .util import cache_dir
    stem = os.path.splitext(os.path.basename(path))[0]
    if os.path.abspath(path).startswith(cache_dir()):
        return False
    return not re.fullmatch(r"(disc|track\s*\d*|game|image|cd\d*|dvd\d*|default|iso|data)", stem, re.I)


def _text(b: bytes) -> str:
    b = b.split(b"\0")[0]
    for enc in ("utf-8", "shift_jis", "latin-1"):
        try:
            return b.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return ""


GUESS = [(r"\b(ps1|psx|playstation 1|playstation)\b", "ps1"), (r"\b(ps2|playstation 2)\b", "ps2"),
         (r"\b(gc|gamecube|ngc)\b", "gamecube"), (r"\bwii\b", "wii"), (r"\b(xbox|xbx)\b", "xbox"),
         (r"\b(dreamcast|dc)\b", "dreamcast"), (r"\b(saturn|sat)\b", "saturn"),
         (r"\b(sega ?cd|mega ?cd|segacd|megacd)\b", "segacd"), (r"\b(pc ?engine|pce|turbografx|tg16)\b", "pce"),
         (r"\b3do\b", "3do")]


def guess_platform(path: str) -> str:
    """Best guess from folder and file names (for the library, before a file is opened)."""
    low = path.lower().replace("_", " ").replace("-", " ")
    parts = low.split(os.sep)[-4:]
    for part in reversed(parts):
        for rx, pid in GUESS:
            if re.search(rx, part):
                return pid
    ext = os.path.splitext(path)[1].lower()
    return {".cdi": "dreamcast", ".gdi": "dreamcast", ".wbfs": "wii", ".gcm": "gamecube", ".rvz": "gamecube",
            ".gcz": "gamecube", ".xiso": "xbox", ".xbe": "xbox", ".cso": "ps2", ".zso": "ps2"}.get(ext, "unknown")


# --------------------------------------------------------------------------
# CD images

def find_cue_for(bin_path: str) -> str | None:
    d = os.path.dirname(os.path.abspath(bin_path))
    stem = os.path.splitext(os.path.basename(bin_path))[0]
    base = os.path.basename(bin_path).lower()
    cands = []
    try:
        names = os.listdir(d)
    except OSError:
        return None
    for n in names:
        if n.lower().endswith(".cue"):
            if os.path.splitext(n)[0].lower() == stem.lower():
                return os.path.join(d, n)
            cands.append(os.path.join(d, n))
    for c in cands:
        try:
            with open(c, encoding="utf-8", errors="replace") as f:
                if base in f.read(65536).lower():
                    return c
        except OSError:
            pass
    return None


def synth_cue(bin_path: str) -> CUE.CueSheet | None:
    """A one-track sheet for a lone raw .bin/.img (sync pattern + Mode byte)."""
    size = os.path.getsize(bin_path)
    with open(bin_path, "rb") as f:
        head = f.read(2352)
        if head[:12] != SYNC or size % 2352:
            return None
        mode = {1: "MODE1/2352", 2: "MODE2/2352"}.get(head[15])
        if not mode:
            return None
        n = size // 2352
        for k in sorted({n - 1, n - 2, n * 3 // 4, n // 2}):          # CD audio has no sync pattern
            if 0 < k < n:
                f.seek(k * 2352)
                if f.read(12) != SYNC:
                    raise CUE.CueError("This .bin also holds CD audio tracks, so it needs its .cue file to be "
                                       "burned correctly. Put the .cue next to it and open that.")
    t = CUE.CueTrack(1, mode, os.path.abspath(bin_path), "BINARY", {1: 0})
    t.length = os.path.getsize(bin_path) // 2352
    return CUE.CueSheet(bin_path, [t])


def parse_ccd(path: str) -> CUE.CueSheet:
    """CloneCD .ccd + .img (raw 2352-byte sectors) -> sheet.  The .sub subchannel file isn't used."""
    img = os.path.splitext(path)[0] + ".img"
    if not os.path.exists(img):
        raise CUE.CueError("The .img file that belongs to this .ccd is missing")
    with open(path, encoding="latin-1") as f:
        text = f.read()
    if re.search(r"^\s*Sessions\s*=\s*([2-9])", text, re.M | re.I):
        raise CUE.CueError("Multi-session CloneCD images aren't supported")
    found: dict[int, tuple[int | None, dict[int, int]]] = {}
    for m in re.finditer(r"\[TRACK\s+(\d+)\]([^\[]*)", text, re.I):
        mode = re.search(r"MODE\s*=\s*(\d+)", m.group(2), re.I)
        idx = {int(k): int(v) for k, v in re.findall(r"INDEX\s+(\d+)\s*=\s*(\d+)", m.group(2), re.I)}
        found[int(m.group(1))] = (int(mode.group(1)) if mode else None, idx)
    if not found:                               # older files: only the TOC entries
        for m in re.finditer(r"\[Entry\s+\d+\]([^\[]*)", text, re.I):
            kv = dict(re.findall(r"(\w+)\s*=\s*(\S+)", m.group(1)))
            point = int(kv.get("Point", "0"), 0)
            if 1 <= point <= 99:
                found[point] = (None if int(kv.get("Control", "0"), 0) & 4 else 0, {1: int(kv.get("PLBA", "0"))})
    if not found:
        raise CUE.CueError("This .ccd has no tracks")
    total = os.path.getsize(img) // 2352
    tracks = []
    for num in sorted(found):
        mode, idx = found[num]
        if 1 not in idx:
            raise CUE.CueError(f"Track {num} in the .ccd has no INDEX 1")
        if mode is None:
            with open(img, "rb") as f:
                f.seek(idx[1] * 2352 + 15)
                mode = 2 if f.read(1) == b"\x02" else 1
        tracks.append(CUE.CueTrack(num, {0: "AUDIO", 1: "MODE1/2352"}.get(mode, "MODE2/2352"),
                                   os.path.abspath(img), "BINARY", dict(idx)))
    for i, t in enumerate(tracks):
        t.length = (tracks[i + 1].start if i + 1 < len(tracks) else total) - t.start
        if t.length <= 0:
            raise CUE.CueError(f"Track {t.number} in the .ccd has no data")
    return CUE.CueSheet(path, tracks)


def identify_cue(sheet: CUE.CueSheet, source: str) -> Identity:
    pid, sysname = CUE.detect_cd_system(sheet)
    title = title_from_name(source)
    ident = Identity(pid if pid in P.PLATFORMS else "cd", title, "cd", source, cue=sheet, media="cd")
    nt = len(sheet.tracks)
    audio = sum(t.is_audio for t in sheet.tracks)
    ident.details.append(("Disc", f"{nt} track{'s' * (nt != 1)}" + (f" ({audio} audio)" if audio else "")))
    if pid in ("ps1", "ps2"):
        gid = CUE.ps_game_id(sheet)
        if gid:
            ident.game_id = gid
    if pid == "dreamcast" or sheet.gd_rom:
        ident.platform, ident.method = "dreamcast", "dc"
        ident.blockers.append("This is a Dreamcast GD-ROM rip in CUE form. Open its .gdi version (or a .chd) "
                              "instead — DiscForge converts those to a bootable CD-R.")
    elif sheet.sessions > 1:
        ident.blockers.append("This CUE sheet describes a multi-session disc, which can't be burned disc-at-once. "
                              "Use a CloneCD (.ccd) or DiscJuggler (.cdi) image of it instead.")
    if pid == "ps1":
        ident.raw_only, notes = Q.ps1_notes(title)
        ident.warnings += notes
    ident.size = sheet.total_frames * 2352
    if sheet.total_frames > P.CD80 + 2000:
        ident.blockers.append("This CD image is larger than an 80-minute CD-R.")
    elif sheet.total_frames > P.CD80:
        ident.warnings.append("This image only fits if the drive can overburn slightly.")
    if sysname and pid == "cd":
        ident.details.append(("Type", sysname))
    return ident


# --------------------------------------------------------------------------
# 2048-byte images (ISO and friends)

def _has_udf(r: F.Reader) -> bool:
    for s in range(16, 24):
        d = r.read(s * 2048, 8)
        if d[1:6] in (b"BEA01", b"NSR02", b"NSR03"):
            return True
    return False


def _system_cnf(r: F.Reader) -> bytes | None:
    from . import iso9660
    try:
        fs = iso9660.IsoFS(lambda lba: r.read(lba * 2048, 2048), 0,
                           read_range=lambda lba, n: r.read(lba * 2048, n * 2048))
        e = fs.find("SYSTEM.CNF")
        return fs.read_file(e, limit=4096) if e else None
    except Exception:
        return None


def _ps_id(cnf: bytes) -> str:
    m = re.search(rb"cdrom0?:\\?([A-Z]{4})[_-]?(\d{3})\.?(\d{2})", cnf, re.I)
    return f"{m.group(1).decode().upper()}-{m.group(2).decode()}{m.group(3).decode()}" if m else ""


def identify_reader(r: F.Reader, source: str, container: str = "") -> Identity:
    title = title_from_name(source)
    h = r.read(0, 0x440)
    ident = Identity("unknown", title, "dvd", source, container, size=r.size)

    def dvd_media():
        ident.media = P.media_for(ident.sectors, "dvd")
        if ident.sectors > P.DVD9:
            ident.blockers.append("This image is larger than a dual-layer DVD.")

    if h[0x200:0x204] == b"NKIT":
        ident.platform = "wii" if h[0x18:0x1C] == WII_MAGIC else "gamecube"
        ident.blockers.append("This is an NKit image, which consoles can't read. Restore it to a full ISO "
                              "with NKit first.")
        return ident
    if h[0x1C:0x20] == GC_MAGIC or h[0x18:0x1C] == WII_MAGIC:
        wii = h[0x18:0x1C] == WII_MAGIC
        ident.platform = "wii" if wii else "gamecube"
        ident.game_id = _text(h[0:6])
        ident.title = title if named_well(source) else (_text(h[0x20:0x60]) or title)
        if not wii and r.size > P.GAMECUBE_SIZE:
            ident.warnings.append("This GameCube image is larger than a real GameCube disc.")
        elif not wii and r.size < P.GAMECUBE_SIZE:
            ident.warnings.append("This image is trimmed. Full-size images (1.46 GB) work with more loaders and "
                                  "drive chips.")
        if not wii and h[8]:
            ident.warnings.append("This game streams its music straight from the disc. GameCube drive chips play "
                                  "it fine; Wii drive chips and old Swiss versions play static instead.")
        if h[6]:
            ident.details.append(("Disc", f"Disc {h[6] + 1}"))
        dvd_media()
        if wii and ident.sectors == Q.WII_DL_SECTORS:
            ident.layer_break, ident.plus_dl_only = Q.WII_DL_BREAK, True
            ident.details.append(("Layer break", f"sector {Q.WII_DL_BREAK:,}"))
        return ident
    xb = X.partition_offset(r)
    if xb is not None:
        ident.platform = "xbox"
        try:
            xr = X.XdvdfsReader(r, xb)
            xbe = xr.find("default.xbe")
            if xbe is not None:
                info = X.xbe_info(xr.read_file(xbe, 0x10000))
                if info.get("title") and not named_well(source):
                    ident.title = info["title"]
                ident.game_id = info.get("title_id", "")
            trimmed = X.layout(xr.tree())[0] * 2048
        except F.FormatError as e:
            ident.blockers.append(str(e))
            return ident
        if xb or trimmed < r.size * 0.98:
            ident.container = "xbox-redump" if xb else "xbox-untrimmed"
            ident.size = trimmed
            ident.details.append(("Prepared as", "trimmed XISO"))
        dvd_media()
        return ident
    if X.is_xbox360(r):
        ident.platform = "unknown"
        ident.blockers.append("This is an Xbox 360 game. DiscForge burns original Xbox games only.")
        return ident
    if h[:16] == b"SEGA SEGAKATANA ":
        ident.platform, ident.method = "dreamcast", "dc"
        return ident
    if h[:15] == b"SEGA SEGASATURN":
        ident.platform, ident.method, ident.media, ident.cd_mode = "saturn", "cd-iso", "cd", "MODE1"
        ident.title = smart(_text(h[0x60:0xD0])) or title
        ident.game_id = _text(h[0x20:0x2A])
        ident.warnings.append("This Saturn image has only the data track; music on audio tracks will be missing.")
        return _cd_fit(ident)
    if h[:14] in (b"SEGADISCSYSTEM", b"SEGABOOTDISC") or h[:11] == b"SEGADISCSYS":
        ident.platform, ident.method, ident.media, ident.cd_mode = "segacd", "cd-iso", "cd", "MODE1"
        ident.warnings.append("This Sega CD image has only the data track; music on audio tracks will be missing.")
        return _cd_fit(ident)
    pvd = r.read(16 * 2048, 2048)
    if pvd[1:6] == b"CD001":
        sysid = _text(pvd[8:40])
        volid = _text(pvd[40:72])
        vol_space = struct.unpack_from("<I", pvd, 80)[0]
        if sysid == "PLAYSTATION":
            cnf = _system_cnf(r) or b""
            ident.game_id = _ps_id(cnf)
            if re.search(rb"BOOT2\s*=", cnf, re.I):
                ident.platform = "ps2"
                if _has_udf(r) or r.size > P.CD80 * 2048:
                    dvd_media()
                    if ident.media == "dvd-dl":
                        second = r.read((vol_space + 16) * 2048, 6)
                        if second[1:6] == b"CD001":
                            ident.layer_break, ident.plus_dl_only = vol_space, True
                            ident.details.append(("Layer break", f"sector {vol_space:,}"))
                            why = Q.ps2_layer_problem(vol_space, ident.sectors)
                            if why:
                                ident.blockers.append(why)
                        else:
                            ident.warnings.append("Couldn't find this dual-layer game's layer break, so the "
                                                  "drive picks one; the copy may not work.")
                else:
                    ident.method, ident.media, ident.cd_mode = "cd-iso", "cd", "MODE2_FORM1"
                    ident.details.append(("Disc", "PS2 CD game"))
                    _cd_fit(ident)
            else:
                ident.platform, ident.method, ident.media, ident.cd_mode = "ps1", "cd-iso", "cd", "MODE2_FORM1"
                ident.warnings.append("PlayStation games are best burned from a .cue/.bin rip: an .iso can't hold "
                                      "the CD's streamed music and video, so those may not play.")
                _cd_fit(ident)
            return ident
        if sysid.startswith("PSP GAME") or volid.startswith("UMD"):
            ident.blockers.append("This is a PSP game; PSP discs (UMD) can't be burned.")
            return ident
        ident.platform = "pc"
        if volid:
            ident.details.append(("Volume", volid))
        if r.size <= P.CD80 * 2048:
            ident.method, ident.media, ident.cd_mode = "cd-iso", "cd", "MODE1"
        else:
            dvd_media()
        return ident
    if _has_udf(r):
        ident.platform = "pc"
        dvd_media()
        return ident
    ident.blockers.append("DiscForge doesn't recognise this disc image.")
    return ident


def smart(t: str) -> str:
    from .library import smart_case
    return smart_case(t) if t else t


def _cd_fit(ident: Identity) -> Identity:
    if ident.sectors > P.CD80:
        ident.blockers.append("This image is larger than an 80-minute CD-R.")
    return ident


# --------------------------------------------------------------------------

def identify(path: str) -> Identity:
    """Quick identification of any supported file or folder.  Never raises for bad input:
    problems become blockers."""
    path = os.path.abspath(os.path.expanduser(path))
    title = title_from_name(path)
    try:
        return _identify(path, title)
    except (OSError, F.FormatError, CUE.CueError, struct.error, ValueError) as e:
        ident = Identity(guess_platform(path), title, "", path)
        ident.blockers.append(str(e) or "This file couldn't be read.")
        return ident


def _identify(path: str, title: str) -> Identity:
    if os.path.isdir(path):
        names = {n.lower() for n in os.listdir(path)}
        if "default.xbe" in names:
            root = X.folder_tree(path)
            ident = Identity("xbox", title, "dvd", path, "xbox-folder", size=X.layout(root)[0] * 2048)
            with open(os.path.join(path, next(n for n in os.listdir(path) if n.lower() == "default.xbe")), "rb") as f:
                info = X.xbe_info(f.read(0x10000))
            ident.title = info.get("title") or title
            ident.game_id = info.get("title_id", "")
            ident.media = P.media_for(ident.sectors, "dvd")
            return ident
        return Identity("dreamcast", title, "folder", path, "folder")   # jobs decides (homebrew / data disc)
    ext = os.path.splitext(path)[1].lower()
    if ext in ARCHIVE_EXTS:
        return Identity(guess_platform(path), title, "", path, "archive")
    if ext in DC_EXTS:
        return Identity("dreamcast", title, "dc", path, media="cd")
    if ext == ".chd":
        info = F.chd_info(path)
        if info["kind"] == "gd":
            return Identity("dreamcast", title, "dc", path, "chd", media="cd")
        ident = Identity(guess_platform(path), title, "cd" if info["kind"] == "cd" else "dvd", path, "chd",
                         size=info["logical_size"], media="cd" if info["kind"] == "cd" else "dvd")
        if info["kind"] == "dvd":
            ident.media = P.media_for(ident.sectors, "dvd")
        elif info["kind"] != "cd":
            ident.blockers.append("This CHD isn't a CD or DVD image.")
        return ident
    if ext == ".ecm":
        return Identity(guess_platform(path), title, "cd", path, "ecm", size=os.path.getsize(path))
    if ext in (".rvz", ".wia"):
        h = F.wia_header(path)
        ident = Identity(h["platform"], title if named_well(path) else _text(h["header"][0x20:0x60]) or title,
                         "dvd", path, ext[1:],
                         game_id=_text(h["header"][0:6]), size=h["iso_size"])
        ident.media = P.media_for(ident.sectors, "dvd")
        if not F.dolphin_tool():
            ident.warnings.append("Needs Dolphin's converter (dolphin-tool) to unpack this image.")
        return ident
    if ext == ".cue":
        return identify_cue(CUE.parse_cue(path), path)
    if ext == ".ccd":
        return identify_cue(parse_ccd(path), path)
    if ext == ".toc":
        ident = Identity("cd", title, "cd", path, "toc", media="cd")
        return ident
    if ext == ".xbe":
        return _identify(os.path.dirname(path), title_from_name(os.path.dirname(path)))
    if ext in (".bin", ".img"):
        cue = find_cue_for(path) if ext == ".bin" else None
        if cue:
            return identify_cue(CUE.parse_cue(cue), cue)
        if ext == ".img" and os.path.exists(os.path.splitext(path)[0] + ".ccd"):
            return identify_cue(parse_ccd(os.path.splitext(path)[0] + ".ccd"), path)
        sheet = synth_cue(path)
        if sheet:
            return identify_cue(sheet, path)
        if ext == ".bin":
            return Identity("dreamcast", title, "dc", path, "program", media="cd")
    if ext == ".elf":
        return Identity("dreamcast", title, "dc", path, "program", media="cd")
    with F.open_reader(path) as r:
        container = {F.WbfsReader: "wbfs", F.CsoReader: "cso", F.GczReader: "gcz"}.get(type(r), "")
        return identify_reader(r, path, container)
