#!/usr/bin/env bash
# Builds the CD/DVD tools that ship inside DiscForge.app, from pinned sources.
#
#   packaging/tools/macos.sh OUT_DIR [SOURCES_DIR]
#
#   OUT_DIR/bin       cdrecord readcd mkisofs   cdrtools 3.02a09 (writes CDs, DVDs and Dreamcast discs)
#                     cdrdao                    cdrdao 1.2.6 (raw disc-at-once CDs)
#                     7zz                       7-Zip 26.05 (the official universal build)
#   OUT_DIR/licenses  every tool's licence and SOURCES.md
#   SOURCES_DIR       the source archives themselves, published next to each release
#
# Built for macOS 14 and newer (MACOSX_DEPLOYMENT_TARGET), linking only Apple's system frameworks,
# so nothing else has to be installed.  Needs the Xcode command line tools and Homebrew's smake
# (build only: `brew install smake`).
set -euo pipefail

OUT=$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$1")
SRC=$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "${2:-$OUT/../tool-sources}")
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
JOBS=$(sysctl -n hw.ncpu)
export MACOSX_DEPLOYMENT_TARGET=${MACOSX_DEPLOYMENT_TARGET:-14.0}
# Build against the SDK only: on Intel Macs Homebrew lives in /usr/local, which the compiler would
# otherwise search, and its libraries must not end up linked into the tools.
SDK=$(xcrun --show-sdk-path)
SYS="-isysroot $SDK"

log() { printf '==> %s\n' "$*"; }

fetch() {  # url sha256 name -> prints the local path; the file is kept in SOURCES_DIR
  local dest="$SRC/$3"
  if [ ! -f "$dest" ] || ! echo "$2  $dest" | shasum -a 256 -c --status -; then
    curl -fsSL --retry 4 --retry-delay 5 -o "$dest.part" "$1"
    echo "$2  $dest.part" | shasum -a 256 -c - >&2
    mv "$dest.part" "$dest"
  fi
  printf '%s\n' "$dest"
}

quiet() {
  local logf="$WORK/step.log"
  if ! "$@" >"$logf" 2>&1; then
    tail -80 "$logf" >&2
    return 1
  fi
}

mkdir -p "$OUT/bin" "$OUT/licenses" "$SRC"
SOURCES="$OUT/licenses/SOURCES.md"
cat > "$SOURCES" <<'EOF'
# Where DiscForge's bundled tools come from

DiscForge runs these programs as separate processes. Each keeps its own licence (see the other
files in this folder). Their exact source archives are published with every DiscForge release
(`DiscForge-<version>-tool-sources.tar`) and are listed here with their SHA-256 checksums.

| Tool | Version | Licence | Source | SHA-256 |
|---|---|---|---|---|
EOF
source_row() { printf '| %s | %s | %s | %s | `%s` |\n' "$@" >> "$SOURCES"; }

# --------------------------------------------------------------------------------------------
log "cdrtools 3.02a09 (cdrecord, readcd, mkisofs) for macOS $MACOSX_DEPLOYMENT_TARGET+"
command -v smake >/dev/null || { echo "smake is needed: brew install smake" >&2; exit 1; }
f=$(fetch https://downloads.sourceforge.net/project/cdrtools/alpha/cdrtools-3.02a09.tar.bz2 \
  aa28438f458ef3f314b79f2029db27679dae1d5ffe1569b6de57742511915e81 cdrtools-3.02a09.tar.bz2)
tar -xjf "$f" -C "$WORK"
(
  cd "$WORK/cdrtools-3.02"
  rm -f lib*/*_p.mk                  # skip the profiled libraries (as Homebrew does)
  quiet smake INS_BASE=/usr/local INS_RBASE=/usr/local \
    "CFLAGS=-O2 $SYS -Wno-implicit-int -Wno-implicit-function-declaration -Wno-int-conversion" \
    "LDOPTX=$SYS"
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
  # system tools only (no Homebrew pkg-config, lame or libao): the result needs nothing extra
  export PATH=/usr/bin:/bin:/usr/sbin:/sbin PKG_CONFIG=/usr/bin/false
  export CFLAGS="-O2 $SYS" CXXFLAGS="-O2 $SYS" LDFLAGS="$SYS"
  quiet ./configure --prefix=/usr/local --without-gcdmaster --without-ogg-support --without-mp3-support
  quiet make -j"$JOBS"
  install -m 755 dao/cdrdao "$OUT/bin/cdrdao"
  cp COPYING "$OUT/licenses/cdrdao-COPYING.txt"
)
source_row cdrdao 1.2.6 GPL-2.0-or-later \
  "https://github.com/cdrdao/cdrdao/releases/download/rel_1_2_6/cdrdao-1.2.6.tar.bz2" \
  0cf29e1183ffd8e4d167c403d7a6ea2109a2dd4cd97e61f80417cf1373220ff4

# --------------------------------------------------------------------------------------------
log "7-Zip 26.05 (7zz, universal)"
f=$(fetch https://github.com/ip7z/7zip/releases/download/26.05/7z2605-mac.tar.xz \
  12ef6830317743cfd3f0ab1724bde43fcc270d8b6d8564f8c12c2a2b7f993570 7z2605-mac.tar.xz)
mkdir -p "$WORK/7z"
tar -xJf "$f" -C "$WORK/7z"
install -m 755 "$WORK/7z/7zz" "$OUT/bin/7zz"
cp "$WORK/7z/License.txt" "$OUT/licenses/7-Zip-License.txt"
fetch https://github.com/ip7z/7zip/releases/download/26.05/7z2605-src.tar.xz \
  0b6080b8c3e7a579161d2d495224c0e51c467576cb4c7048b0ab485ef44cc22e 7z2605-src.tar.xz > /dev/null
source_row 7-Zip 26.05 "LGPL-2.1-or-later, BSD-3-Clause, unRAR restriction" \
  "https://github.com/ip7z/7zip/releases/download/26.05/7z2605-src.tar.xz" \
  0b6080b8c3e7a579161d2d495224c0e51c467576cb4c7048b0ab485ef44cc22e

# --------------------------------------------------------------------------------------------
log "checking that the tools only use system libraries"
bad=0
for b in "$OUT"/bin/*; do
  others=$(otool -L "$b" | tail -n +2 | awk '{print $1}' | grep -vE '^(/usr/lib/|/System/Library/)' || true)
  if [ -n "$others" ]; then
    echo "  $(basename "$b") links outside the system: $others" >&2
    bad=1
  fi
  minos=$(otool -l "$b" | awk '/LC_BUILD_VERSION/{f=1} f&&/minos/{print $2; exit}')
  printf '  %-10s %s, macOS %s+\n' "$(basename "$b")" "$(lipo -archs "$b")" "${minos:-?}"
done
[ "$bad" = 0 ]
