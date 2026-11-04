#!/usr/bin/env python3
"""Boot a Dreamcast image in Flycast (RetroArch core, HLE BIOS) inside a throwaway
headless KWin and save a screenshot after N frames.  Nothing appears on the desktop.

    tests/emu_boot.py IMAGE.cdi SHOT.png [frames]

Cleanup only touches processes carrying this run's unique marker variable,
never anything matched by name (the desktop runs kwin_wayland too).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time

CORE = "/usr/lib/libretro/flycast_libretro.so"


def marked_pids(mark: str) -> list[int]:
    pids = []
    needle = f"DISCFORGE_TEST_MARK={mark}".encode()
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            with open(f"/proc/{name}/environ", "rb") as f:
                if needle in f.read().split(b"\0"):
                    pids.append(int(name))
        except OSError:
            pass
    return pids


def boot(image: str, shot: str, frames: int = 1500, size=(1280, 960)) -> tuple[int, str]:
    tmp = tempfile.mkdtemp(prefix="dcburn-emu-")
    for d in ("system", "saves", "states", "shots"):
        os.makedirs(os.path.join(tmp, d))
    opts = os.path.join(tmp, "opts.cfg")
    with open(opts, "w") as f:
        f.write('reicast_hle_bios = "enabled"\n')
    cfg = os.path.join(tmp, "retroarch.cfg")
    with open(cfg, "w") as f:
        f.write(f'''video_driver = "gl"
audio_driver = "null"
audio_enable = "false"
menu_driver = "rgui"
system_directory = "{tmp}/system"
savefile_directory = "{tmp}/saves"
savestate_directory = "{tmp}/states"
screenshot_directory = "{tmp}/shots"
core_options_path = "{opts}"
global_core_options = "true"
config_save_on_exit = "false"
video_fullscreen = "false"
video_window_width = "{size[0]}"
video_window_height = "{size[1]}"
pause_nonactive = "false"
video_font_enable = "false"
libretro_directory = "/usr/lib/libretro"
libretro_info_path = "/usr/share/libretro/info"
content_runtime_log = "false"
content_runtime_log_aggregate = "false"
history_list_enable = "false"
core_info_cache_enable = "false"
playlist_directory = "{tmp}"
runtime_log_directory = "{tmp}"
log_dir = "{tmp}"
cache_directory = "{tmp}"
''')
    mark = f"{os.getpid()}-{int(time.time())}"
    sock = f"dcburn-test-{mark}"
    env = dict(os.environ, DISCFORGE_TEST_MARK=mark)
    kwin = subprocess.Popen(
        ["dbus-run-session", "--", "kwin_wayland", "--virtual", "--no-lockscreen", "--no-global-shortcuts",
         "--width", str(size[0]), "--height", str(size[1]), "--socket", sock],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    log = os.path.join(tmp, "retroarch.log")
    try:
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        for _ in range(100):
            if os.path.exists(os.path.join(runtime, sock)):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("nested KWin did not start")
        renv = dict(env, WAYLAND_DISPLAY=sock, XDG_SESSION_TYPE="wayland")
        renv.pop("DISPLAY", None)
        r = subprocess.run(["retroarch", "--config", cfg, "-L", CORE, f"--max-frames={frames}",
                            "--max-frames-ss", f"--max-frames-ss-path={shot}", "--verbose",
                            f"--log-file={log}", image],
                           env=renv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=frames / 4 + 120)   # some homebrew polls hardware and runs slowly
        rc = r.returncode
    finally:
        try:
            os.killpg(kwin.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            kwin.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(kwin.pid, signal.SIGKILL)
        # leftovers: exact PIDs carrying our marker or our unique socket name only
        for attempt in range(50):
            left = set(marked_pids(mark)) | set(socket_pids(sock))
            if not left:
                break
            for pid in left:
                try:
                    os.kill(pid, signal.SIGTERM if attempt < 30 else signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(0.2)
    return rc, log


def socket_pids(sock: str) -> list[int]:
    pids = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as f:
                if sock.encode() in f.read().split(b"\0"):
                    pids.append(int(name))
        except OSError:
            pass
    return pids


if __name__ == "__main__":
    image, shot = sys.argv[1], sys.argv[2]
    frames = int(sys.argv[3]) if len(sys.argv) > 3 else 1500
    rc, log = boot(os.path.abspath(image), os.path.abspath(shot), frames)
    print(f"retroarch exit {rc}; screenshot {'saved' if os.path.exists(shot) else 'MISSING'}; log {log}")
    with open(log, errors="replace") as f:
        for line in f:
            if any(k in line for k in ("REIOS", "GDROM", "Boot", "boot", "Error", "ERROR", "fail", "IP.BIN", "Disc")):
                print("  ", line.rstrip()[:200])
