#!/usr/bin/env python3
"""Import Alex Free's LibCrypt Patcher (BSD 3-Clause) into DiscForge's data files.

    python3 -I tools/import_libcrypt.py /path/to/libcrypt-patcher-master

Reads lcp.c (which game boot file -> which PPF, plus single-byte revision checks) and the
ppf-c/*.PPF.h byte arrays as plain text, and writes:
    discforge/data/libcrypt/patches.zip    the PPF files, unchanged
    discforge/data/libcrypt/index.json     boot file -> variants
    discforge/data/libcrypt/LICENSE        upstream licence
Nothing from the source tree is executed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "discforge", "data", "libcrypt")


def c_array(text: str) -> tuple[str, bytes]:
    m = re.search(r"unsigned char\s+(\w+)\[\]\s*=\s*\{(.*?)\};", text, re.S)
    if not m:
        raise ValueError("no byte array")
    return m.group(1), bytes(int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{2})", m.group(2)))


def blocks(src: str):
    """Yield (comment lines, condition text, body text) for every boot-file branch in main()."""
    starts = [m.start() for m in re.finditer(r"(?:else\s+)?if\s*\((?:\s*\()+\s*strcmp\(boot_file", src)]
    end = src.rindex("printf(\"Unknown game")
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else end
        chunk = src[s:e]
        brace = chunk.index("{")
        cond, body = chunk[:brace], chunk[brace:]
        before = src[:s].rstrip().splitlines()
        comments = []
        for line in reversed(before[-4:]):
            t = line.strip()
            if t.startswith("//"):
                comments.insert(0, t[2:].strip())
            elif t in ("", "}"):
                if t == "}":
                    break
            else:
                break
        yield comments, cond, body


def variants_of(body: str, ppf_names: dict[str, str]) -> list[dict]:
    rev = re.search(r"id_rev\(bin,\s*(0x[0-9A-Fa-f]+|\d+),\s*(0x[0-9A-Fa-f]+|\d+),\s*(0x[0-9A-Fa-f]+|\d+)\)", body)

    def apply_in(part: str) -> str | None:
        m = re.search(r"apply_ppf\((\w+)_PPF,", part)
        if not m:
            return None
        return ppf_names[m.group(1) + "_PPF"]

    def title_in(part: str) -> str:
        m = re.search(r'printf\("([^"\\]+)', part)
        return m.group(1).strip() if m else ""

    if not rev:
        p = apply_in(body)
        return [{"ppf": p}] if p else []
    off, a, b = (int(x, 0) for x in rev.groups())
    split = re.split(r"\}\s*else\s+if\s*\(\s*is_rev\s*\)\s*\{", body, maxsplit=1)
    first, second = split[0], split[1] if len(split) > 1 else ""
    second = re.split(r"\}\s*else\s*\{", second, maxsplit=1)[0]
    out = []
    for byte, part in ((a, first), (b, second)):
        p = apply_in(part)
        out.append({"check": [off, byte], "ppf": p, "title": title_in(part),
                    "unprotected": p is None})
    return out


def main(src_dir: str) -> int:
    with open(os.path.join(src_dir, "lcp.c"), encoding="utf-8", errors="replace") as f:
        lcp = f.read()
    main_body = lcp[lcp.index("int main"):]
    ppf_names: dict[str, str] = {}
    ppfs: dict[str, bytes] = {}
    for name in sorted(os.listdir(os.path.join(src_dir, "ppf-c"))):
        if not name.endswith(".PPF.h"):
            continue
        with open(os.path.join(src_dir, "ppf-c", name), encoding="latin-1") as f:
            var, data = c_array(f.read())
        fname = name[:-2]                                   # SCES_003.11.PPF
        ppf_names[var] = fname
        ppfs[fname] = data
    index: dict[str, dict] = {}
    for comments, cond, body in blocks(main_body):
        boots = re.findall(r'strcmp\(boot_file,\s*"([^"]+)"\)', cond)
        disc_notes = re.findall(r'"\)\s*==\s*0\)\)\s*//\s*([^\n]+)', cond)
        variants = variants_of(body, ppf_names)
        if not variants:
            print("no patch in branch for", boots, file=sys.stderr)
            continue
        title = " / ".join(c for c in comments if c) or boots[0]
        for k, boot in enumerate(boots):
            entry = {"title": title, "variants": variants}
            if disc_notes and k < len(disc_notes):
                entry["disc"] = disc_notes[k].strip()
            index[boot] = entry
    used = {v["ppf"] for e in index.values() for v in e["variants"] if v.get("ppf")}
    missing = used - set(ppfs)
    if missing:
        print("PPF files referenced but missing:", sorted(missing), file=sys.stderr)
        return 1
    os.makedirs(OUT, exist_ok=True)
    with zipfile.ZipFile(os.path.join(OUT, "patches.zip"), "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in sorted(used):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 24, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, ppfs[name])
    with open(os.path.join(OUT, "index.json"), "w") as f:
        json.dump({"source": "https://github.com/alex-free/libcrypt-patcher", "version": _version(src_dir),
                   "games": dict(sorted(index.items()))}, f, indent=1, sort_keys=False)
    with open(os.path.join(src_dir, "license.md"), encoding="utf-8") as f:
        lic = f.read()
    with open(os.path.join(OUT, "LICENSE"), "w") as f:
        f.write("The LibCrypt patches in this folder come from LibCrypt Patcher by Alex Free\n"
                "https://github.com/alex-free/libcrypt-patcher\n\n" + lic)
    print(f"{len(index)} boot files, {len(used)} patches -> {os.path.normpath(OUT)}")
    return 0


def _version(src_dir: str) -> str:
    try:
        with open(os.path.join(src_dir, "variables.mk"), encoding="latin-1") as f:
            m = re.search(r"VERSION\s*=\s*(\S+)", f.read())
            return m.group(1) if m else ""
    except OSError:
        return ""


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/Downloads/libcrypt-patcher-master")))
