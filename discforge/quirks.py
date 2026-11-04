"""Per-game and per-console exceptions that decide whether a burned copy works.

Sources: alex-free's Tonyhax International anti-piracy table (PS1 EDC / anti-modchip titles),
Redump DVD-9 layer breaks, growisofs 7.1's layer-break rules, GC-Forever (audio streaming),
XboxDevWiki and extract-xiso (media patch).
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from importlib import resources

DVDPLUS_DL_L0_MAX = 2_086_912          # largest first layer a DVD+R DL can take
WII_DL_SECTORS = 4_155_840             # every dual-layer Wii disc
WII_DL_BREAK = 2_084_960               # ...and its layer break (Brawl, Metroid Prime Trilogy, Xenoblade ...)


def title_key(title: str) -> str:
    t = unicodedata.normalize("NFKD", title)
    t = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", t)
    return re.sub(r"[^a-z0-9]+", "", t.lower())


@lru_cache(maxsize=1)
def _ps1() -> dict:
    with resources.files("discforge").joinpath("data/quirks/ps1_protection.json").open("rb") as f:
        return json.load(f)["games"]


def ps1_protection(title: str) -> list[str]:
    """Protection types of a PlayStation game ('EDC', 'APv1', 'APv2', 'LibCrypt', 'PAL BIOS', 'Key Disc')."""
    e = _ps1().get(title_key(title))
    return list(e["p"]) if e else []


def ps1_notes(title: str) -> tuple[bool, list[str]]:
    """(must be written raw, warnings) for a PlayStation game."""
    tags = ps1_protection(title)
    warns = []
    raw_only = "EDC" in tags
    if raw_only:
        warns.append("This game checks for untouched error-correction data, so it only works when the burner "
                     "writes raw sectors — DiscForge won't fall back to any other mode for it.")
    ap = [t for t in tags if t.startswith("APv")]
    if ap:
        warns.append(f"This game has anti-modchip protection ({'/'.join(ap)}): it refuses to start on consoles "
                     "with an older (non-stealth) modchip or a soft-mod. Stealth modchips are fine, and "
                     "tonyhax International can bypass it.")
    if "Key Disc" in tags:
        warns.append("This disc works together with a 'key' disc (an add-on or append disc).")
    return raw_only, warns


def ps2_layer_problem(l0: int, total: int) -> str | None:
    """Why a PS2 DVD-9 with this layer break can't be burned to work, or None."""
    l1 = total - l0
    if l1 > l0:
        return (f"This game's second layer is bigger than its first ({l1:,} vs {l0:,} sectors). Recordable "
                "dual-layer discs always need the first layer to be the bigger one, so a burned copy can't "
                "match the original layout and won't work.")
    if l0 > DVDPLUS_DL_L0_MAX:
        return (f"This game's first layer ({l0:,} sectors) is bigger than a DVD+R DL's first layer "
                f"({DVDPLUS_DL_L0_MAX:,}), so it can't be burned with the right layer break.")
    if l0 % 16:
        return "This game's layer break isn't on a 16-sector boundary, which recordable discs can't reproduce."
    return None


XBE_MEDIA_PATTERN = bytes.fromhex("E8CAFDFFFF85C07D")      # extract-xiso's "media patch"


def xbe_media_patch(data: bytearray) -> int:
    """Patch an XBE so it runs from burned (non-original) media; returns how many places changed."""
    n, i = 0, 0
    while True:
        i = data.find(XBE_MEDIA_PATTERN, i)
        if i < 0:
            return n
        data[i + len(XBE_MEDIA_PATTERN) - 1] = 0xEB         # jge -> jmp
        n += 1
        i += len(XBE_MEDIA_PATTERN)
