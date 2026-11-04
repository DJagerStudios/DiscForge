"""ELF -> flat binary (what `sh-elf-objcopy -O binary` does for KOS programs)."""
from __future__ import annotations

import struct

from .util import DcError

EM_SH = 42
PT_LOAD = 1
SHF_ALLOC = 0x2
SHT_NOBITS = 8
LOAD_ADDR = 0x8C010000


class ElfError(DcError):
    pass


def is_elf(data: bytes) -> bool:
    return data[:4] == b"\x7fELF"


def elf_to_bin(data: bytes) -> tuple[bytes, int, int]:
    """Return (binary, load address, entry point)."""
    if not is_elf(data):
        raise ElfError("not an ELF file")
    if data[4] != 1:
        raise ElfError("only 32-bit ELF files are supported")
    end = "<" if data[5] == 1 else ">"
    (e_type, e_machine, _ver, e_entry, e_phoff, e_shoff, _flags, _ehsize,
     e_phentsize, e_phnum, e_shentsize, e_shnum, _shstrndx) = struct.unpack_from(end + "HHIIIIIHHHHHH", data, 16)
    if e_machine != EM_SH:
        raise ElfError(f"ELF is not for the SuperH CPU (machine {e_machine}); not a Dreamcast program")
    if end != "<":
        raise ElfError("big-endian SH ELF: not a Dreamcast program")

    segs = []
    for i in range(e_phnum):
        p_type, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, _f, _a = struct.unpack_from(
            "<IIIIIIII", data, e_phoff + i * e_phentsize)
        if p_type == PT_LOAD:
            segs.append((p_offset, p_vaddr, p_paddr, p_filesz))

    # Prefer sections (exactly like objcopy); fall back to segments for stripped files.
    pieces: list[tuple[int, bytes]] = []
    if e_shnum and e_shoff:
        for i in range(e_shnum):
            (_n, sh_type, sh_flags, sh_addr, sh_offset, sh_size, *_rest) = struct.unpack_from(
                "<IIIIIIIIII", data, e_shoff + i * e_shentsize)
            if not (sh_flags & SHF_ALLOC) or sh_type == SHT_NOBITS or sh_size == 0:
                continue
            lma = sh_addr
            for p_offset, _v, p_paddr, p_filesz in segs:
                if p_offset <= sh_offset < p_offset + max(p_filesz, 1):
                    lma = p_paddr + (sh_offset - p_offset)
                    break
            pieces.append((lma, data[sh_offset:sh_offset + sh_size]))
    if not pieces:
        pieces = [(p_paddr, data[p_offset:p_offset + p_filesz]) for p_offset, _v, p_paddr, p_filesz in segs if p_filesz]
    if not pieces:
        raise ElfError("ELF has nothing to load")

    base = min(a for a, _ in pieces)
    top = max(a + len(b) for a, b in pieces)
    if top - base > 16 << 20:
        raise ElfError("ELF image is larger than Dreamcast RAM (16 MB)")
    out = bytearray(top - base)
    for a, b in pieces:
        out[a - base:a - base + len(b)] = b
    return bytes(out), base, e_entry


def check_load_address(base: int, entry: int) -> list[str]:
    """Warnings if the program won't run as 1ST_READ.BIN (loaded/jumped at 0x8C010000)."""
    problems = []
    if base & 0x1FFFFFFF != LOAD_ADDR & 0x1FFFFFFF:
        problems.append(f"program is linked at 0x{base:08X}, but 1ST_READ.BIN is loaded at 0x8C010000")
    if entry & 0x1FFFFFFF != LOAD_ADDR & 0x1FFFFFFF:
        problems.append(f"entry point is 0x{entry:08X}, but the boot ROM jumps to 0x8C010000")
    return problems
