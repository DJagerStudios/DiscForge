"""Every system DiscForge can burn for: names, colours, disc type, speeds and honest notes."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Platform:
    id: str
    name: str                 # "PlayStation 2"
    short: str                # "PS2"
    maker: str
    year: int
    accent: str               # main colour (UI glow, covers)
    accent2: str              # second colour for gradients
    media: str                # "CD-R" or "DVD-R"
    speed: int                # preferred (slowest sensible) burn speed, in x of the media type
    needs: str                # what the console needs to play a burned disc
    tips: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_dvd(self) -> bool:
        return self.media.startswith("DVD")


_P = [
    Platform("dreamcast", "Dreamcast", "DC", "Sega", 1998, "#FF7A2F", "#FFB347", "CD-R", 8,
             "Boots without a mod on most Dreamcasts. Consoles built from mid-2000 on may refuse burned "
             "discs, and the last models (VA2) don't boot them at all.",
             ("Insert the disc and power on - it boots like a normal game.",
              "If it won't start, burn another copy at a slower speed or on a different brand of CD-R.")),
    Platform("ps1", "PlayStation", "PS1", "Sony", 1994, "#F0505A", "#FF9A6B", "CD-R", 4,
             "Needs a PlayStation with a modchip, or a soft-mod such as tonyhax International or UniROM. "
             "Burned discs don't start on an unmodified console.",
             ("PlayStation consoles can't read CD-RW - use CD-R.",
              "Early PS2s (SCPH-10000 to 39004) can't read some games from 80-minute CD-Rs; use 74-minute "
              "discs for those.",
              "Older lasers like slow burns and quality discs.")),
    Platform("ps2", "PlayStation 2", "PS2", "Sony", 2000, "#3D7BFF", "#7FB2FF", "DVD-R", 4,
             "Needs a PS2 that plays burned discs: a modchip, or MechaPwn (SCPH-5xxxx and newer models).",
             ("Quality DVD-R or DVD+R media (Verbatim) reads best on PS2 lasers.",
              "Dual-layer games need DVD+R DL discs.")),
    Platform("gamecube", "GameCube", "GC", "Nintendo", 2001, "#8C6CFF", "#C3A6FF", "DVD-R", 4,
             "Needs a drive chip (for example XenoGC or a Qoob). The lid only fits 8 cm mini DVD-R; full-size "
             "discs need an open or replacement lid.",
             ("DVD-R works best; some drive chips can't read DVD+R.",
              "A whole GameCube game fits on an 8 cm mini DVD-R.")),
    Platform("wii", "Wii", "Wii", "Nintendo", 2006, "#2EC5FF", "#9BE7FF", "DVD-R", 4,
             "Needs a soft-modded Wii (Homebrew Channel + cIOS) with a disc loader such as USB Loader GX, and "
             "an older drive that reads DVD-R. Wiis made from late 2009 (D3-2 drives) can't read burned discs.",
             ("Start the game from your disc loader's disc channel.",
              "Dual-layer games (Smash Bros. Brawl, Metroid Prime Trilogy...) need DVD+R DL discs.")),
    Platform("xbox", "Xbox", "Xbox", "Microsoft", 2001, "#5BD22F", "#B6F27A", "DVD-R", 4,
             "Needs a soft-modded or modchipped original Xbox. DVD-R reads on almost every Xbox drive; DVD+R "
             "needs the DVD-ROM book type on Samsung drives (DiscForge sets it when the drive allows).",
             ("Start the game from your dashboard (for example XBMC4Gamers or UnleashX).",
              "Quality DVD-R discs at a slow speed read best.")),
    Platform("saturn", "Sega Saturn", "Saturn", "Sega", 1994, "#FFC23D", "#FFE08A", "CD-R", 4,
             "Needs a modchip, a Pseudo Saturn Kai cartridge, or the disc-swap trick.",
             ("Saturn lasers prefer slow burns on quality CD-R.",)),
    Platform("segacd", "Sega CD / Mega-CD", "Sega CD", "Sega", 1991, "#FF5FA2", "#FFA3CB", "CD-R", 4,
             "Boots on a standard Sega CD / Mega-CD of the same region - no mod needed.",
             ()),
    Platform("pce", "PC Engine CD", "PCE CD", "NEC", 1988, "#19D3B5", "#86F0DE", "CD-R", 4,
             "Boots on a standard PC Engine CD / TurboGrafx-CD with the right System Card. Games find their data "
             "through the disc's table of contents, which DiscForge writes exactly as the image describes.",
             ()),
    Platform("pcfx", "PC-FX", "PC-FX", "NEC", 1994, "#E2E5EA", "#FFFFFF", "CD-R", 4,
             "The PC-FX has no copy protection, so burned discs boot as they are.",
             ()),
    Platform("neogeocd", "Neo Geo CD", "NGCD", "SNK", 1994, "#E8C547", "#FFE58A", "CD-R", 4,
             "Front- and top-loading Neo Geo CDs boot burned discs without a mod.",
             ()),
    Platform("cd32", "Amiga CD32", "CD32", "Commodore", 1993, "#E5484D", "#FF9089", "CD-R", 4,
             "The CD32 boots burned discs without a mod.",
             ()),
    Platform("cdi", "Philips CD-i", "CD-i", "Philips", 1991, "#2FB5A8", "#8FE3D9", "CD-R", 4,
             "CD-i players have no copy protection and read burned CD-R discs (not CD-RW).",
             ("Burn at a slow speed; some console-style players are picky.",)),
    Platform("3do", "3DO", "3DO", "The 3DO Company", 1993, "#E9E4C9", "#FFFFFF", "CD-R", 4,
             "Boots on a standard 3DO - no mod needed.",
             ()),
    Platform("pc", "PC / data disc", "PC", "", 1990, "#9FB3C8", "#DCE6F0", "CD-R", 16,
             "Opens on any computer. Great for homebrew collections, software and backups.",
             ()),
    Platform("cd", "CD image", "CD", "", 1990, "#9FB3C8", "#DCE6F0", "CD-R", 8,
             "Burned exactly as the image describes, track by track.",
             ()),
]

PLATFORMS: dict[str, Platform] = {p.id: p for p in _P}
CONSOLES = [p for p in _P if p.id not in ("pc", "cd")]
# Consoles that can't read CD-RW: their drives predate MultiRead (1997) and a CD-RW reflects too
# little light for them; the Dreamcast's GD-ROM drive and PS2 drives don't read them either.
NO_CDRW = {"ps1", "ps2", "cdi", "saturn", "dreamcast", "segacd", "3do", "pce", "pcfx", "neogeocd", "cd32"}


def get(pid: str) -> Platform:
    return PLATFORMS.get(pid, PLATFORMS["cd"])


# Disc capacities in 2048-byte sectors (smallest common size of each kind)
CD74 = 333000
CD80 = 359849
DVD5 = 2295104            # DVD+R 4.7 GB
DVDR_SL = 2298496         # DVD-R 4.7 GB (a little bigger than DVD+R)
DVD9 = 4171712            # DVD+R DL 8.5 GB (DVD-R DL is 4173824)
MINI_DVD = 712880         # 8 cm DVD-R, 1.46 GB
GAMECUBE_SIZE = 1459978240
WII_SL_SIZE = 4699979776
WII_DL_SIZE = 8511160320


def media_for(sectors: int, kind: str) -> str:
    """'cd', 'dvd' or 'dvd-dl' for an image of `sectors` 2048-byte sectors (CD frames for CDs)."""
    if kind == "cd":
        return "cd"
    return "dvd" if sectors <= DVDR_SL else "dvd-dl"


def media_label(media: str) -> str:
    return {"cd": "CD-R", "dvd": "DVD-R / DVD+R", "dvd-dl": "DVD+R DL (dual layer)"}.get(media, media)
