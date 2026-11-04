"""Test a disc in Flycast before burning it (standalone Flycast or the RetroArch core)."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys

from .cdi import write_cdi
from .plan import BurnPlan
from .util import cache_dir

HOME = os.path.expanduser("~")


def _core_dirs() -> list[str]:
    if sys.platform == "darwin":
        return [os.path.join(HOME, "Library", "Application Support", "RetroArch", "cores"),
                "/Applications/RetroArch.app/Contents/Resources/cores"]
    if sys.platform.startswith("win"):
        return [os.path.join(os.environ.get("APPDATA", ""), "RetroArch", "cores")]
    return ["/usr/lib/libretro", "/usr/lib64/libretro", "/usr/local/lib/libretro",
            "/usr/lib/x86_64-linux-gnu/libretro", "/usr/lib/aarch64-linux-gnu/libretro",
            os.path.join(HOME, ".config", "retroarch", "cores")]


def _core_in(folder: str) -> str | None:
    for name in ("flycast_libretro.so", "flycast_libretro.dylib", "flycast_libretro.dll"):
        p = os.path.join(folder, name)
        if os.path.isfile(p):
            return p
    return None


def _flatpak(app: str) -> bool:
    exe = shutil.which("flatpak")
    if not exe:
        return False
    try:
        return subprocess.run([exe, "info", app], capture_output=True, stdin=subprocess.DEVNULL,
                              timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def find_emulator() -> list[str] | None:
    """Command prefix that runs a disc image (standalone Flycast, or RetroArch with the Flycast core;
    installed normally, as an app bundle, in the usual Windows folders, or as a Flatpak), or None."""
    fly = shutil.which("flycast")
    if fly:
        return [fly]
    apps = []
    if sys.platform == "darwin":
        apps = [os.path.join(base, "Flycast.app", "Contents", "MacOS", "Flycast")
                for base in ("/Applications", os.path.join(HOME, "Applications"))]
    elif sys.platform.startswith("win"):
        for var in ("ProgramFiles", "LOCALAPPDATA"):
            if os.environ.get(var):
                apps.append(os.path.join(os.environ[var], "Flycast", "flycast.exe"))
    for a in apps:
        if os.path.isfile(a):
            return [a]
    ra_list = [shutil.which("retroarch")]
    if sys.platform == "darwin":
        ra_list += ["/Applications/RetroArch.app/Contents/MacOS/RetroArch"]
    elif sys.platform.startswith("win"):
        ra_list += [r"C:\RetroArch-Win64\retroarch.exe",
                    os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Steam", "steamapps", "common",
                                 "RetroArch", "retroarch.exe")]
    for ra in ra_list:
        if ra and os.path.isfile(ra):
            for d in [os.path.join(os.path.dirname(ra), "cores")] + _core_dirs():
                core = _core_in(d)
                if core:
                    return [ra, "-L", core]
    if not sys.platform.startswith(("win", "darwin")):
        if _flatpak("org.flycast.Flycast"):
            return ["flatpak", "run", "--filesystem=host", "org.flycast.Flycast"]
        core = _core_in(os.path.join(HOME, ".var", "app", "org.libretro.RetroArch", "config", "retroarch", "cores"))
        if core and _flatpak("org.libretro.RetroArch"):
            return ["flatpak", "run", "--filesystem=host", "org.libretro.RetroArch", "-L", core]
    return None


def install_hint() -> str:
    if sys.platform == "darwin":
        return "Install Flycast from flyca.st (or RetroArch with the Flycast core)."
    if sys.platform.startswith("win"):
        return "Install Flycast from flyca.st (or RetroArch with the Flycast core)."
    return ("Install Flycast (Flathub: org.flycast.Flycast) or RetroArch with the Flycast core, for example:\n"
            "  sudo pacman -S retroarch libretro-flycast")


def test_image_path(plan: BurnPlan) -> str:
    h = hashlib.sha1()
    path = plan.disc.path or plan.disc.name
    try:
        h.update(f"{path}:{os.path.getmtime(path)}".encode())
    except OSError:
        h.update(path.encode())
    h.update("\n".join(plan.describe()).encode())
    for s in plan.sessions:                 # catches IP.BIN patches and rebuilt homebrew
        for t in s.tracks:
            if t.mode != "audio":
                h.update(t.source.read(0, min(32, t.sectors)))
    return os.path.join(cache_dir(), f"test-{h.hexdigest()[:12]}.cdi")


def build_test_image(plan: BurnPlan, progress=None) -> str:
    """Write exactly what would be burned as a CDI (same LBAs, same patched IP.BIN)."""
    path = test_image_path(plan)
    if not os.path.exists(path):
        write_cdi(path, plan.cdi_sessions(), "DISCFORGE TEST", progress)
    return path


def launch(image: str) -> subprocess.Popen:
    cmd = find_emulator()
    if not cmd:
        raise RuntimeError("No Dreamcast emulator found. " + install_hint())
    return subprocess.Popen(cmd + [image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
