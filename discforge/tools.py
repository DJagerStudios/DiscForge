"""Finding the external programs DiscForge drives: the copies bundled with the app first
(PyInstaller bundle, AppImage, DISCFORGE_TOOLS), then the system's PATH."""
from __future__ import annotations

import os
import shutil
import sys
from functools import lru_cache

WINDOWS = sys.platform.startswith("win")
DVD_WITH_CDRECORD = WINDOWS or sys.platform == "darwin"     # no growisofs there: DVDs are written by cdrecord

# what a burn can't do without on this system, and what only some games need
REQUIRED = ("cdrecord", "readcd", "cdrdao") + (() if DVD_WITH_CDRECORD else ("growisofs",))
OPTIONAL = ("mkisofs",) + (() if DVD_WITH_CDRECORD else ("dvd+rw-mediainfo",)) + ("chdman", "7z", "dolphin-tool")
ALIASES = {"7z": ("7z", "7zz", "7za"), "readcd": ("readcd", "readom")}

# what each tool is for, and the package that provides it where it isn't bundled
PACKAGES = {"cdrecord": "cdrtools", "readcd": "cdrtools", "mkisofs": "cdrtools", "cdrdao": "cdrdao",
            "growisofs": "dvd+rw-tools", "dvd+rw-mediainfo": "dvd+rw-tools", "dvd+rw-booktype": "dvd+rw-tools",
            "dvd+rw-format": "dvd+rw-tools", "chdman": "mame-tools", "7z": "7zip",
            "dolphin-tool": "dolphin-emu"}


@lru_cache(maxsize=1)
def bundle_dirs() -> tuple[str, ...]:
    dirs = []
    env = os.environ.get("DISCFORGE_TOOLS")
    if env:
        dirs.append(env)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(os.path.join(meipass, "tools", "bin"))
    exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else ""
    if exe_dir:
        dirs += [os.path.join(exe_dir, "tools", "bin"), os.path.join(exe_dir, "..", "Resources", "tools", "bin")]
    appdir = os.environ.get("APPDIR")
    if appdir:
        dirs.append(os.path.join(appdir, "usr", "bin"))
    return tuple(d for d in dirs if os.path.isdir(d))


def find(name: str) -> str | None:
    names = [name + ".exe", name] if WINDOWS else [name]
    for d in bundle_dirs():
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return shutil.which(name)


def find_any(name: str) -> str | None:
    """find(), also accepting the other names a tool goes by (7zz for 7z, readom for readcd); for
    display and yes/no checks."""
    if name == "dolphin-tool":
        cmd = dolphin_tool()
        return " ".join(cmd) if cmd else None
    for n in ALIASES.get(name, (name,)):
        p = find(n)
        if p:
            return p
    return None


@lru_cache(maxsize=1)
def dolphin_tool() -> tuple[str, ...] | None:
    """The command that runs Dolphin's image converter: dolphin-tool on PATH or bundled, the one inside
    an installed Dolphin (Windows, macOS), or the Dolphin Flatpak's."""
    p = find("dolphin-tool") or find("DolphinTool")
    if p:
        return (p,)
    cands = []
    if sys.platform == "darwin":
        cands = [os.path.join(base, "Dolphin.app", "Contents", "MacOS", "dolphin-tool")
                 for base in ("/Applications", os.path.expanduser("~/Applications"))]
    elif WINDOWS:
        for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                cands += [os.path.join(base, d, "DolphinTool.exe")
                          for d in ("Dolphin", "Dolphin-x64", "Dolphin Emulator")]
    for c in cands:
        if os.path.isfile(c):
            return (c,)
    flatpak = shutil.which("flatpak") if not (WINDOWS or sys.platform == "darwin") else None
    if flatpak:
        import subprocess
        try:
            r = subprocess.run([flatpak, "info", "org.DolphinEmu.dolphin-emu"], capture_output=True,
                               stdin=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode == 0:
            return (flatpak, "run", "--filesystem=host", "--command=dolphin-tool", "org.DolphinEmu.dolphin-emu")
    return None


def bundled(name: str) -> bool:
    p = find(name)
    return bool(p) and any(os.path.abspath(p).startswith(os.path.abspath(d)) for d in bundle_dirs())


def package(name: str) -> str:
    return PACKAGES.get(name, name)


def env(extra: dict | None = None) -> dict:
    """Environment for running a burning tool.

    A frozen DiscForge (PyInstaller) points LD_LIBRARY_PATH / DYLD_LIBRARY_PATH at its own bundled
    libraries; tools must not inherit that, or they load Qt's copies of libstdc++ and friends.  The
    original values are restored, the bundled tools' own lib folder is added, and the bundled bin
    folders go first on PATH."""
    e = dict(os.environ)
    if getattr(sys, "frozen", False):
        for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
            orig = e.pop(var + "_ORIG", None)
            if orig is not None:
                e[var] = orig
            else:
                e.pop(var, None)
    libs = [os.path.join(os.path.dirname(d), "lib") for d in bundle_dirs()]
    libs = [d for d in libs if os.path.isdir(d)]
    if libs and not WINDOWS:
        var = "DYLD_FALLBACK_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
        e[var] = os.pathsep.join(libs + ([e[var]] if e.get(var) else []))
    if bundle_dirs():
        e["PATH"] = os.pathsep.join(list(bundle_dirs()) + [e.get("PATH", "")])
    if extra:
        e.update(extra)
    return e
