#!/usr/bin/env python3
"""Build a synthetic GD-ROM dump (.gdi) from a homebrew folder, for testing the converter.

    tests/make_test_gdi.py FOLDER OUTDIR

Layout like a real GD-ROM: track01 (data, LBA 0), track02 (audio, LBA 450),
track03 (data, LBA 45000) holding the folder with an UNscrambled 1ST_READ.BIN
and a "GD-ROM1/1" IP.BIN, plus an audio track04 and a data track05 after it.
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from discforge import ipbin  # noqa: E402

SYNC = bytes([0] + [0xFF] * 10 + [0])


def bcd(n):
    return ((n // 10) << 4) | (n % 10)


def mode1_raw(user: bytes, lba: int) -> bytes:
    out = bytearray()
    for i in range(len(user) // 2048):
        fad = lba + i + 150
        hdr = bytes([bcd(fad // 4500), bcd(fad // 75 % 60), bcd(fad % 75), 1])
        out += SYNC + hdr + user[i * 2048:(i + 1) * 2048] + bytes(288)
    return bytes(out)


def main(folder, outdir):
    os.makedirs(outdir, exist_ok=True)
    tmp = tempfile.mkdtemp()
    try:
        ip = bytearray(ipbin.build("SYNTHETIC GD TEST", boot_filename="1ST_READ.BIN"))
        ip[0x20:0x30] = b"0000 GD-ROM1/1  "
        ipbin.update_crc(ip)
        ip_path = os.path.join(tmp, "IP.BIN")
        open(ip_path, "wb").write(ip)
        iso = os.path.join(tmp, "hd.iso")
        subprocess.run(["mkisofs", "-quiet", "-C", "0,45000", "-V", "GDTEST", "-G", ip_path, "-l",
                        "-o", iso, folder], check=True)
        hd = open(iso, "rb").read()
        open(os.path.join(outdir, "track03.bin"), "wb").write(mode1_raw(hd, 45000))
        low = bytes(300 * 2048)
        open(os.path.join(outdir, "track01.bin"), "wb").write(mode1_raw(low, 0))
        open(os.path.join(outdir, "track02.raw"), "wb").write(bytes(2352 * 1000))
        t3_end = 45000 + len(hd) // 2048
        t4 = t3_end + 150
        open(os.path.join(outdir, "track04.raw"), "wb").write(bytes(2352 * 750))
        t5 = t4 + 750 + 150
        open(os.path.join(outdir, "track05.bin"), "wb").write(mode1_raw(bytes(500 * 2048), t5))
        with open(os.path.join(outdir, "disc.gdi"), "w") as f:
            f.write("5\n1 0 4 2352 track01.bin 0\n2 450 0 2352 track02.raw 0\n"
                    "3 45000 4 2352 track03.bin 0\n"
                    f"4 {t4} 0 2352 track04.raw 0\n5 {t5} 4 2352 \"track05.bin\" 0\n")
        print(os.path.join(outdir, "disc.gdi"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
