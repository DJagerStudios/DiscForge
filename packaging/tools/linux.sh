#!/usr/bin/env bash
# Builds the CD/DVD tools that ship inside DiscForge's Linux downloads, from pinned sources.
#
#   packaging/tools/linux.sh OUT_DIR [SOURCES_DIR]
#
#   OUT_DIR/bin       cdrecord readcd mkisofs        cdrtools 3.02a09
#                     cdrdao                         cdrdao 1.2.6
#                     growisofs dvd+rw-mediainfo ... dvd+rw-tools 7.1 with Debian's 7.1-14 patches
#                     7zz                            7-Zip 26.05 (the official static build)
#   OUT_DIR/lib       shared libraries a tool needs that not every Linux system has (usually none)
#   OUT_DIR/licenses  every tool's licence and SOURCES.md (where each source comes from)
#   SOURCES_DIR       the source archives themselves, published next to each release
#
# Needs a C/C++ compiler, make, m4, curl, xz and patchelf (Ubuntu: build-essential m4 curl xz-utils
# patchelf).  The result runs on any Linux with the build system's glibc or newer.
set -euo pipefail

OUT=$(realpath -m "$1")
SRC=$(realpath -m "${2:-$OUT/../tool-sources}")
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
JOBS=$(nproc)
case "$(uname -m)" in
  x86_64) SEVENZIP_ARCH=x64; SEVENZIP_SHA=22cf82fcc6eb7474f1d5bb4f02c0f8ebd573a718089d6833dc30f8350a3b232e ;;
  aarch64) SEVENZIP_ARCH=arm64; SEVENZIP_SHA=0b71f6cfc34669b3bf69f2347efd3429f77020665003a13b9cdc53b784270421 ;;
  *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

log() { printf '==> %s\n' "$*"; }

fetch() {  # url sha256 name -> prints the local path; the file is kept in SOURCES_DIR
  local dest="$SRC/$3"
  if [ ! -f "$dest" ] || ! echo "$2  $dest" | sha256sum -c --status -; then
    curl -fsSL --retry 4 --retry-delay 5 -o "$dest.part" "$1"
    echo "$2  $dest.part" | sha256sum -c - >&2
    mv "$dest.part" "$dest"
  fi
  printf '%s\n' "$dest"
}

quiet() {  # run a build step, showing its output only when it fails
  local logf="$WORK/step.log"
  if ! "$@" >"$logf" 2>&1; then
    tail -80 "$logf" >&2
    return 1
  fi
}

mkdir -p "$OUT/bin" "$OUT/lib" "$OUT/licenses" "$SRC"
SOURCES="$OUT/licenses/SOURCES.md"
cat > "$SOURCES" <<'EOF'
# Where DiscForge's bundled tools come from

DiscForge runs these programs as separate processes. Each keeps its own licence (see the other
files in this folder). Their exact source archives are published with every DiscForge release
(`DiscForge-<version>-tool-sources.tar`) and are listed here with their SHA-256 checksums.

| Tool | Version | Licence | Source | SHA-256 |
|---|---|---|---|---|
EOF
# (the backticks are Markdown, not a command)
# shellcheck disable=SC2016
source_row() { printf '| %s | %s | %s | %s | `%s` |\n' "$@" >> "$SOURCES"; }

# --------------------------------------------------------------------------------------------
log "cdrtools 3.02a09 (cdrecord, readcd, mkisofs)"
f=$(fetch https://downloads.sourceforge.net/project/cdrtools/alpha/cdrtools-3.02a09.tar.bz2 \
  aa28438f458ef3f314b79f2029db27679dae1d5ffe1569b6de57742511915e81 cdrtools-3.02a09.tar.bz2)
tar -xjf "$f" -C "$WORK"
(
  cd "$WORK/cdrtools-3.02"
  # the Schily makefiles don't support parallel builds; -std=gnu89 keeps newer compilers happy
  export GMAKE_NOWARN=true CFLAGS="-O2 -std=gnu89 -fno-strict-aliasing"
  quiet make INS_BASE=/usr INS_RBASE=/
  for t in cdrecord readcd mkisofs; do
    bin=$(find "$t/OBJ" -type f -name "$t" | head -n1)
    [ -n "$bin" ] || { echo "cdrtools: $t wasn't built" >&2; exit 1; }
    install -m 755 "$bin" "$OUT/bin/$t"
  done
  cp COPYING "$OUT/licenses/cdrtools-COPYING.txt"
  cp CDDL.Schily.txt "$OUT/licenses/cdrtools-CDDL.txt"
  cp GPL-2.0.txt "$OUT/licenses/cdrtools-GPL-2.0.txt"
  cp LGPL-2.1.txt "$OUT/licenses/cdrtools-LGPL-2.1.txt"
)
source_row cdrtools 3.02a09 "CDDL-1.0 (cdrecord, readcd), GPL-2.0 (mkisofs)" \
  "https://downloads.sourceforge.net/project/cdrtools/alpha/cdrtools-3.02a09.tar.bz2" \
  aa28438f458ef3f314b79f2029db27679dae1d5ffe1569b6de57742511915e81

# --------------------------------------------------------------------------------------------
log "cdrdao 1.2.6"
f=$(fetch https://github.com/cdrdao/cdrdao/releases/download/rel_1_2_6/cdrdao-1.2.6.tar.bz2 \
  0cf29e1183ffd8e4d167c403d7a6ea2109a2dd4cd97e61f80417cf1373220ff4 cdrdao-1.2.6.tar.bz2)
tar -xjf "$f" -C "$WORK"
(
  cd "$WORK/cdrdao-1.2.6"
  quiet ./configure --prefix=/usr --without-gcdmaster --without-ogg-support --without-mp3-support
  quiet make -j"$JOBS"
  install -m 755 dao/cdrdao "$OUT/bin/cdrdao"
  cp COPYING "$OUT/licenses/cdrdao-COPYING.txt"
)
source_row cdrdao 1.2.6 GPL-2.0-or-later \
  "https://github.com/cdrdao/cdrdao/releases/download/rel_1_2_6/cdrdao-1.2.6.tar.bz2" \
  0cf29e1183ffd8e4d167c403d7a6ea2109a2dd4cd97e61f80417cf1373220ff4

# --------------------------------------------------------------------------------------------
log "dvd+rw-tools 7.1 + Debian 7.1-14 patches (growisofs)"
orig=$(fetch https://deb.debian.org/debian/pool/main/d/dvd+rw-tools/dvd+rw-tools_7.1.orig.tar.gz \
  f8d60f822e914128bcbc5f64fbe3ed131cbff9045dca7e12c5b77b26edde72ca dvd+rw-tools_7.1.orig.tar.gz)
deb=$(fetch https://deb.debian.org/debian/pool/main/d/dvd+rw-tools/dvd+rw-tools_7.1-14.debian.tar.xz \
  37af7d602118c8456af119d9d2164cbe10a47cd44cd6dec7c6a486bc4ed4490c dvd+rw-tools_7.1-14.debian.tar.xz)
tar -xzf "$orig" -C "$WORK"
(
  cd "$WORK/dvd+rw-tools-7.1"
  tar -xJf "$deb"
  while read -r p; do
    case "$p" in ''|'#'*) continue ;; esac
    patch -p1 -s < "debian/patches/$p"
  done < debian/patches/series
  # I_KNOW_ALL_ABOUT_SUDO: growisofs otherwise refuses to run when started through sudo
  quiet make WARN=-DI_KNOW_ALL_ABOUT_SUDO
  for t in growisofs dvd+rw-mediainfo dvd+rw-booktype dvd+rw-format; do
    install -m 755 "$t" "$OUT/bin/$t"
  done
  cp LICENSE "$OUT/licenses/dvd+rw-tools-LICENSE.txt"
  cp debian/copyright "$OUT/licenses/dvd+rw-tools-debian-copyright.txt"
)
source_row dvd+rw-tools "7.1 (Debian 7.1-14)" GPL-2.0 \
  "https://deb.debian.org/debian/pool/main/d/dvd+rw-tools/dvd+rw-tools_7.1.orig.tar.gz" \
  f8d60f822e914128bcbc5f64fbe3ed131cbff9045dca7e12c5b77b26edde72ca
source_row "dvd+rw-tools patches" 7.1-14 GPL-2.0 \
  "https://deb.debian.org/debian/pool/main/d/dvd+rw-tools/dvd+rw-tools_7.1-14.debian.tar.xz" \
  37af7d602118c8456af119d9d2164cbe10a47cd44cd6dec7c6a486bc4ed4490c

# --------------------------------------------------------------------------------------------
log "7-Zip 26.05 (7zz, static)"
f=$(fetch "https://github.com/ip7z/7zip/releases/download/26.05/7z2605-linux-$SEVENZIP_ARCH.tar.xz" \
  "$SEVENZIP_SHA" "7z2605-linux-$SEVENZIP_ARCH.tar.xz")
mkdir -p "$WORK/7z"
tar -xJf "$f" -C "$WORK/7z"
install -m 755 "$WORK/7z/7zzs" "$OUT/bin/7zz"
cp "$WORK/7z/License.txt" "$OUT/licenses/7-Zip-License.txt"
fetch https://github.com/ip7z/7zip/releases/download/26.05/7z2605-src.tar.xz \
  0b6080b8c3e7a579161d2d495224c0e51c467576cb4c7048b0ab485ef44cc22e 7z2605-src.tar.xz > /dev/null
source_row 7-Zip 26.05 "LGPL-2.1-or-later, BSD-3-Clause, unRAR restriction" \
  "https://github.com/ip7z/7zip/releases/download/26.05/7z2605-src.tar.xz" \
  0b6080b8c3e7a579161d2d495224c0e51c467576cb4c7048b0ab485ef44cc22e

# --------------------------------------------------------------------------------------------
log "libraries"
# Bundle any shared library outside the base set every distribution has, and point the tools at
# their own lib folder.  (These tools normally need nothing beyond the base set.)  The dynamic
# loader itself must never be bundled.
base='^(linux-vdso|linux-gate|ld-linux.*|ld64|libc|libm|libdl|librt|libpthread|libresolv|libutil|libgcc_s|libstdc\+\+|libz|libcap)\.so'
for b in "$OUT"/bin/*; do
  # (ldd fails on static programs such as 7zz; that just means there's nothing to bundle)
  { ldd "$b" 2>/dev/null || true; } | awk '/=> \// {print $1, $3}' | while read -r soname libfile; do
    soname=${soname##*/}
    if ! [[ "$soname" =~ $base ]]; then
      cp -Ln "$libfile" "$OUT/lib/$soname"
      echo "  $(basename "$b") needs $soname (bundled)"
    fi
  done
  if [ -n "$(ls -A "$OUT/lib")" ]; then
    # $ORIGIN is for the dynamic linker, not the shell
    # shellcheck disable=SC2016
    patchelf --set-rpath '$ORIGIN/../lib' "$b" 2>/dev/null || true
  fi
done
strip "$OUT"/bin/* 2>/dev/null || true

log "done"
for b in "$OUT"/bin/*; do
  printf '  %-18s %s\n' "$(basename "$b")" "$(file -b "$b" | cut -d, -f1-2)"
done
