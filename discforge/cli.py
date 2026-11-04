"""Command line interface: discforge info|burn|doctor|drives|cdi|test|scramble|descramble|gui."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from . import burner as B
from . import ipbin
from . import scramble as SCR
from .disc import Disc
from .loader import load_any
from .plan import make_plan
from .util import DcError, clean_cache, fmt_mb, read_bytes, write_bytes


def _load(args) -> Disc:
    scr = None
    if getattr(args, "scrambled", False):
        scr = True
    elif getattr(args, "unscrambled", False):
        scr = False
    return load_any(args.image, title=getattr(args, "title", None), scrambled=scr,
                    regions="" if getattr(args, "no_region", False) else "JUE",
                    vga=not getattr(args, "no_vga", False), gd_lba=getattr(args, "gd_lba", False))


def _plan(disc: Disc, args, capacity=None):
    kw = dict(region_free=args.region_free, vga=args.vga)
    if capacity:
        kw["capacity"] = capacity
    return make_plan(disc, **kw)


def describe(disc: Disc, plan) -> list[str]:
    out = [f"{disc.name}  [{disc.kind}, {disc.layout}]"]
    ip = disc.ip()
    if ip:
        out.append(f"  Title     : {ip.title or '-'}")
        out.append(f"  Product   : {ip.product_no or '-'}  {ip.version}  ({ip.release_date})  by {ip.sw_maker or '-'}")
        regs = ", ".join(ipbin.REGION_NAMES[r] for r in ip.regions) or "none"
        out.append(f"  Regions   : {regs}    VGA: {'yes' if ip.vga else 'no'}    "
                   f"WinCE: {'yes' if ip.wince else 'no'}    Media: {ip.media}")
        out.append(f"  Boot file : {ip.boot_filename}")
    out.append("  Burn plan :")
    out += ["    " + line for line in plan.describe()]
    out.append(f"  Disc use  : {fmt_mb(plan.end_lba)} of {fmt_mb(plan.capacity)}")
    for n in disc.notes + plan.notes:
        out.append(f"  note: {n}")
    for w in disc.warnings + plan.warnings:
        out.append(f"  WARNING: {w}")
    for e in plan.errors:
        out.append(f"  ERROR: {e}")
    return out


def _dc_opts(args) -> dict:
    scr = True if getattr(args, "scrambled", False) else False if getattr(args, "unscrambled", False) else None
    return {"title": getattr(args, "title", None), "scrambled": scr, "gd_lba": getattr(args, "gd_lba", False),
            "regions": "" if getattr(args, "no_region", False) else "JUE", "vga": not getattr(args, "no_vga", False),
            "region_free": getattr(args, "region_free", True) or True}


def _open(args):
    from .jobs import open_job

    def prog(text: str, frac: float) -> None:
        if frac >= 0:
            sys.stderr.write(f"\r{text} {frac * 100:5.1f}%   ")
            sys.stderr.flush()

    opts = _dc_opts(args)
    opts["libcrypt"] = not getattr(args, "no_libcrypt", False)
    job = open_job(args.image, progress=prog, opts=opts)
    sys.stderr.write("\r" + " " * 60 + "\r")
    return job


def describe_job(job) -> list[str]:
    from . import tools
    from .platforms import media_label
    if job.method == "dc":
        return describe(job.disc, job.plan)
    dvd_tool = "cdrecord" if tools.DVD_WITH_CDRECORD or not tools.find("growisofs") else "growisofs"
    out = [f"{job.title}", f"  System:  {job.platform.name}" + (f"   ({job.game_id})" if job.game_id else ""),
           f"  Disc:    {media_label(job.media)}, {job.size_bytes / 1e6:,.0f} MB"
           f" ({job.sectors:,} {'CD' if job.method == 'cd' else 'DVD'} sectors)",
           f"  Burner:  {'cdrdao, raw disc-at-once' if job.method == 'cd' else dvd_tool + ', closed disc'}"]
    out += [f"  {k}: {v}" for k, v in job.details]
    out += [f"  Note: {job.platform.needs}"]
    out += [f"  WARNING: {w}" for w in job.warnings]
    out += [f"  CAN'T BURN: {b}" for b in job.blockers]
    return out


def cmd_info(args) -> int:
    job = _open(args)
    try:
        if args.json:
            print(json.dumps({"title": job.title, "platform": job.platform.id, "game_id": job.game_id,
                              "method": job.method, "media": job.media, "sectors": job.sectors,
                              "details": job.details, "warnings": job.warnings, "blockers": job.blockers,
                              "plan": job.plan.describe() if job.plan else None}, indent=2))
        else:
            print("\n".join(describe_job(job)))
        return 0 if job.ok else 2
    finally:
        job.cleanup()


def cmd_drives(_args) -> int:
    drives = B.list_drives()
    if not drives:
        print("No optical drives found. Plug in a CD/DVD writer (any USB DVD writer burns CD-R).")
        return 1
    for d in drives:
        print(d.label)
        if d.accessible and B.cdrecord_path():
            try:
                m = B.media_info(d.dev)
                if not m.present:
                    print("   no disc")
                else:
                    print(f"   disc: {m.kind or '?'}  status {m.status or '?'}  free {m.capacity or '?'} sectors"
                          f"{'  (rewritable)' if m.erasable else ''}  write modes: {m.modes or '?'}")
            except B.BurnError as e:
                print(f"   {e}")
    return 0


def _progress_printer():
    last = [""]

    def progress(frac: float, text: str) -> None:
        line = f"\r[{'#' * int(frac * 30):<30}] {frac * 100:5.1f}%  {text}"
        if line != last[0]:
            sys.stdout.write(line[:110].ljust(110))
            sys.stdout.flush()
            last[0] = line

    return progress


def cmd_burn(args) -> int:
    from .jobs import burn_job
    job = _open(args)
    try:
        print("\n".join(describe_job(job)))
        if not job.ok:
            return 2
        drives = B.list_drives()
        dev = args.dev or (drives[0].dev if drives else None)
        if not dev:
            print("\nNo optical drive found. Plug in a CD/DVD writer and try again.")
            return 1
        speed = args.speed
        if speed is None and job.method == "dc":
            speed = B.choose_speed(B.drive_caps(dev).cd_write_speeds)
        elif speed is None and job.method == "cd":
            speed = B.choose_speed(B.drive_caps(dev).cd_write_speeds, job.platform.speed)
        mode = "SIMULATE (laser off)" if args.simulate else "BURN"
        if not args.yes:
            ans = input(f"\n{mode} on {dev} at {speed or 'auto'}x? Insert the blank disc and type 'yes': ")
            if ans.strip().lower() not in ("y", "yes"):
                print("Aborted.")
                return 1
        opts = B.BurnOptions(dev=dev, speed=speed, simulate=args.simulate,
                             verify=not args.no_verify, eject=not args.no_eject, allow_rw=args.allow_rw)
        prog = _progress_printer()

        def events(e: dict) -> None:
            st = e.get("stage")
            if st == "write":
                prog(e.get("frac", 0.0), f"writing {e.get('mb', 0)}/{e.get('mb_total', 0)} MB"
                     + (f" @ {e['speed']:.0f}x" if e.get("speed") else ""))
            elif st == "verify":
                prog(e.get("part", 0.0), "verifying")
            elif st == "fixate":
                prog(1.0, "finishing the disc")

        def log(msg: str) -> None:
            sys.stdout.write("\r" + " " * 110 + "\r" + msg + "\n")

        try:
            res = burn_job(job, opts, log=log if args.verbose else (lambda m: None), events=events)
        except B.BurnError as e:
            print(f"\n\nFAILED: {e}")
            return 1
        print()
        for m in res.messages:
            print(m)
        if res.verified is False:
            print("Burn finished but verification FAILED.")
            return 1
        print("Done." + (" Verified OK." if res.verified else ""))
        return 0
    finally:
        job.cleanup()


def cmd_cdi(args) -> int:
    from .cdi import write_cdi
    disc = _load(args)
    plan = _plan(disc, args)
    if not plan.ok:
        print("\n".join(describe(disc, plan)))
        return 2
    src = os.path.abspath(os.path.normpath(args.image))
    out = args.output or os.path.join(os.path.dirname(src),
                                      os.path.splitext(os.path.basename(src))[0] + ".selfboot.cdi")
    if os.path.abspath(out) == src:
        out = os.path.splitext(src)[0] + ".selfboot.cdi"
    vol = (disc.ip().title if disc.ip() else "DREAMCAST")[:32]
    prog = _progress_printer()
    write_cdi(out, plan.cdi_sessions(), vol, progress=lambda d, t: prog(d / max(t, 1), "writing CDI"))
    print(f"\nWrote {out} ({os.path.getsize(out) / 2**20:.1f} MB)")
    disc.cleanup()
    return 0


def cmd_test(args) -> int:
    from . import emulator
    disc = _load(args)
    plan = _plan(disc, args)
    if not plan.ok:
        print("\n".join(describe(disc, plan)))
        return 2
    prog = _progress_printer()
    path = emulator.build_test_image(plan, progress=lambda d, t: prog(d / max(t, 1), "building test image"))
    print(f"\nLaunching emulator with {path}")
    emulator.launch(path)
    disc.cleanup()
    return 0


def cmd_scramble(args) -> int:
    data = read_bytes(args.input)
    out = SCR.scramble(data) if args.cmd == "scramble" else SCR.descramble(data)
    write_bytes(args.output, out)
    return 0


def cmd_doctor(_args) -> int:
    """Everything that matters for burning on this computer, in one report."""
    import platform as pf
    from . import devices, libcrypt, tools
    print(f"DiscForge {__version__} on {pf.system()} {pf.release()} ({pf.machine()}), Python {pf.python_version()}")
    print("\nTools:")
    ok = True
    for exe in tools.REQUIRED + tools.OPTIONAL:
        p = tools.find_any(exe)
        where = "missing" if not p else ("bundled  " + p if tools.bundled(exe) else "system   " + p)
        need = exe in tools.REQUIRED
        ok &= bool(p) or not need
        print(f"  {exe:<17} {where}" + ("   <- needed for burning" if need and not p else ""))
    print("\nDrives:")
    drives = devices.list_drives()
    if not drives:
        print("  none found - plug in a CD/DVD writer")
    for d in drives:
        print(f"  {d.label}")
        print(f"    writes: {', '.join(d.writes) or 'unknown'}")
        if not d.accessible:
            fix = devices.fix_permission_command(d)
            print("    NO ACCESS for this account" + (f" - fix: sudo {' '.join(fix)} (then log in again)" if fix
                                                    else ""))
            continue
        try:
            m = B.media_info(d.dev)
            print(f"    disc: {m.kind or 'none'}" + (f", {m.status}, {m.capacity or '?'} sectors free" if m.present
                                                   else ""))
        except B.BurnError as e:
            print(f"    disc: {e}")
    print(f"\nLibCrypt patches: {libcrypt.known_count()} PAL PlayStation releases")
    from .util import cache_dir
    print(f"Cache: {cache_dir()}")
    return 0 if ok else 1


def cmd_gui(args) -> int:
    from .gui.app import main as gui_main
    return gui_main(args.image)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="discforge", description=(
        "Burn retro console games, homebrew and software to CD and DVD: Dreamcast, PlayStation 1/2, GameCube, Wii, "
        "Xbox, Saturn, Sega CD and more."))
    p.add_argument("--version", action="version", version=f"discforge {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def image_cmd(name, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("image", help="a game: .cue/.bin, .iso, .cdi, .gdi, .chd, .wbfs, .rvz, .cso, .zip ... "
                                       "or a folder")
        sp.add_argument("--region-free", action="store_true", help="patch IP.BIN to boot on any region's console")
        sp.add_argument("--vga", action="store_true", help="patch IP.BIN to allow the VGA box")
        sp.add_argument("--no-libcrypt", action="store_true",
                        help="don't remove LibCrypt protection from PAL PlayStation games")
        g = sp.add_argument_group("homebrew options")
        g.add_argument("--title", help="title for a generated IP.BIN")
        g.add_argument("--scrambled", action="store_true", help="the program is already scrambled")
        g.add_argument("--unscrambled", action="store_true", help="the program is NOT scrambled yet")
        g.add_argument("--no-vga", action="store_true", help="generated IP.BIN without the VGA flag")
        g.add_argument("--gd-lba", action="store_true",
                       help="boot session at LBA 45000, for programs that expect the GD-ROM data position "
                            "(e.g. GDEMU menus such as openMenu)")
        return sp

    sp = image_cmd("info", "show what's in an image and how it would be burned")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_info)

    sp = image_cmd("burn", "burn a game to CD or DVD")
    sp.add_argument("--dev", help="burner device (default: first one, e.g. /dev/sr0)")
    sp.add_argument("--speed", type=int, default=None,
                    help="write speed (default: automatic, a slow safe speed for the console)")
    sp.add_argument("--simulate", action="store_true", help="dry run with the laser off")
    sp.add_argument("--no-verify", action="store_true", help="skip reading the disc back")
    sp.add_argument("--no-eject", action="store_true")
    sp.add_argument("--allow-rw", action="store_true", help="allow CD-RW media (rarely readable by a Dreamcast)")
    sp.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    sp.add_argument("-v", "--verbose", action="store_true", help="show the burning tool's output")
    sp.set_defaults(func=cmd_burn)

    sp = image_cmd("cdi", "save as a selfboot .cdi (for emulators, ODEs or other burners)")
    sp.add_argument("-o", "--output")
    sp.set_defaults(func=cmd_cdi)

    sp = image_cmd("test", "boot exactly what would be burned in Flycast")
    sp.set_defaults(func=cmd_test)

    sp = sub.add_parser("drives", help="list burners and the disc inside")
    sp.set_defaults(func=cmd_drives)

    sp = sub.add_parser("doctor", help="check tools, drives and permissions")
    sp.set_defaults(func=cmd_doctor)

    for name in ("scramble", "descramble"):
        sp = sub.add_parser(name, help=f"{name} a 1ST_READ.BIN")
        sp.add_argument("input")
        sp.add_argument("output")
        sp.set_defaults(func=cmd_scramble)

    sp = sub.add_parser("gui", help="open the graphical burner")
    sp.add_argument("image", nargs="?")
    sp.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_gui)

    args = p.parse_args(argv)
    clean_cache()
    if not args.cmd:
        return cmd_gui(argparse.Namespace(image=None))
    try:
        return args.func(args)
    except (DcError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
