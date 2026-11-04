#!/bin/sh
# Install DiscForge for the current user (no root needed for this part).
#   ./install.sh            launchers, desktop icon, app menu entry, Dolphin menu, icon, file types
#   ./install.sh --remove   undo all of that
# System packages it relies on:
#   sudo pacman -S --needed python-numpy pyside6 python-lz4 cdrtools cdrdao dvd+rw-tools
# Optional: mame-tools (.chd), 7zip (.7z/.rar), dolphin-emu (.rvz/.wia),
#           retroarch libretro-flycast (test Dreamcast discs in an emulator)
set -eu
HERE=$(cd "$(dirname "$0")" && pwd -P)
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
MENUS="$HOME/.local/share/kio/servicemenus"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
MIME="$HOME/.local/share/mime"
DESKTOP_DIR=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")
ICON="$ICONS/discforge.svg"

refresh() {
    update-mime-database "$MIME" >/dev/null 2>&1 || true
    update-desktop-database "$APPS" 2>/dev/null || true
    kbuildsycoca6 >/dev/null 2>&1 || true
}

# the app's previous name was "Dreamcast Burner" (dcburn): its launchers point at code that's gone
remove_old() {
    rm -f "$BIN/dcburn-gui" "$BIN/dcburn-action" "$APPS/dcburn.desktop" "$MENUS/dcburn.desktop" \
          "$ICONS/dcburn.svg" "$DESKTOP_DIR/dcburn.desktop"
    rm -rf "$HOME/.cache/dcburn"
}

if [ "${1:-}" = "--remove" ]; then
    remove_old
    rm -f "$BIN/discforge" "$BIN/discforge-gui" "$BIN/discforge-action" "$BIN/dcburn" "$APPS/discforge.desktop" \
          "$MENUS/discforge.desktop" "$MENUS/discforge-dreamcast.desktop" "$ICON" "$DESKTOP_DIR/discforge.desktop" \
          "$MIME/packages/discforge.xml"
    refresh
    echo "Removed. (The project folder $HERE and ~/.cache/discforge are left alone.)"
    exit 0
fi

remove_old
mkdir -p "$BIN" "$APPS" "$MENUS" "$ICONS" "$DESKTOP_DIR" "$MIME/packages"

cat > "$BIN/discforge" <<EOF
#!/bin/sh
# DiscForge command line (see: discforge --help)
PYTHONPATH="$HERE\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m discforge "\$@"
EOF

cat > "$BIN/discforge-gui" <<EOF
#!/bin/sh
# DiscForge window. A file given here opens in the already-running window if there is one.
PYTHONPATH="$HERE\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m discforge gui "\$@"
EOF

cat > "$BIN/discforge-action" <<'EOF'
#!/bin/sh
# Dolphin service-menu helper: run a discforge command, report problems with kdialog.
cmd=$1; shift
out=$("$HOME/.local/bin/discforge" "$cmd" "$@" 2>&1); rc=$?
title="DiscForge"
if [ $rc -ne 0 ]; then
    kdialog --title "$title" --error "$(printf '%s' "$out" | tail -n 12)" 2>/dev/null || printf '%s\n' "$out" >&2
elif [ "$cmd" = "cdi" ]; then
    kdialog --title "$title" --passivepopup "$(printf '%s' "$out" | tail -n 1)" 6 2>/dev/null || true
fi
exit $rc
EOF

cat > "$BIN/dcburn" <<'EOF'
#!/bin/sh
# Old name of DiscForge's command line.
exec "$HOME/.local/bin/discforge" "$@"
EOF
chmod +x "$BIN/discforge" "$BIN/discforge-gui" "$BIN/discforge-action" "$BIN/dcburn"

cp "$HERE/discforge/data/discforge.svg" "$ICON"

# file types the shared MIME database doesn't know yet
cat > "$MIME/packages/discforge.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="application/x-wbfs"><comment>Wii backup (WBFS)</comment><glob pattern="*.wbfs"/><glob pattern="*.wbf1"/></mime-type>
  <mime-type type="application/x-gcz"><comment>Dolphin compressed disc image (GCZ)</comment><glob pattern="*.gcz"/></mime-type>
  <mime-type type="application/x-zso"><comment>Compressed ISO (ZSO)</comment><glob pattern="*.zso"/></mime-type>
  <mime-type type="application/x-ecm"><comment>ECM-packed CD image</comment><glob pattern="*.ecm"/></mime-type>
  <mime-type type="application/x-clonecd"><comment>CloneCD disc image</comment><glob pattern="*.ccd"/></mime-type>
  <mime-type type="application/x-xbox-iso"><comment>Xbox disc image (XISO)</comment><glob pattern="*.xiso"/></mime-type>
</mime-info>
EOF

DC_TYPES="application/x-discjuggler-cd-image;application/x-gd-rom-cue;"
TYPES="${DC_TYPES}application/x-mame-chd;application/x-cue;application/x-cd-image;application/vnd.efi.iso;\
application/x-iso9660-image;application/x-compressed-iso;application/x-gamecube-rom;application/x-wii-rom;\
application/x-saturn-rom;application/x-sega-cd-rom;application/x-wbfs;application/x-gcz;application/x-zso;\
application/x-ecm;application/x-clonecd;application/x-xbox-iso;"
ENTRY="[Desktop Entry]
Type=Application
Version=1.5
Name=DiscForge
GenericName=Retro Game Disc Burner
Comment=Burn retro games, homebrew and software to CD and DVD in one click
Exec=$BIN/discforge-gui %f
Icon=$ICON
Terminal=false
Categories=AudioVideo;DiscBurning;
Keywords=burn;cd-r;dvd-r;dreamcast;playstation;ps1;ps2;gamecube;wii;xbox;saturn;sega cd;homebrew;iso;cue;chd;retro;
MimeType=$TYPES
StartupWMClass=discforge
StartupNotify=true
SingleMainWindow=true
Actions=open;

[Desktop Action open]
Name=Choose a Game to Burn…
Icon=document-open
Exec=$BIN/discforge-gui"

printf '%s\n' "$ENTRY" > "$APPS/discforge.desktop"
printf '%s\n' "$ENTRY" > "$DESKTOP_DIR/discforge.desktop"
chmod +x "$DESKTOP_DIR/discforge.desktop"     # Plasma only launches executable desktop files

cat > "$MENUS/discforge.desktop" <<EOF
[Desktop Entry]
Type=Service
MimeType=$TYPES
Actions=burn;
X-KDE-Submenu=DiscForge
Icon=$ICON

[Desktop Action burn]
Name=Burn to a disc…
Icon=$ICON
Exec=$BIN/discforge-gui %f
EOF
cat > "$MENUS/discforge-dreamcast.desktop" <<EOF
[Desktop Entry]
Type=Service
MimeType=$DC_TYPES
Actions=test;cdi;
X-KDE-Submenu=DiscForge
Icon=$ICON

[Desktop Action test]
Name=Try in the Dreamcast emulator
Icon=media-playback-start
Exec=$BIN/discforge-action test %f

[Desktop Action cdi]
Name=Save as a selfboot CDI file
Icon=document-save-as
Exec=$BIN/discforge-action cdi %f
EOF
chmod +x "$MENUS/discforge.desktop" "$MENUS/discforge-dreamcast.desktop"

refresh
xdg-mime default discforge.desktop application/x-discjuggler-cd-image application/x-gd-rom-cue 2>/dev/null || true
echo "Installed DiscForge:"
echo "  - on your desktop ($DESKTOP_DIR/discforge.desktop) and in the app menu"
echo "  - right-click a game file in Dolphin for the 'DiscForge' menu"
echo "  - command line: discforge --help"
