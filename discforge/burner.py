"""Drive detection, media checks and the actual burn (cdrecord), plus read-back verify.

The burn mirrors the long-proven Linux/Bootdreams Dreamcast method, checked against the
cdrtools 3.02 sources: Track-At-Once, session 1 written with -multi (left open), session 2
written with -xa (XA Mode 2 Form 1; the session is flagged CD-ROM XA) and closed.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
import sys
from typing import Callable

from . import sectors as S
from .plan import BurnPlan, PlanSession, PlanTrack
from .util import DcError, cache_dir
from . import tools

try:
    import fcntl                     # Linux/BSD/macOS; the CD ioctls below are Linux-only
except ImportError:                  # Windows
    fcntl = None                     # type: ignore[assignment]

LINUX = sys.platform.startswith("linux")

# linux/cdrom.h, linux/fs.h
CDROMEJECT = 0x5309
CDROMCLOSETRAY = 0x5319
CDROMREADTOCHDR = 0x5305
CDROMREADTOCENTRY = 0x5306
CDROMMULTISESSION = 0x5310
CDROM_DRIVE_STATUS = 0x5326
CDROM_LOCKDOOR = 0x5329
CDROM_LBA = 0x01
BLKGETSIZE64 = 0x80081272
CDS_NO_INFO, CDS_NO_DISC, CDS_TRAY_OPEN, CDS_DRIVE_NOT_READY, CDS_DISC_OK = 0, 1, 2, 3, 4

SPEEDS = [0, 4, 8, 10, 12, 16, 24, 32, 48]   # 0 = let the drive decide
GRACE_TIME = 3                               # cdrecord's minimum (MIN_GRACE_TIME)


class BurnError(DcError):
    pass


class Cancelled(BurnError):
    pass


def cdrecord_path() -> str | None:
    return tools.find("cdrecord")


# --------------------------------------------------------------------------
# Drives


@dataclass
class Drive:
    dev: str
    name: str
    can_write_cd: bool | None = None
    accessible: bool = True

    @property
    def label(self) -> str:
        bits = [self.name or "Optical drive", f"({self.dev})"]
        if self.can_write_cd is False:
            bits.append("- read only")
        if not self.accessible:
            bits.append("- no permission")
        return " ".join(bits)


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def list_drives():
    """All optical drives (see devices.py; the objects have .dev, .name, .accessible, .can_write_cd)."""
    from . import devices
    return devices.list_drives()


# --------------------------------------------------------------------------
# Media


@dataclass
class Media:
    present: bool
    kind: str = ""                   # "CD-R", "CD-RW", "DVD+R", ...
    blank: bool = False
    status: str = ""                 # empty / incomplete / complete
    erasable: bool = False
    capacity: int | None = None      # writable sectors when blank
    nwa: int | None = None
    modes: str = ""                  # supported write modes (TAO SAO ...)
    raw: str = ""

    @property
    def is_cd(self) -> bool:
        return self.kind.upper().startswith("CD")


NO_DISC_RX = re.compile(r"No disk|Wrong disk|medium not present|Cannot load media", re.I)


def _addr(dev: str, tool: str = "cdrecord") -> str:
    from . import devices
    return devices.address(dev, tool)


def _run(args: list[str], timeout: float = 90) -> subprocess.CompletedProcess:
    env = tools.env({"LC_ALL": "C", "LANG": "C"})
    try:
        return subprocess.run(args, capture_output=True, text=True, errors="replace", env=env,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise BurnError("The drive stopped responding. Unplug it, plug it back in and try again.") from None


def parse_minfo(out: str) -> Media:
    """Parse `cdrecord -minfo` output (strings as printed by cdrtools' scsi_mmc.c)."""
    m = Media(present=False, raw=out)
    if NO_DISC_RX.search(out):
        return m
    if mt := re.search(r"Mounted media type:\s*(\S+)", out):
        m.present = True
        m.kind = mt.group(1)
    if st := re.search(r"disk status:\s*(\w+)", out):
        m.present = True
        m.status = st.group(1)
        m.blank = m.status == "empty"
    m.erasable = bool(re.search(r"Disk Is erasable", out))
    if rs := re.search(r"Remaining writable size:\s*(\d+)", out):
        m.capacity = int(rs.group(1))
    if nw := re.search(r"Next writable address:\s*(\d+)", out):
        m.nwa = int(nw.group(1))
    if sm := re.search(r"Supported modes:\s*(.*)", out):
        m.modes = sm.group(1).strip()
    return m


def _open_failed(out: str, dev: str) -> None:
    """Raise a clear error when cdrecord couldn't use the device at all."""
    if re.search(r"Cannot open (or use )?SCSI driver|Cannot open '?/dev", out, re.I):
        if re.search(r"Permission denied", out, re.I):
            raise BurnError(f"No permission to use {dev}. Add yourself to the 'optical' group "
                            "(sudo usermod -aG optical $USER, then log out and in) and try again.")
        if re.search(r"busy", out, re.I):
            raise BurnError("Another program is using the drive. Close other disc or music apps and try again.")
        raise BurnError(f"Couldn't talk to the drive {dev}. Unplug it, plug it back in and try again.")


def media_info(dev: str) -> Media:
    tool = cdrecord_path()
    if not tool:
        raise BurnError("cdrecord is not installed (sudo pacman -S cdrtools)")
    r = _run([tool, f"dev={_addr(dev)}", "-minfo"])
    out = r.stdout + "\n" + r.stderr
    m = parse_minfo(out)
    if NO_DISC_RX.search(out):
        return m
    if not m.present:
        _open_failed(out, dev)
    if m.capacity is None or not m.present:  # drives without -minfo: fall back to ATIP
        a = _run([tool, f"dev={_addr(dev)}", "-atip"])
        if lo := re.search(r"ATIP start of lead out:\s*(\d+)", a.stdout):
            m.capacity = int(lo.group(1))
            m.present = True
        if not m.kind and "ATIP info" in a.stdout:
            m.kind = "CD-RW" if re.search(r"Is erasable", a.stdout) else "CD-R"
            m.erasable = m.kind == "CD-RW"
        if m.present and not m.status:  # blank discs have no TOC yet
            t = _run([tool, f"dev={_addr(dev)}", "-toc"])
            if re.search(r"Cannot read TOC|No TOC", t.stdout + t.stderr, re.I):
                m.status, m.blank = "empty", True
            elif re.search(r"first:\s*\d+\s+last", t.stdout):
                m.status = "written"
    return m


@dataclass
class DriveCaps:
    writes_cd_r: bool | None = None
    cd_write_speeds: list[int] = field(default_factory=list)   # e.g. [10, 16, 24, 32, 48]


def parse_prcap(out: str) -> DriveCaps:
    """Parse `cdrecord -prcap` (MMC page 2A): CD-R writing support and CD write speeds."""
    caps = DriveCaps()
    if re.search(r"Does write CD-R media", out):
        caps.writes_cd_r = True
    elif re.search(r"Does not write CD-R media", out):
        caps.writes_cd_r = False
    speeds = {int(m.group(1)) for m in re.finditer(r"Write speed #\s*\d+:.*?\(CD\s+(\d+)x", out)}
    if not speeds and (mx := re.search(r"Maximum write speed:.*?\(CD\s+(\d+)x", out)):
        speeds.add(int(mx.group(1)))
    caps.cd_write_speeds = sorted(s for s in speeds if s > 0)
    return caps


def drive_caps(dev: str) -> DriveCaps:
    tool = cdrecord_path()
    if not tool:
        return DriveCaps()
    try:
        r = _run([tool, f"dev={_addr(dev)}", "-prcap"], timeout=40)
    except BurnError:
        return DriveCaps()
    return parse_prcap(r.stdout + r.stderr)


DREAMCAST_SPEED = 8   # Dreamcast lasers read slow burns best; modern drives rarely go below 8-16x


def choose_speed(speeds: list[int], preferred: int = DREAMCAST_SPEED) -> int:
    """Slowest supported CD speed that is at least `preferred` (else the fastest below it)."""
    speeds = sorted({s for s in speeds if s > 0})
    if not speeds:
        return preferred
    at_least = [s for s in speeds if s >= preferred]
    return at_least[0] if at_least else speeds[-1]


def msinfo(dev: str) -> tuple[int, int] | None:
    try:
        r = _run([cdrecord_path() or "cdrecord", f"dev={_addr(dev)}", "-msinfo"], timeout=60)
    except BurnError:
        return None
    for line in reversed(r.stdout.splitlines()):
        if mm := re.fullmatch(r"\s*(\d+),(\d+)\s*", line):
            return int(mm.group(1)), int(mm.group(2))
    return None


# --------------------------------------------------------------------------
# Low level CD-ROM ioctls (used for verification after the burn)


def _open_dev(dev: str) -> int:
    return os.open(dev, os.O_RDONLY | os.O_NONBLOCK)


def drive_status(dev: str) -> int:
    """CDS_* tray/disc state.  Systems without the Linux ioctl report CDS_NO_INFO, so callers look
    at the disc itself (media_info) instead."""
    if not LINUX:
        return CDS_NO_INFO
    fd = _open_dev(dev)
    try:
        return fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0)
    finally:
        os.close(fd)


def _cdrecord_toc(dev: str) -> list[tuple[int, int, bool]]:
    """`cdrecord -toc`, for systems without the Linux CD ioctls."""
    from . import devices
    tool = cdrecord_path()
    if not tool:
        raise OSError("cdrecord is not available")
    r = _run([tool, f"dev={devices.address(dev, 'cdrecord')}", "-toc"], timeout=90)
    out = []
    for m in re.finditer(r"track:\s*(\d+|lout)\s+lba:\s*(-?\d+).*?control:\s*(\d+)", r.stdout):
        num = 0xAA if m.group(1) == "lout" else int(m.group(1))
        out.append((num, int(m.group(2)), bool(int(m.group(3)) & 4)))
    if not out:
        raise OSError("couldn't read the disc's table of contents")
    return out


def last_session(dev: str) -> tuple[int, bool]:
    if not LINUX:
        info = msinfo(dev)
        if not info:
            raise OSError("couldn't read the last session")
        return info[0], True
    fd = _open_dev(dev)
    try:
        buf = bytearray(struct.pack("iBBxx", 0, 0, CDROM_LBA))
        fcntl.ioctl(fd, CDROMMULTISESSION, buf, True)
        lba, xa, _fmt = struct.unpack("iBBxx", buf)
        return lba, bool(xa)
    finally:
        os.close(fd)


def read_toc(dev: str) -> list[tuple[int, int, bool]]:
    """[(track number, LBA, is_data)] including the lead-out as track 0xAA."""
    if not LINUX:
        return _cdrecord_toc(dev)
    fd = _open_dev(dev)
    try:
        hdr = bytearray(2)
        fcntl.ioctl(fd, CDROMREADTOCHDR, hdr, True)
        first, last = hdr[0], hdr[1]
        out = []
        for trk in list(range(first, last + 1)) + [0xAA]:
            buf = bytearray(struct.pack("BBBxiBxxx", trk, 0, CDROM_LBA, 0, 0))
            fcntl.ioctl(fd, CDROMREADTOCENTRY, buf, True)
            _t, adr_ctrl, _f, lba, _dm = struct.unpack("BBBxiBxxx", buf)
            out.append((trk, lba, bool((adr_ctrl >> 4) & 0x04)))
        return out
    finally:
        os.close(fd)


def block_size(dev: str) -> int:
    """Size the kernel currently believes the disc has (0 if unknown/stale or not Linux)."""
    if not LINUX:
        return 0
    try:
        fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return 0
    try:
        buf = bytearray(8)
        fcntl.ioctl(fd, BLKGETSIZE64, buf, True)
        return struct.unpack("Q", buf)[0]
    except OSError:
        return 0
    finally:
        os.close(fd)


def eject(dev: str) -> bool:
    if not LINUX:
        from . import devices
        return devices.eject(dev)
    try:
        fd = _open_dev(dev)
        try:
            fcntl.ioctl(fd, CDROM_LOCKDOOR, 0)
            fcntl.ioctl(fd, CDROMEJECT, 0)
            return True
        finally:
            os.close(fd)
    except OSError:
        return False


def close_tray(dev: str) -> bool:
    if not LINUX:
        from . import devices
        return devices.close_tray(dev)
    try:
        fd = _open_dev(dev)
        try:
            fcntl.ioctl(fd, CDROMCLOSETRAY, 0)
            return True
        finally:
            os.close(fd)
    except OSError:
        return False


class SleepInhibitor:
    """Keeps the computer awake while a disc is being written: a systemd-logind inhibitor lock on
    Linux, caffeinate on macOS, SetThreadExecutionState on Windows."""

    def __init__(self, why: str = "Burning a disc"):
        self.why = why
        self.proc: subprocess.Popen | None = None
        self.win = False

    def __enter__(self):
        if sys.platform.startswith("win"):
            try:
                import ctypes
                # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000040)
                self.win = True
            except (OSError, AttributeError):
                pass
            return self
        if sys.platform == "darwin" and shutil.which("caffeinate"):
            try:
                self.proc = subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                             stdin=subprocess.DEVNULL)
            except OSError:
                self.proc = None
            return self
        tool = shutil.which("systemd-inhibit")
        if tool:
            try:
                self.proc = subprocess.Popen([tool, "--what=sleep:idle", "--who=DiscForge",
                                              f"--why={self.why}", "--mode=block", "sleep", "infinity"],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                             stdin=subprocess.DEVNULL)
            except OSError:
                self.proc = None
        return self

    def __exit__(self, *exc):
        if self.win:
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except (OSError, AttributeError):
                pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        return False


# cdrecord always prints these when it runs without root; they say nothing about the failure
HARMLESS = re.compile(r"WARNING|Warning:|Cannot do mlockall|Cannot set RR-scheduler|setpriority|"
                      r"Open by 'devname' is unintentional", re.I)


def humanize_failure(text: str) -> tuple[str, str]:
    """(friendly reason, what to try) for a failed burn's cdrecord output."""
    t = "\n".join(ln for ln in text.lower().splitlines() if not HARMLESS.search(ln))
    if "power calibration" in t or ("opc" in t and "fail" in t):
        return ("The drive couldn't tune its laser for this disc.",
                "Try a CD-R from a different brand (Verbatim discs are a safe bet).")
    if any(k in t for k in ("medium error", "write error", "input/output error", "sense key", "illegal request",
                            "write fail")):
        return ("The drive reported a write error on this disc.",
                "Try a fresh blank CD-R, ideally another brand. Cleaning the drive's lens can help too.")
    if NO_DISC_RX.search(t):
        return ("The disc disappeared from the drive.", "Put a blank CD-R back in and try again.")
    if "resource busy" in t or "device busy" in t:
        return ("Another program is using the drive.", "Close other disc or music apps and try again.")
    if "permission denied" in t and "cannot open" in t:
        return ("This account isn't allowed to use the drive.", "Use “Allow access”, then log out and back in.")
    if "buffer underrun" in t:
        return ("The computer couldn't send data fast enough.", "Close heavy programs and try again.")
    if "cannot get next writable address" in t:
        return ("The drive couldn't find where to continue on the disc.", "Try again with a fresh blank CD-R.")
    return ("The burn stopped unexpectedly.", "Try again with a fresh blank CD-R.")


# --------------------------------------------------------------------------
# Burning


def write_wav(path: str, src: S.SectorSource) -> None:
    """cdrecord recognises WAV by its header (stereo / 44.1 kHz / 16 bit) and treats the samples
    as little-endian, which is how CD audio is stored in images."""
    size = src.count * S.RAW_SIZE
    hdr = (b"RIFF" + struct.pack("<I", 36 + size) + b"WAVE" + b"fmt " +
           struct.pack("<IHHIIHH", 16, 1, 2, 44100, 44100 * 4, 4, 16) + b"data" + struct.pack("<I", size))
    with open(path, "wb") as f:
        f.write(hdr)
        for chunk in src.iter_chunks(1024):
            f.write(chunk)


@dataclass
class BurnOptions:
    dev: str
    speed: int | None = 8          # None: the console's safe speed; 0: as fast as the drive allows
    simulate: bool = False
    verify: bool = True
    eject: bool = True
    allow_rw: bool = False


@dataclass
class BurnResult:
    ok: bool
    simulated: bool
    verified: bool | None = None
    messages: list[str] = field(default_factory=list)


def _private_copy(plan: BurnPlan) -> BurnPlan:
    """The burner may rebuild a homebrew session; never change the caller's plan object."""
    import copy
    p = copy.copy(plan)
    p.sessions = [PlanSession(s.index, list(s.tracks), s.final) for s in plan.sessions]
    return p


TRANSIENT_RX = re.compile(r"resource busy|not ready|No disk|medium not present|Cannot open|reload the media|"
                          r"Cannot get next writable address|Cannot load media|becoming ready", re.I)


class Burner:
    """Runs a BurnPlan through cdrecord.  Thread-friendly: pass callbacks, call cancel()."""

    def __init__(self, opts: BurnOptions, log: Callable[[str], None] = print,
                 progress: Callable[[float, str], None] | None = None,
                 events: Callable[[dict], None] | None = None):
        """`events` receives structured updates for a UI, e.g.
        {"stage": "write", "session": 2, "sessions": 2, "frac": 0.41, "mb": 250, "mb_total": 612, "speed": 16.0}
        stages: prepare, write, fixate, check, verify."""
        self.opts = opts
        self.log = log
        self.progress = progress or (lambda f, t: None)
        self.events = events or (lambda e: None)
        self._cancel = threading.Event()
        self._proc: subprocess.Popen | None = None
        self.writing = False
        self.sessions = 0
        self.speed: float | None = None

    def cancel(self) -> None:
        self._cancel.set()
        p = self._proc
        if p and p.poll() is None:
            p.send_signal(2)  # SIGINT: cdrecord stops cleanly (a started disc is lost)

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise Cancelled("Cancelled.")

    # -- pre-flight --------------------------------------------------------
    def preflight(self, plan: BurnPlan) -> Media:
        if not plan.ok:
            raise BurnError("The image can't be burned:\n- " + "\n- ".join(plan.errors))
        if not cdrecord_path():
            raise BurnError("cdrecord is not installed (sudo pacman -S cdrtools).")
        dev = self.opts.dev
        if not os.path.exists(dev):
            raise BurnError(f"{dev} does not exist. Is the burner plugged in?")
        if not os.access(dev, os.R_OK | os.W_OK):
            raise BurnError(f"No permission to use {dev}. Run: sudo usermod -aG optical $USER (then log out and in).")
        self.log(f"Checking the disc in {dev}…")
        m = media_info(dev)
        if not m.present:
            raise BurnError("No disc in the drive. Insert a blank CD-R.")
        if m.kind and not m.is_cd:
            raise BurnError(f"The disc is a {m.kind}. Dreamcast needs a CD-R.")
        if m.erasable and not self.opts.allow_rw:
            raise BurnError("That's a CD-RW. The Dreamcast laser can't read CD-RW reliably; use a CD-R.")
        if not m.blank:
            raise BurnError(f"The disc is not blank (status: {m.status or 'unknown'}). Insert a blank CD-R.")
        if m.modes and "TAO" not in m.modes.split():
            raise BurnError(f"This drive doesn't support Track-At-Once writing (modes: {m.modes}).")
        if m.capacity and plan.end_lba > m.capacity:
            raise BurnError(f"Image needs {plan.end_lba} sectors but the disc has only {m.capacity}. "
                            "Use an 80-minute (700 MB) CD-R.")
        self.log(f"Disc: {m.kind or 'CD-R'}, blank, {m.capacity or '?'} sectors free.")
        return m

    # -- the burn ------------------------------------------------------------
    def burn(self, plan: BurnPlan) -> BurnResult:
        plan = _private_copy(plan)
        self.events({"stage": "prepare", "frac": 0.0, "part": 0.0})
        self.preflight(plan)
        from . import devices
        devices.prepare_for_burn(self.opts.dev, self.log)
        work = os.path.join(cache_dir(), f"burn-{os.getpid()}-{int(time.time())}")
        os.makedirs(work)
        need = sum(t.size_bytes for s in plan.sessions for t in s.tracks) + (64 << 20)
        if shutil.disk_usage(work).free < need:
            shutil.rmtree(work, ignore_errors=True)
            raise BurnError(f"Not enough free space in {cache_dir()} to prepare the disc "
                            f"(needs about {need >> 20} MB).")
        total = max(1, plan.total_write_sectors)
        self.sessions = len(plan.sessions)
        result = BurnResult(ok=False, simulated=self.opts.simulate)
        try:
            with SleepInhibitor():
                # Everything is prepared and checked BEFORE the laser touches the disc, so a
                # problem with the image can never leave you with a half-written CD-R.
                files: dict[int, list[str]] = {}
                boot_hash = None
                done = 0
                for s in plan.sessions:
                    files[s.index], h = self._prepare(s, work, done, total)
                    boot_hash = h or boot_hash
                    done += sum(t.sectors for t in s.tracks)
                done_before = 0
                for s in plan.sessions:
                    self._check_cancel()
                    self._record(s, files[s.index], done_before, total)
                    for f in files.pop(s.index):
                        os.remove(f)
                    done_before += sum(t.sectors for t in s.tracks)
                    if not s.final and not self.opts.simulate:
                        self.events({"stage": "check", "session": s.index + 1, "frac": done_before / total})
                        if self._check_next_session(plan, s):     # homebrew rebuilt for another LBA
                            nxt = plan.sessions[s.index + 1]
                            for f in files.pop(nxt.index, []):
                                os.remove(f)
                            files[nxt.index], h = self._prepare(nxt, work, done_before, total)
                            boot_hash = h or boot_hash
                result.ok = True
                self.progress(1.0, "Done")
                if self.opts.simulate:
                    result.messages.append("Simulation finished: the drive accepted every step (nothing was written).")
                elif self.opts.verify:
                    result.verified = self.verify(plan, boot_hash)
            if self.opts.eject and not self.opts.simulate:
                eject(self.opts.dev)
            return result
        finally:
            self.writing = False
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def verify_length(t: PlanTrack) -> int:
        """Sectors of the boot track to read back: the filesystem's declared size, and never
        the track's last 2 sectors (some drives can't read right up to TAO run-out blocks)."""
        n = t.sectors
        try:
            vs = struct.unpack_from("<I", t.source.read(16, 1), 80)[0]
            if 0 < vs <= t.sectors:
                n = vs                        # mkisofs -C stores the session's own size
            elif t.lba < vs <= t.lba + t.sectors:
                n = vs - t.lba                # some tools store the absolute end
        except (IndexError, struct.error):
            pass
        return max(0, min(n, t.sectors - 2))

    def _prepare(self, s: PlanSession, work: str, done_before: int, total: int) -> tuple[list[str], str | None]:
        files = []
        boot_hash = None
        for i, t in enumerate(s.tracks):
            ext = "wav" if t.mode == S.AUDIO else "iso"
            path = os.path.join(work, f"s{s.index + 1}t{i + 1}.{ext}")
            self.progress(done_before / total, f"Preparing session {s.index + 1} track {i + 1}…")
            if t.mode == S.AUDIO:
                write_wav(path, t.source)
            else:
                h = hashlib.sha256()
                vlen = self.verify_length(t) if s.final and i == 0 else 0
                with open(path, "wb") as f:
                    n = 0
                    for chunk in t.source.iter_chunks(2048):
                        self._check_cancel()
                        k = len(chunk) // 2048
                        if t.mode == S.MODE2 and any(t.source.form2_flags(n, k)):
                            raise BurnError("This image has XA Form 2 sectors (streamed video/audio) inside its "
                                            "data track. They can't be written in Dreamcast selfboot format, so "
                                            "nothing was burned. Look for another version of this image.")
                        f.write(chunk)
                        if n < vlen:
                            h.update(chunk[:(min(vlen, n + k) - n) * 2048])
                        n += k
                        part = (done_before + n) / total
                        self.progress(done_before / total,
                                      f"Preparing session {s.index + 1}: {100 * n // max(1, t.sectors)}%")
                        self.events({"stage": "prepare", "session": s.index + 1, "frac": 0.0, "part": part})
                if vlen:
                    boot_hash = h.hexdigest()
            files.append(path)
        return files, boot_hash

    def _record(self, s: PlanSession, files: list[str], done_before: int, total: int) -> None:
        for attempt in range(3):
            started, rc, tail = self._run_cdrecord(s, files, done_before, total)
            if rc == 0:
                break
            if self._cancel.is_set():
                raise Cancelled("Cancelled while writing; the disc is probably unusable.")
            real = [ln for ln in tail if not HARMLESS.search(ln)]
            text = "\n".join(real)
            no_access = any("permission denied" in ln.lower() for ln in real)
            # Only retry when nothing was written yet: e.g. the desktop was still probing the new disc.
            if not started and attempt < 2 and TRANSIENT_RX.search(text) and not no_access:
                self.log(f"The drive wasn't ready (attempt {attempt + 1}); waiting a moment and retrying…")
                for _ in range(8):
                    self._check_cancel()
                    time.sleep(1)
                continue
            raise BurnError(f"cdrecord failed (exit {rc}):\n" + "\n".join(tail[-12:]))

    def _run_cdrecord(self, s: PlanSession, files: list[str], done_before: int, total: int):
        o = self.opts
        args = [cdrecord_path(), "-v", f"dev={_addr(o.dev)}", f"gracetime={GRACE_TIME}", "driveropts=burnfree", "-tao"]
        if o.speed:
            args.append(f"speed={o.speed}")
        if o.simulate:
            args.append("-dummy")
        if not s.final:
            args.append("-multi")
        for t, f in zip(s.tracks, files):
            args += [{S.AUDIO: "-audio", S.MODE1: "-data", S.MODE2: "-xa"}[t.mode], f]
        what = "Simulating" if o.simulate else "Writing"
        self.log(f"{what} session {s.index + 1}: " + " ".join(a if " " not in a else repr(a) for a in args))
        sess_sectors = sum(t.sectors for t in s.tracks)
        env = tools.env({"LC_ALL": "C", "LANG": "C"})
        self.writing = True
        self._proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                      stdin=subprocess.DEVNULL)
        buf = b""
        tail: list[str] = []
        track_rx = re.compile(r"Track\s+(\d+):\s+(\d+)\s+of\s+(\d+)\s+MB written")
        start_rx = re.compile(r"Starting new track at sector:\s*(\d+)")
        speed_rx = re.compile(r"at speed\s+(\d+(?:\.\d+)?)\s+in")
        live_rx = re.compile(r"(\d+(?:\.\d+)?)x\.\s*$")
        starts: list[int] = []
        started = False
        seen_tracks: list[int] = []   # cdrecord's track numbers, in the order this run writes them
        base = {"session": s.index + 1, "sessions": self.sessions, "test": o.simulate}
        self.events({**base, "stage": "write", "frac": done_before / total, "mb": 0,
                     "mb_total": sess_sectors * 2048 >> 20})
        proc = self._proc
        assert proc.stdout
        try:
            while True:
                ch = proc.stdout.read1(4096)
                if not ch:
                    break
                buf += ch
                parts = re.split(rb"[\r\n]", buf)
                buf = parts.pop()
                for raw in parts:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    m = track_rx.search(line)
                    if m:
                        started = True
                        tno, mb_done, mb_tot = (int(x) for x in m.groups())
                        frac_track = mb_done / mb_tot if mb_tot else 0
                        if tno not in seen_tracks:
                            seen_tracks.append(tno)
                        idx = min(seen_tracks.index(tno), len(s.tracks) - 1)
                        before = sum(t.sectors for t in s.tracks[:idx])
                        cur = before + frac_track * s.tracks[idx].sectors
                        if (lv := live_rx.search(line)):
                            self.speed = float(lv.group(1))
                        self.progress((done_before + cur) / total,
                                      f"{what} session {s.index + 1}: {mb_done} of {mb_tot} MB")
                        self.events({**base, "stage": "write", "frac": (done_before + cur) / total,
                                     "mb": mb_done, "mb_total": mb_tot, "speed": self.speed})
                        continue
                    if (ms := start_rx.search(line)):
                        started = True
                        starts.append(int(ms.group(1)))
                    if (sp := speed_rx.search(line)):
                        self.speed = float(sp.group(1))
                        self.events({**base, "stage": "write", "frac": done_before / total, "speed": self.speed})
                    if "Fixating" in line:
                        started = True
                        self.progress((done_before + sess_sectors) / total, f"Fixating session {s.index + 1}…")
                        self.events({**base, "stage": "fixate", "frac": (done_before + sess_sectors) / total})
                    tail.append(line)
                    tail = tail[-60:]
                    self.log(line)
            rc = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            self._proc = None
            self.writing = False
        if self._cancel.is_set():
            raise Cancelled("Cancelled while writing; the disc is probably unusable.")
        if rc == 0 and starts and not o.simulate and starts[0] != s.start_lba:
            raise BurnError(f"The drive wrote session {s.index + 1} at sector {starts[0]} instead of {s.start_lba}.")
        return started, rc, tail

    def _check_next_session(self, plan: BurnPlan, s: PlanSession) -> bool:
        """Confirm the drive will put the next session exactly where its data was built for.
        Returns True if the (homebrew) next session had to be rebuilt for another LBA."""
        nxt = plan.sessions[s.index + 1]
        info = None
        for _ in range(30):  # some drives need a moment after fixating
            self._check_cancel()
            info = msinfo(self.opts.dev)
            if info:
                break
            time.sleep(2)
        if not info:
            raise BurnError("Couldn't read the multisession info after session 1.")
        self.log(f"Drive reports next writable address {info[1]} (planned {nxt.start_lba}).")
        if info[1] == nxt.start_lba:
            return False
        disc = plan.disc
        if disc.rebuild is not None and nxt.final and len(nxt.tracks) == 1:
            self.log(f"Rebuilding the homebrew filesystem for LBA {info[1]}…")
            src = disc.rebuild(info[1])
            t = nxt.tracks[0]
            pad = max(0, t.sectors - t.image_sectors)
            nxt.tracks[0] = PlanTrack(t.mode, info[1], S.fit(src, max(src.count + pad, 300)), t.label, src.count)
            return True
        raise BurnError(
            f"The drive placed the next session at LBA {info[1]}, but this image's boot data is built for "
            f"LBA {nxt.start_lba}. Stopped before writing it; this disc can't be finished. "
            "(Unusual drive behaviour - please report the drive model.)")

    def verify(self, plan: BurnPlan, boot_hash: str | None) -> bool:
        dev = self.opts.dev
        self.progress(1.0, "Verifying…")
        self.events({"stage": "verify", "frac": 1.0, "part": 0.0})
        self.log("Verifying the burned disc…")
        for _ in range(60):
            try:
                if drive_status(dev) in (CDS_DISC_OK, CDS_NO_INFO):
                    break
            except OSError:
                pass
            time.sleep(1)
        lba, xa, toc, err = None, False, [], None
        for _ in range(10):
            try:
                lba, xa = last_session(dev)
                toc = read_toc(dev)
                break
            except OSError as e:
                err = e
                time.sleep(2)
        if lba is None:
            self.log(f"Verify: couldn't read the disc layout ({err}).")
            return False
        self.log("TOC: " + ", ".join(f"track {t if t != 0xAA else 'lead-out'} @ {at}{' data' if d else ''}"
                                     for t, at, d in toc))
        if lba != plan.boot.lba:
            self.log(f"Verify FAILED: last session starts at {lba}, expected {plan.boot.lba}.")
            return False
        if not xa:
            self.log("Verify note: the kernel doesn't report the last session as XA.")
        if not boot_hash:
            return True
        t = plan.boot
        vlen = self.verify_length(t)
        digest = None
        if block_size(dev) >= (t.lba + vlen) * 2048:
            digest = self._hash_blockdev(dev, t.lba, vlen)
        if digest is None:
            self.log("Reading the disc back through the drive directly…")
            digest = self._hash_readcd(dev, t.lba, vlen)
        if digest is None:
            self.log("Verify FAILED: the disc couldn't be read back.")
            return False
        if digest != boot_hash:
            self.log("Verify FAILED: the data read back differs from what was written.")
            return False
        self.log("Verify OK: boot session position and every data sector match.")
        return True

    def _hash_blockdev(self, dev: str, lba: int, count: int) -> str | None:
        h = hashlib.sha256()
        try:
            fd = os.open(dev, os.O_RDONLY)
        except OSError:
            return None
        try:
            pos, chunk = 0, 512
            while pos < count:
                n = min(chunk, count - pos)
                data = os.pread(fd, n * 2048, (lba + pos) * 2048)
                if len(data) != n * 2048:
                    self.log(f"Short read at sector {lba + pos}; trying another way.")
                    return None
                h.update(data)
                pos += n
                self.progress(1.0, f"Verifying… {100 * pos // max(1, count)}%")
                self.events({"stage": "verify", "frac": 1.0, "part": pos / max(1, count)})
        except OSError as e:
            self.log(f"Read error at sector {lba + pos} ({e}); trying another way.")
            return None
        finally:
            os.close(fd)
        return h.hexdigest()

    def _hash_readcd(self, dev: str, lba: int, count: int) -> str | None:
        tool = tools.find("readcd") or tools.find("readom")
        if not tool:
            return None
        h = hashlib.sha256()
        got = 0
        proc = subprocess.Popen([tool, f"dev={_addr(dev, 'readcd')}", f"sectors={lba}-{lba + count}", "f=-"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                env=tools.env({"LC_ALL": "C", "LANG": "C"}))
        assert proc.stdout
        try:
            while True:
                data = proc.stdout.read(1 << 20)
                if not data:
                    break
                h.update(data)
                got += len(data)
                self.events({"stage": "verify", "frac": 1.0, "part": got / max(1, count * 2048)})
            rc = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
        if rc != 0 or got != count * 2048:
            return None
        return h.hexdigest()
