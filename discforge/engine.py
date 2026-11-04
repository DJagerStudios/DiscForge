"""Shared machinery for the cdrdao (CD) and growisofs (DVD) burners: media checks, running a
burning tool with live progress, cancelling, waiting for the disc, raw sector reads."""
from __future__ import annotations

import ctypes
import os
import sys
import re
import signal
import subprocess
import threading
import time
from typing import Callable

from . import burner as B
from .burner import BurnError, BurnOptions, Cancelled, Media
from .platforms import media_label
from . import tools


def media_ok(m: Media, need: str, sectors: int, platform: str = "", plus_dl_only: bool = False) -> str | None:
    """None when the inserted disc suits a job needing `need` (cd/dvd/dvd-dl) and `sectors`;
    otherwise a friendly reason."""
    from .platforms import DVD5, NO_CDRW, get
    label = media_label(need)
    if not m.present:
        return f"Insert a blank {label}."
    kind = m.kind.upper()
    is_cd, is_dvd = kind.startswith("CD"), kind.startswith("DVD")
    dl = "DL" in kind or "DOUBLE" in kind or "DUAL" in kind
    if kind.startswith("BD"):
        return f"That's a Blu-ray disc. This game goes on a {label}."
    if "RAM" in kind:
        return f"DVD-RAM discs don't work in game consoles. Insert a {label}."
    if need == "cd" and is_dvd:
        return "That's a DVD. This game goes on a CD-R."
    if need != "cd" and is_cd:
        return f"That's a CD. This game needs a {label}."
    if need == "dvd-dl" and is_dvd and not dl:
        return "This game is too big for a single-layer DVD. Insert a DVD+R DL (dual layer)."
    if platform in NO_CDRW and is_cd and m.erasable:
        return f"{get(platform).name} consoles can't read CD-RW discs. Insert a CD-R."
    if plus_dl_only and need == "dvd-dl" and kind.startswith("DVD-R"):
        return "This game needs a DVD+R DL: its layer break can't be set on DVD-R DL discs."
    if need == "dvd" and kind.startswith("DVD+R") and not dl and sectors > DVD5:
        return "This game is a little too big for a DVD+R. Use a DVD-R (they hold slightly more)."
    if not m.blank and not m.erasable:
        return "This disc has already been written. Insert a blank one."
    if m.blank and m.capacity and sectors and m.capacity < sectors:
        return f"The game doesn't fit on this disc ({sectors:,} sectors needed, {m.capacity:,} free)."
    return None


def wait_ready(dev: str, log: Callable[[str], None], seconds: int = 90) -> bool:
    for _ in range(seconds):
        try:
            if B.drive_status(dev) in (B.CDS_DISC_OK, B.CDS_NO_INFO):
                return True
        except OSError:
            pass
        time.sleep(1)
    log("The drive didn't report the disc as ready.")
    return False


class ToolBurner:
    """Base for the external-tool burners.  Thread-friendly: callbacks + cancel()."""

    stop_signal = signal.SIGINT

    def __init__(self, opts: BurnOptions, log: Callable[[str], None] = print,
                 events: Callable[[dict], None] | None = None):
        self.opts = opts
        self.log = log
        self.events = events or (lambda e: None)
        self._cancel = threading.Event()
        self._proc: subprocess.Popen | None = None
        self.writing = False
        self.written = False             # the write finished; a cancel now only stops the check
        self.speed: float | None = None

    def cancel(self) -> None:
        self._cancel.set()
        p = self._proc
        if p and p.poll() is None:
            try:
                if sys.platform.startswith("win"):
                    p.terminate()
                else:
                    p.send_signal(self.stop_signal)
            except OSError:
                pass

    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise Cancelled("Cancelled.")

    def run(self, args: list[str], on_line: Callable[[str], None], cwd: str | None = None) -> tuple[int, list[str]]:
        """Run a tool, feeding each output line (\\r or \\n separated) to on_line."""
        self.check_cancel()
        self.log("$ " + " ".join(args))
        env = tools.env({"LC_ALL": "C", "LANG": "C"})
        self._proc = proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, env=env, cwd=cwd)
        if self._cancel.is_set():          # a cancel that arrived while the tool was starting
            self.cancel()
        tail: list[str] = []
        buf = b""
        assert proc.stdout
        try:
            while True:
                ch = proc.stdout.read1(4096)
                if not ch:
                    break
                buf += ch
                parts = re.split(rb"[\r\n]", buf)
                buf = parts.pop()
                for p in parts:
                    line = p.decode("utf-8", "replace").rstrip()
                    if not line.strip():
                        continue
                    tail = (tail + [line])[-40:]
                    on_line(line)
            if buf.strip():
                line = buf.decode("utf-8", "replace").rstrip()
                tail.append(line)
                on_line(line)
            rc = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            self._proc = None
        return rc, tail

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check_cancel()
            time.sleep(0.2)


def erase(dev: str, kind: str, log: Callable[[str], None] = print) -> None:
    """Quick-erase a rewritable disc (CD-RW, DVD-RW, DVD+RW) so it can be burned again."""
    k = kind.upper()
    if k.startswith("CD"):
        from . import devices
        args = [tool("cdrecord", "cdrtools"), f"dev={devices.address(dev, 'cdrecord')}", "blank=fast", "gracetime=3"]
    elif k.startswith("DVD-RW"):
        from . import devices
        args = [tool("dvd+rw-format", "dvd+rw-tools"), "-blank", devices.address(dev, "growisofs")]
    elif k.startswith("DVD+RW"):
        from . import devices
        args = [tool("dvd+rw-format", "dvd+rw-tools"), "-force", devices.address(dev, "growisofs")]
    else:
        raise BurnError("Only rewritable discs (CD-RW, DVD-RW, DVD+RW) can be erased.")
    log("$ " + " ".join(args))
    try:
        r = subprocess.run(args, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL,
                           env=tools.env({"LC_ALL": "C"}), timeout=3600)
    except subprocess.TimeoutExpired:
        raise BurnError("Erasing took too long; the drive may be stuck. Take the disc out and try again.") from None
    out = (r.stdout + r.stderr).strip()
    if out:
        log(out[-2000:])
    if r.returncode != 0:
        raise BurnError("The drive couldn't erase this disc." + (f" ({out.splitlines()[-1]})" if out else ""))


def tool(name: str, package: str) -> str:
    path = tools.find(name)
    if not path:
        raise BurnError(f"{name} is not installed (sudo pacman -S {package})")
    return path


# --------------------------------------------------------------------------
# Raw CD sector reads through SG_IO (SCSI READ CD, allowed for read-only opens)

SG_IO = 0x2285
SG_DXFER_FROM_DEV = -3


class _SgIoHdr(ctypes.Structure):
    _fields_ = [("interface_id", ctypes.c_int), ("dxfer_direction", ctypes.c_int),
                ("cmd_len", ctypes.c_ubyte), ("mx_sb_len", ctypes.c_ubyte), ("iovec_count", ctypes.c_ushort),
                ("dxfer_len", ctypes.c_uint), ("dxferp", ctypes.c_void_p), ("cmdp", ctypes.c_void_p),
                ("sbp", ctypes.c_void_p), ("timeout", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("pack_id", ctypes.c_int), ("usr_ptr", ctypes.c_void_p), ("status", ctypes.c_ubyte),
                ("masked_status", ctypes.c_ubyte), ("msg_status", ctypes.c_ubyte), ("sb_len_wr", ctypes.c_ubyte),
                ("host_status", ctypes.c_ushort), ("driver_status", ctypes.c_ushort), ("resid", ctypes.c_int),
                ("duration", ctypes.c_uint), ("info", ctypes.c_uint)]


def _sg_in(fd: int, cdb: bytes, length: int, timeout_ms: int = 60000) -> bytes:
    """One SCSI command reading `length` bytes from the device (Linux SG_IO)."""
    if not sys.platform.startswith("linux"):
        raise OSError("SCSI pass-through is only used on Linux")
    import fcntl
    buf = ctypes.create_string_buffer(max(1, length))
    cmd = (ctypes.c_ubyte * len(cdb))(*cdb)
    sense = ctypes.create_string_buffer(32)
    hdr = _SgIoHdr(interface_id=ord("S"), dxfer_direction=SG_DXFER_FROM_DEV, cmd_len=len(cdb), mx_sb_len=32,
                   dxfer_len=length, dxferp=ctypes.addressof(buf), cmdp=ctypes.addressof(cmd),
                   sbp=ctypes.addressof(sense), timeout=timeout_ms)
    fcntl.ioctl(fd, SG_IO, hdr)
    if hdr.info & 1:
        key = sense.raw[2] & 0x0F if hdr.sb_len_wr > 2 else -1
        raise OSError(f"SCSI command 0x{cdb[0]:02X} failed (status {hdr.status}, sense key {key})")
    return buf.raw[:length - max(0, hdr.resid)]


def scsi_in(dev: str, cdb: bytes, length: int) -> bytes:
    fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
    try:
        return _sg_in(fd, cdb, length, 20000)
    finally:
        os.close(fd)


class RawCd:
    """READ CD: whole 2352-byte sectors (sync..ECC) or just the 2048-byte user data."""

    def __init__(self, dev: str):
        if not sys.platform.startswith("linux"):
            raise OSError("raw reads go through readcd on this system")
        self.fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)

    def read(self, lba: int, count: int, user_only: bool = False) -> bytes:
        size = 2048 if user_only else 2352
        cdb = bytes([0xBE, 0, (lba >> 24) & 255, (lba >> 16) & 255, (lba >> 8) & 255, lba & 255,
                     (count >> 16) & 255, (count >> 8) & 255, count & 255, 0x10 if user_only else 0xF8, 0, 0])
        try:
            return _sg_in(self.fd, cdb, count * size)
        except OSError as e:
            raise OSError(f"READ CD failed at sector {lba} ({e})") from None

    def close(self) -> None:
        os.close(self.fd)
