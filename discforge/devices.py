"""Optical drives on every system: finding them, what they can write, and how each burning tool
addresses them.

    for d in list_drives(): print(d.name, d.dev, d.writes)

Linux reads sysfs and asks the drive itself (MMC GET CONFIGURATION through SG_IO, which is allowed
on read-only opens).  FreeBSD uses camcontrol, macOS drutil/IOKit names, Windows the drive letters
and IOCTL_STORAGE_QUERY_PROPERTY; everything falls back to `cdrecord -scanbus`.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

from . import tools

SYSTEM = ("windows" if sys.platform.startswith("win") else "macos" if sys.platform == "darwin"
          else "freebsd" if sys.platform.startswith("freebsd") else "linux" if sys.platform.startswith("linux")
          else sys.platform)

# MMC feature codes -> what the drive writes
WRITE_FEATURES = {0x002D: "CD-R", 0x002E: "CD-R", 0x002F: "DVD-R", 0x002B: "DVD+R", 0x003B: "DVD+R DL",
                  0x0040: "BD", 0x0041: "BD-R"}
PROFILES = {0x08: "CD-ROM", 0x09: "CD-R", 0x0A: "CD-RW", 0x10: "DVD-ROM", 0x11: "DVD-R", 0x12: "DVD-RAM",
            0x13: "DVD-RW", 0x14: "DVD-RW", 0x15: "DVD-R DL", 0x16: "DVD-R DL", 0x1A: "DVD+RW", 0x1B: "DVD+R",
            0x2A: "DVD+RW DL", 0x2B: "DVD+R DL", 0x40: "BD-ROM", 0x41: "BD-R", 0x42: "BD-R", 0x43: "BD-RE"}


@dataclass
class OpticalDrive:
    dev: str                                   # /dev/sr0, /dev/cd0, E:, IODVDServices/0 ...
    name: str                                  # "ASUS DRW-24D5MT"
    accessible: bool = True
    writes: tuple[str, ...] = ()               # media it can burn, e.g. ("CD-R", "DVD-R", "DVD+R", "DVD+R DL")
    reads_dvd: bool | None = None
    group: str = ""                            # Linux/FreeBSD: group that owns the device node
    addr: dict = field(default_factory=dict)   # per-tool address overrides, e.g. {"cdrecord": "1,0,0"}

    @property
    def can_write_cd(self) -> bool | None:
        return None if not self.writes else "CD-R" in self.writes

    @property
    def can_write_dvd(self) -> bool | None:
        return None if not self.writes else any(w.startswith("DVD") for w in self.writes)

    @property
    def can_write_dl(self) -> bool | None:
        return None if not self.writes else any(w.endswith("DL") for w in self.writes)

    @property
    def label(self) -> str:
        return f"{self.name or 'Optical drive'} ({self.dev})"

    def address(self, tool: str) -> str:
        """How `tool` (cdrecord, readcd, cdrdao, growisofs) names this drive."""
        return self.addr.get(tool, self.dev)


def _read(path: str) -> str:
    try:
        with open(path, errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _group(path: str) -> str:
    try:
        import grp
        return grp.getgrgid(os.stat(path).st_gid).gr_name
    except (OSError, KeyError, ImportError):
        return ""


# --------------------------------------------------------------------------
# Linux

def _linux_features(dev: str) -> tuple[set[str], bool | None]:
    """Writable media from GET CONFIGURATION (all features)."""
    try:
        from .engine import scsi_in
    except ImportError:
        return set(), None
    try:
        data = scsi_in(dev, bytes([0x46, 0x00, 0, 0, 0, 0, 0, 0xFF, 0xF8, 0]), 0xFFF8)
    except OSError:
        return set(), None
    if len(data) < 8:
        return set(), None
    end = min(len(data), 4 + int.from_bytes(data[0:4], "big"))
    pos = 8
    writes: set[str] = set()
    profiles: set[int] = set()
    while pos + 4 <= end:
        code = int.from_bytes(data[pos:pos + 2], "big")
        length = data[pos + 3]
        body = data[pos + 4:pos + 4 + length]
        if code == 0x0000:
            for k in range(0, len(body) - 3, 4):
                profiles.add(int.from_bytes(body[k:k + 2], "big"))
        elif code in (0x002D, 0x002E):
            writes.add("CD-R")
            if 0x0A in profiles:
                writes.add("CD-RW")
        elif code == 0x002F:
            writes.add("DVD-R")
            if body and body[0] & 0x08:                 # RDL: DVD-R dual layer recording
                writes.add("DVD-R DL")
            if body and body[0] & 0x02:                 # DVD-RW
                writes.add("DVD-RW")
        elif code == 0x002B and body and body[0] & 0x01:
            writes.add("DVD+R")
        elif code == 0x002A and body and body[0] & 0x01:
            writes.add("DVD+RW")
        elif code == 0x003B and body and body[0] & 0x01:
            writes.add("DVD+R DL")
        elif code == 0x0041:
            writes.add("BD-R")
        pos += 4 + length
    return writes, bool(profiles & {0x10, 0x11, 0x13, 0x14, 0x15, 0x1A, 0x1B, 0x2B}) or None


def _linux() -> list[OpticalDrive]:
    caps: dict[str, dict[str, bool]] = {}
    info = _read("/proc/sys/dev/cdrom/info")
    names: list[str] = []
    for line in info.splitlines():
        key, _, vals = line.partition(":")
        vals_l = vals.split()
        if key.strip() == "drive name":
            names = vals_l
            for n in names:
                caps.setdefault(n, {})
        elif key.strip().startswith(("Can write", "Can read")) and names:
            for n, v in zip(names, vals_l):
                caps[n][key.strip()] = v == "1"
    out = []
    for sys_path in sorted(glob.glob("/sys/class/block/sr*")):
        name = os.path.basename(sys_path)
        dev = f"/dev/{name}"
        label = " ".join(f"{_read(sys_path + '/device/vendor')} {_read(sys_path + '/device/model')}".split())
        c = caps.get(name, {})
        writes, reads_dvd = _linux_features(dev) if os.access(dev, os.R_OK) else (set(), None)
        if not writes:
            if c.get("Can write CD-R"):
                writes.add("CD-R")
            if c.get("Can write CD-RW"):
                writes.add("CD-RW")
            if c.get("Can write DVD-R"):
                writes |= {"DVD-R", "DVD+R"}
        if reads_dvd is None and "Can read DVD" in c:
            reads_dvd = c["Can read DVD"]
        out.append(OpticalDrive(dev, label or "Optical drive", os.access(dev, os.R_OK | os.W_OK),
                                tuple(sorted(writes, key=_order)), reads_dvd, _group(dev)))
    return out


ORDER = ["CD-R", "CD-RW", "DVD-R", "DVD-RW", "DVD+R", "DVD+RW", "DVD-R DL", "DVD+R DL", "BD-R"]


def _order(kind: str) -> int:
    return ORDER.index(kind) if kind in ORDER else 99


# --------------------------------------------------------------------------
# FreeBSD

def _freebsd() -> list[OpticalDrive]:
    exe = shutil.which("camcontrol")
    out = []
    if exe:
        try:
            r = subprocess.run([exe, "devlist"], capture_output=True, text=True, timeout=20)
            text = r.stdout
        except (OSError, subprocess.TimeoutExpired):
            text = ""
        for line in text.splitlines():
            m = re.match(r"<(.*?)>\s+at\s+scbus(\d+)\s+target\s+(\d+)\s+lun\s+(\d+)\s+\(([^)]*)\)", line.strip())
            if not m:
                continue
            periph = m.group(5).split(",")
            cd = next((p for p in periph if re.fullmatch(r"cd\d+", p)), None)
            if not cd:
                continue
            words = m.group(1).split()
            name = " ".join(words[:-1]) if len(words) > 1 else m.group(1)
            btl = f"{m.group(2)},{m.group(3)},{m.group(4)}"
            dev = f"/dev/{cd}"
            # cdrecord/readcd address CAM devices as bus,target,lun; cdrdao and growisofs take /dev/cdN
            out.append(OpticalDrive(dev, name, os.access(dev, os.R_OK | os.W_OK), group=_group(dev),
                                    addr={"cdrecord": btl, "readcd": btl, "cdrdao": dev, "growisofs": dev}))
    if not out:
        out = _scanbus()
    return out


# --------------------------------------------------------------------------
# macOS

def _macos() -> list[OpticalDrive]:
    exe = shutil.which("drutil")
    out = []
    if exe:
        try:
            r = subprocess.run([exe, "list"], capture_output=True, text=True, timeout=20)
            text = r.stdout
        except (OSError, subprocess.TimeoutExpired):
            text = ""
        for line in text.splitlines():
            m = re.match(r"\s*(\d+)\s+(.+?)\s{2,}(.+?)\s{2,}(\S+)", line)
            if not m or not m.group(1).isdigit():
                continue
            n = int(m.group(1)) - 1
            name = f"{m.group(2).strip()} {m.group(3).strip()}"
            svc = f"IODVDServices/{n}" if n else "IODVDServices"
            # cdrdao on macOS picks the (1+b+t+l)th burner from a bus,target,lun triple
            out.append(OpticalDrive(svc, name, True,
                                    addr={"cdrecord": svc, "readcd": svc, "cdrdao": f"0,0,{n}", "drutil": str(n + 1)}))
    return out or _scanbus()


# --------------------------------------------------------------------------
# Windows

def _windows() -> list[OpticalDrive]:
    out = []
    try:
        import ctypes
        k32 = ctypes.windll.kernel32                     # type: ignore[attr-defined]
        mask = k32.GetLogicalDrives()
        for i in range(26):
            if not mask & (1 << i):
                continue
            letter = f"{chr(65 + i)}:"
            if k32.GetDriveTypeW(letter + "\\") != 5:     # DRIVE_CDROM
                continue
            # cdrdao takes the drive letter; cdrecord/readcd need the SPTI bus,target,lun (matched below)
            out.append(OpticalDrive(letter, _windows_name(letter) or f"Drive {letter}", True,
                                    addr={"cdrdao": letter[0]}))
    except (OSError, AttributeError):
        pass
    if out:
        units = _scanbus()
        for d in out:
            match = [u for u in units if u.name and u.name.lower() in d.name.lower() or
                     d.name.lower() in (u.name or "").lower()]
            if not match and len(out) == 1 and len(units) == 1:
                match = units
            if match:
                d.addr.update(cdrecord=match[0].dev, readcd=match[0].dev)
    return out or _scanbus()


def _windows_name(letter: str) -> str:
    """Vendor + product from IOCTL_STORAGE_QUERY_PROPERTY (works without admin rights)."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32                     # type: ignore[attr-defined]
        h = k32.CreateFileW(f"\\\\.\\{letter}", 0, 3, None, 3, 0, None)
        if h in (-1, 0, None) or h == wintypes.HANDLE(-1).value:
            return ""
        try:
            query = (ctypes.c_ubyte * 12)()              # StorageDeviceProperty, PropertyStandardQuery
            buf = (ctypes.c_ubyte * 1024)()
            got = wintypes.DWORD()
            if not k32.DeviceIoControl(h, 0x2D1400, query, 12, buf, 1024, ctypes.byref(got), None):
                return ""
            raw = bytes(buf[:got.value])

            def at(off_pos: int) -> str:
                off = int.from_bytes(raw[off_pos:off_pos + 4], "little")
                if not off or off >= len(raw):
                    return ""
                return raw[off:raw.index(b"\0", off)].decode("latin-1").strip()

            return " ".join(x for x in (at(12), at(16)) if x)
        finally:
            k32.CloseHandle(h)
    except (OSError, AttributeError, ValueError):
        return ""


# --------------------------------------------------------------------------

def _scanbus() -> list[OpticalDrive]:
    """Last resort on any system: cdrecord -scanbus lists CD/DVD units with their bus addresses."""
    exe = tools.find("cdrecord")
    if not exe:
        return []
    try:
        r = subprocess.run([exe, "-scanbus"], capture_output=True, text=True, timeout=60, errors="replace",
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return []
    out = []
    for line in r.stdout.splitlines():
        m = re.match(r"\s*(\d+,\d+,\d+)\s+\d+\)\s+'([^']*)'\s+'([^']*)'\s+'([^']*)'\s+(.*)", line)
        if m and ("CD-ROM" in m.group(5) or "Removable" in m.group(5)):
            btl = m.group(1)
            out.append(OpticalDrive(btl, f"{m.group(2).strip()} {m.group(3).strip()}".strip(), True,
                                    addr={"cdrecord": btl, "readcd": btl, "cdrdao": btl}))
    return out


_known: dict[str, OpticalDrive] = {}


def list_drives() -> list[OpticalDrive]:
    try:
        if SYSTEM == "linux":
            found = _linux()
        elif SYSTEM == "freebsd":
            found = _freebsd()
        elif SYSTEM == "macos":
            found = _macos()
        elif SYSTEM == "windows":
            found = _windows()
        else:
            found = _scanbus()
    except Exception:          # noqa: BLE001 - never let drive discovery take the app down
        found = []
    for d in found:
        _known[d.dev] = d
    return found


def address(dev: str, tool: str) -> str:
    """The name `tool` uses for the drive the app calls `dev`."""
    d = _known.get(dev)
    return d.address(tool) if d else dev


def _quiet(args: list[str]) -> bool:
    try:
        return subprocess.run(args, capture_output=True, timeout=60, stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _win_ioctl(dev: str, code: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32                     # type: ignore[attr-defined]
        h = k32.CreateFileW(f"\\\\.\\{dev.rstrip(chr(92))}", 0x80000000, 3, None, 3, 0, None)
        if h in (-1, 0, None):
            return False
        try:
            got = wintypes.DWORD()
            return bool(k32.DeviceIoControl(h, code, None, 0, None, 0, ctypes.byref(got), None))
        finally:
            k32.CloseHandle(h)
    except (OSError, AttributeError):
        return False


def eject(dev: str) -> bool:
    """Open the tray (non-Linux; Linux uses the ioctl in burner.eject)."""
    d = _known.get(dev)
    if SYSTEM == "windows":
        if _win_ioctl(dev, 0x2D4808):                   # IOCTL_STORAGE_EJECT_MEDIA
            return True
    elif SYSTEM == "macos" and shutil.which("drutil"):
        n = d.addr.get("drutil") if d else None
        if _quiet(["drutil", "eject"] + (["-drive", n] if n else [])):
            return True
    elif SYSTEM == "freebsd" and shutil.which("cdcontrol"):
        if _quiet(["cdcontrol", "-f", dev, "eject"]):
            return True
    exe = tools.find("cdrecord")
    return bool(exe) and _quiet([exe, f"dev={address(dev, 'cdrecord')}", "-eject"])


def close_tray(dev: str) -> bool:
    d = _known.get(dev)
    if SYSTEM == "windows":
        if _win_ioctl(dev, 0x2D480C):                   # IOCTL_STORAGE_LOAD_MEDIA
            return True
    elif SYSTEM == "macos" and shutil.which("drutil"):
        n = d.addr.get("drutil") if d else None
        if _quiet(["drutil", "tray", "close"] + (["-drive", n] if n else [])):
            return True
    elif SYSTEM == "freebsd" and shutil.which("cdcontrol"):
        if _quiet(["cdcontrol", "-f", dev, "close"]):
            return True
    exe = tools.find("cdrecord")
    return bool(exe) and _quiet([exe, f"dev={address(dev, 'cdrecord')}", "-load"])


def prepare_for_burn(dev: str, log=print) -> None:
    """macOS: unmount the disc so the burning tool gets exclusive access to the drive
    (DiskArbitration otherwise holds it)."""
    if SYSTEM != "macos" or not shutil.which("drutil"):
        return
    d = _known.get(dev)
    n = d.addr.get("drutil") if d else None
    try:
        r = subprocess.run(["drutil", "status"] + (["-drive", n] if n else []), capture_output=True, text=True,
                           timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return
    m = re.search(r"(/dev/disk\d+)", r.stdout)
    if m and shutil.which("diskutil"):
        if _quiet(["diskutil", "unmountDisk", m.group(1)]):
            log(f"Unmounted {m.group(1)} so the burner can use the drive.")


def fix_permission_command(drive: OpticalDrive) -> list[str] | None:
    """The command that gives this user access to the drive (run through pkexec/sudo), if any."""
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if SYSTEM in ("linux", "freebsd") and drive.group and drive.group != "root" and user:
        if SYSTEM == "freebsd":
            return ["pw", "groupmod", drive.group, "-m", user]
        return ["usermod", "-aG", drive.group, user]
    return None
