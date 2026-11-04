"""IP.BIN (Dreamcast bootstrap / "Initial Program") parsing, generation and patching.

Generation reproduces KallistiOS' `makeip` using its copyright-free bootstrap
template (IP.TMPL by Jacob Alberty, BSD licence, see data/IP.TMPL.LICENSE).
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from importlib import resources

IP_SIZE = 0x8000

# (name, offset, length) as documented by Marcus Comstedt / KOS makeip
FIELDS = [
    ("hardware_id", 0x00, 0x10),
    ("maker_id", 0x10, 0x10),
    ("device_info", 0x20, 0x10),
    ("area_symbols", 0x30, 0x08),
    ("peripherals", 0x38, 0x08),
    ("product_no", 0x40, 0x0A),
    ("version", 0x4A, 0x06),
    ("release_date", 0x50, 0x10),
    ("boot_filename", 0x60, 0x10),
    ("sw_maker", 0x70, 0x10),
    ("title", 0x80, 0x80),
]
_F = {name: (off, ln) for name, off, ln in FIELDS}

REGION_TEXT_OFFSET = 0x3700   # three 32-byte entries: Japan, USA, Europe
REGION_NAMES = {"J": "Japan/Asia", "U": "USA/Canada", "E": "Europe"}

VGA_BIT = 1 << 4
WINCE_BIT = 1 << 0


def template() -> bytes:
    data = resources.files("discforge").joinpath("data/IP.TMPL").read_bytes()
    if len(data) != IP_SIZE:
        raise RuntimeError("IP.TMPL is damaged")
    return data


def crc16(data: bytes) -> int:
    n = 0xFFFF
    for byte in data:
        n ^= byte << 8
        for _ in range(8):
            n = ((n << 1) ^ 0x1021) if n & 0x8000 else (n << 1)
    return n & 0xFFFF


@dataclass
class IpInfo:
    raw: bytes
    hardware_id: str
    maker_id: str
    device_info: str
    area_symbols: str
    peripherals: str
    product_no: str
    version: str
    release_date: str
    boot_filename: str
    sw_maker: str
    title: str

    @property
    def valid(self) -> bool:
        return self.hardware_id.startswith("SEGA SEGAKATANA") and self.maker_id.startswith("SEGA ENTERPRISES")

    @property
    def regions(self) -> str:
        a = self.area_symbols.ljust(3)
        return "".join(c for i, c in enumerate("JUE") if a[i] == c)

    @property
    def peripheral_bits(self) -> int | None:
        try:
            return int(self.peripherals.strip()[:7], 16)
        except ValueError:
            return None

    @property
    def vga(self) -> bool:
        bits = self.peripheral_bits
        return bool(bits is not None and bits & VGA_BIT)

    @property
    def wince(self) -> bool:
        bits = self.peripheral_bits
        return bool(bits is not None and bits & WINCE_BIT)

    @property
    def media(self) -> str:
        # "XXXX GD-ROM1/1" / "CD-ROM1/1" / "MIL CD"
        m = re.search(r"(GD-ROM|CD-ROM|MIL CD)\s*(\d+/\d+)?", self.device_info)
        return f"{m.group(1)} {m.group(2) or ''}".strip() if m else self.device_info.strip()

    @property
    def crc_ok(self) -> bool:
        want = f"{crc16(self.raw[0x40:0x50]):04X}"
        return self.device_info[:4].upper() == want

    def region_text_ok(self, region: str) -> bool:
        i = "JUE".index(region)
        entry = self.raw[REGION_TEXT_OFFSET + 32 * i:REGION_TEXT_OFFSET + 32 * (i + 1)]
        return entry[4:].strip(b" \x00") != b""


def parse(raw: bytes) -> IpInfo:
    if len(raw) < 0x100:
        raise ValueError("IP.BIN too short")
    vals = {name: raw[off:off + ln].decode("latin-1").rstrip(" \x00") for name, off, ln in FIELDS}
    return IpInfo(raw=bytes(raw[:IP_SIZE]), **vals)


def _put(ip: bytearray, name: str, value: str) -> None:
    off, ln = _F[name]
    b = value.encode("ascii", "replace")
    if len(b) > ln:
        b = b[:ln]
    ip[off:off + ln] = b.ljust(ln, b" ")


def update_crc(ip: bytearray) -> None:
    ip[0x20:0x24] = f"{crc16(bytes(ip[0x40:0x50])):04X}".encode()


def build(title: str = "HOMEBREW", *, regions: str = "JUE", vga: bool = True,
          product_no: str = "T0000", version: str = "V1.000", release_date: str | None = None,
          boot_filename: str = "1ST_READ.BIN", sw_maker: str = "HOMEBREW",
          peripherals: int = 0xE000F10, disc: str = "1/1") -> bytes:
    """Build an IP.BIN exactly like KOS makeip does with the equivalent ip.txt."""
    ip = bytearray(template())
    if release_date is None:
        release_date = datetime.date.today().strftime("%Y%m%d")
    if not re.fullmatch(r"V\d\.\d{3}", version):
        raise ValueError("version must look like V1.000")
    if not re.fullmatch(r"\d{8}", release_date):
        raise ValueError("release date must be YYYYMMDD")
    bits = (peripherals | VGA_BIT) if vga else (peripherals & ~VGA_BIT)
    bits &= ~WINCE_BIT
    area = "".join(c if c in regions.upper() else " " for c in "JUE")
    _put(ip, "hardware_id", "SEGA SEGAKATANA")
    _put(ip, "maker_id", "SEGA ENTERPRISES")
    _put(ip, "device_info", f"0000 CD-ROM{disc}")
    _put(ip, "area_symbols", area)
    _put(ip, "peripherals", f"{bits:07X}")
    _put(ip, "product_no", product_no)
    _put(ip, "version", version)
    _put(ip, "release_date", release_date)
    _put(ip, "boot_filename", boot_filename)
    _put(ip, "sw_maker", sw_maker)
    _put(ip, "title", title)
    update_crc(ip)
    return bytes(ip)


def patch(raw: bytes, *, region_free: bool = False, vga: bool = False) -> bytes:
    """Region-free and/or VGA patch of an existing IP.BIN (works on commercial ones too)."""
    ip = bytearray(raw[:IP_SIZE])
    if region_free:
        ip[0x30:0x38] = b"JUE     "
        tmpl = template()
        ip[REGION_TEXT_OFFSET:REGION_TEXT_OFFSET + 96] = tmpl[REGION_TEXT_OFFSET:REGION_TEXT_OFFSET + 96]
    if vga:
        text = ip[0x38:0x3F].decode("latin-1")
        try:
            bits = int(text, 16)
        except ValueError:
            bits = 0xE000F10
        ip[0x38:0x3F] = f"{bits | VGA_BIT:07X}".encode()
    return bytes(ip)


def clean_title(name: str) -> str:
    """Make a reasonable IP.BIN title / ISO volume label from a file name."""
    name = re.sub(r"\.(cdi|gdi|iso|elf|bin|zip|7z)$", "", name, flags=re.I)
    name = re.sub(r"[_\.]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return (name or "HOMEBREW").upper()[:64]
