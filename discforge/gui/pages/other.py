"""Library, Drives, History and Settings pages."""
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHeaderView, QPlainTextEdit, QScrollArea, QSizePolicy,
                               QTableWidget, QTableWidgetItem, QWidget)

from ... import platforms as P
from .. import icons as I
from .. import theme as T
from ..widgets import (Badge, Button, Callout, Card, Elided, EmptyState, IconButton, SearchBox,
                       Segmented, Separator, Spinner, Text, Tile, TileGrid, Toggle, clear_layout, hbox, vbox, widget_of)
from .burn import DiscGlyph


def scroll_of(widget: QWidget) -> QScrollArea:
    s = QScrollArea()
    s.setWidgetResizable(True)
    s.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    s.setWidget(widget)
    return s


# --------------------------------------------------------------------------

class LibraryPage(QWidget):
    SORTS = [("recent", "Recently changed"), ("name", "Name"), ("console", "Console"), ("size", "Size")]
    SEARCH_W = 260                         # narrowest comfortable search box

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.search = SearchBox("Search your games")
        self.search.textChanged.connect(lambda _t: win.fill_library())
        self.console = QComboBox()
        self.console.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.console.currentIndexChanged.connect(lambda _i: win.fill_library())
        self.sort = QComboBox()
        for key, label in self.SORTS:
            self.sort.addItem(label, key)
        self.sort.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.sort.currentIndexChanged.connect(lambda _i: win.fill_library())
        self.spin = Spinner(16)
        self.spin.hide()
        self.b_refresh = IconButton("refresh-cw", "Look for games again", 36)
        self.b_refresh.clicked.connect(win.start_scan)
        self.b_folder = Button("Add folder…", "secondary", "folder")
        self.b_folder.clicked.connect(win.add_library_folder)
        # a small hard minimum, so this row never holds the window open; below SEARCH_W the search
        # moves to a row of its own instead of getting cramped
        self.search.setMinimumWidth(80)
        self.bar = QWidget()                 # one row, or the search above the rest on a narrow page
        self._narrow = None
        self._arrange_bar(False)
        self.count = Text("", T.small(), "text3")
        self.grid_w = QWidget()
        self.flow = TileGrid(self.grid_w, spacing=T.LG, min_w=160)
        self.flow.setContentsMargins(0, 0, 0, T.LG)
        self.scroll = scroll_of(self.grid_w)
        self.empty = EmptyState("library", "No games found yet",
                                "DiscForge looks in your Downloads, Documents, Games and ROMs folders. Add the "
                                "folder where you keep games, or drop a game on the window.",
                                [self._b("Add folder…", win.add_library_folder),
                                 self._b("Open a game…", win.browse, "primary")])
        self.setLayout(vbox(self.bar, T.MD, self.count, T.SM, (self.scroll, 1), self.empty,
                            margins=(T.XL, T.SM, T.XL, 0)))
        self.empty.hide()

    def _arrange_bar(self, narrow: bool):
        if narrow == self._narrow:
            return
        self._narrow = narrow
        old = self.bar.layout()
        if old is not None:
            clear_layout(old, keep_widgets=True)
            QWidget().setLayout(old)             # hand the old layout to a throwaway widget, which deletes it
        tools = (self.console, T.SM, self.sort, T.MD, self.spin, T.SM, self.b_refresh, T.SM, self.b_folder)
        if narrow:
            lay = vbox(self.search, T.SM, hbox(*tools[:3], None, *tools[4:]))
        else:
            lay = hbox((self.search, 1), T.SM, *tools)
        lay.setContentsMargins(0, 0, 0, 0)
        self.bar.setLayout(lay)

    def resizeEvent(self, e):  # noqa: N802
        m = self.layout().contentsMargins()
        room = self.width() - m.left() - m.right()
        need = (self.SEARCH_W + self.console.sizeHint().width() + self.sort.sizeHint().width()
                + self.b_refresh.sizeHint().width() + self.b_folder.sizeHint().width() + 4 * T.SM + T.MD
                + (self.spin.sizeHint().width() + T.SM))
        self._arrange_bar(room < need)
        super().resizeEvent(e)

    @staticmethod
    def _b(text, cb, variant="secondary"):
        b = Button(text, variant)
        b.clicked.connect(cb)
        return b

    def set_consoles(self, counts: dict[str, int]):
        cur = self.console.currentData() or "all"
        self.console.blockSignals(True)
        self.console.clear()
        self.console.addItem(f"All consoles ({sum(counts.values())})", "all")
        order = [p.id for p in P.PLATFORMS.values()]
        for k in sorted(counts, key=lambda x: order.index(x) if x in order else 99):
            name = P.get(k).name if k in P.PLATFORMS else "Other"
            self.console.addItem(f"{name} ({counts[k]})", k)
        i = self.console.findData(cur)
        self.console.setCurrentIndex(max(0, i))
        self.console.blockSignals(False)

    def set_tiles(self, tiles: list[Tile], total: int):
        clear_layout(self.flow)
        for t in tiles:
            self.flow.addWidget(t)
        self.grid_w.updateGeometry()
        self.count.setText(f"{len(tiles)} of {total} games" if len(tiles) != total else
                           f"{total} game{'s' * (total != 1)}")
        self.empty.setVisible(total == 0)
        self.scroll.setVisible(total > 0)
        self.count.setVisible(total > 0)


# --------------------------------------------------------------------------

class DriveRow(Card):
    def __init__(self, win, info):
        super().__init__(padding=18)
        self.win, self.info = win, info
        self.glyph = DiscGlyph(64)
        self.name = Elided(info.name, T.headline(), "text")
        self.dev = Elided(info.dev, T.caption(), "text3")
        self.caps = hbox(spacing=T.XS)
        self.status = Text("", T.small(), "text2", wrap=True)
        self.actions = hbox(spacing=T.SM)
        self.lay.addLayout(hbox(self.glyph, T.LG, (vbox(self.name, 2, self.dev, T.SM, self.caps, T.SM, self.status), 1),
                                T.LG, (self.actions, 0)))
        self.refresh(info)

    def refresh(self, info):
        sig = (info.name, info.dev, tuple(getattr(info, "writes", ()) or ()), info.state,
               getattr(info.media, "kind", None), getattr(info.media, "capacity", None), self.win.current_dev,
               self.win.drive_count(), T.P.name)
        if sig == getattr(self, "_sig", None):
            return
        self._sig = sig
        self.info = info
        self.name.setText(info.name)
        self.dev.setText(info.dev)
        clear_layout(self.caps)
        writes = list(getattr(info, "writes", ()) or ())
        if writes:
            groups = []
            if "CD-R" in writes:
                groups.append("CD-R" + ("/RW" if "CD-RW" in writes else ""))
            dvd = [w for w in writes if w in ("DVD-R", "DVD+R")]
            if dvd:
                groups.append("DVD±R" if len(dvd) == 2 else dvd[0])
            dl = [w for w in writes if w.endswith("DL")]
            if dl:
                groups.append("Dual layer")
            if "BD-R" in writes:
                groups.append("Blu-ray")
            for g in groups:
                self.caps.addWidget(Badge(g, T.P.text2, dot=False))
        else:
            self.caps.addWidget(Badge("Capabilities unknown", T.P.text3, dot=False))
        self.caps.addStretch(1)
        clear_layout(self.actions)
        st = info.state
        dye = self.win.dye_for(info)
        self.glyph.set({"ready": "ready", "used": "used", "rw": "used", "reading": "reading",
                        "busy": "reading"}.get(st, "empty"), dye)
        self.status.setText(self.win.drive_status_text(info))
        if st == "noperm":
            self._act("Allow access", lambda: self.win.drive_action(info.dev, "perm"), "primary")
        if st == "rw":
            self._act("Erase disc", lambda: self.win.drive_action(info.dev, "erase"), "danger")
        self._act("Eject", lambda: self.win.drive_action(info.dev, "eject"), icon="eject")
        if self.win.drive_count() > 1:
            sel = self.win.current_dev == info.dev
            b = self._act("In use" if sel else "Use this drive", lambda: self.win.choose_drive(info.dev),
                          "secondary" if sel else "primary")
            b.setEnabled(not sel)

    def _act(self, text, cb, variant="secondary", icon=None):
        b = Button(text, variant, icon, "sm")
        b.clicked.connect(cb)
        self.actions.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        return b


class DrivesPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.rows_box = vbox(spacing=T.MD)
        self.list_w = widget_of(vbox(self.rows_box, None))
        self.help = Callout("Any USB or internal DVD writer works: DVD writers also burn CD-R. DiscForge checks "
                            "the disc whenever one is inserted.", "info")
        self.b_refresh = Button("Look again", "secondary", "refresh-cw")
        self.b_refresh.clicked.connect(win.refresh_drives)
        self.empty = EmptyState("hard-drive", "No disc writer found",
                                "Plug in a CD/DVD writer (USB writers work great). It shows up here as soon as "
                                "it's connected.", [self._b()])
        self.setLayout(vbox(hbox((self.help, 1), T.MD, self.b_refresh), T.LG, (scroll_of(self.list_w), 1), self.empty,
                            margins=(T.XL, T.SM, T.XL, T.XL)))
        self.rows: dict[str, DriveRow] = {}

    def _b(self):
        b = Button("Look again", "primary", "refresh-cw")
        b.clicked.connect(self.win.refresh_drives)
        return b

    def set_drives(self, drives: list):
        devs = [d.dev for d in drives]
        for dev in list(self.rows):
            if dev not in devs:
                row = self.rows.pop(dev)
                self.rows_box.removeWidget(row)
                row.deleteLater()
        for d in drives:
            if d.dev in self.rows:
                self.rows[d.dev].refresh(d)
            else:
                row = DriveRow(self.win, d)
                self.rows[d.dev] = row
                self.rows_box.addWidget(row)
        self.empty.setVisible(not drives)
        self.list_w.parentWidget().parentWidget().setVisible(bool(drives))


# --------------------------------------------------------------------------

class HistoryPage(QWidget):
    COLS = ["When", "Game", "Console", "Disc", "Result"]

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.verticalHeader().setDefaultSectionSize(40)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selected)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(T.mono())
        self.log.setPlaceholderText("Select a burn to see its log.")
        self.log.setFixedHeight(170)
        self.b_clear = Button("Clear history", "ghost", "trash-2", "sm")
        self.b_clear.clicked.connect(win.clear_history)
        self.card = Card(padding=8)
        self.card.lay.addWidget(self.table)
        self.empty = EmptyState("history", "No burns yet", "Every disc you burn is listed here with its result "
                                "and the full log, so you can always check what happened.")
        self.setLayout(vbox((self.card, 1), T.MD, self.log, T.SM, hbox(None, self.b_clear), self.empty,
                            margins=(T.XL, T.SM, T.XL, T.XL)))
        self.entries: list[dict] = []

    def set_entries(self, entries: list[dict]):
        self.entries = entries
        self.table.setRowCount(len(entries))
        for row, e in enumerate(entries):
            when = dt.datetime.fromtimestamp(e.get("time", 0)).strftime("%d %b %Y, %H:%M")
            res = e.get("result", "")
            label = {"ok": "Ready · checked" if e.get("verified") else "Ready", "verify": "Didn't check out",
                     "failed": "Failed", "cancelled": "Stopped", "practice": "Practice run"}.get(res, res)
            vals = [when, e.get("title", ""), P.get(e.get("platform", "cd")).name, e.get("media", ""), label]
            for col, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setFont(T.small_medium() if col == 1 else T.small())
                if col == 4:
                    it.setForeground({"ok": QColor_(T.P.ok), "practice": QColor_(T.P.info)}.get(res, QColor_(
                        T.P.warn if res in ("verify", "cancelled") else T.P.bad)))
                self.table.setItem(row, col, it)
        has = bool(entries)
        for w in (self.card, self.log, self.b_clear):
            w.setVisible(has)
        self.empty.setVisible(not has)
        self.log.clear()

    def _selected(self):
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self.log.setPlainText(self.entries[rows[0].row()].get("log", ""))


def QColor_(hexcol: str):  # noqa: N802 - tiny helper used above
    from PySide6.QtGui import QColor
    return QColor(hexcol)


# --------------------------------------------------------------------------

class SettingRow(QWidget):
    def __init__(self, title: str, text: str, control: QWidget):
        super().__init__()
        t = Text(title, T.body_medium(), "text")
        d = Text(text, T.small(), "text3", wrap=True)
        self.setLayout(hbox((vbox(t, 3, d), 1), T.XL, (control, 0, Qt.AlignmentFlag.AlignVCenter),
                            margins=(0, T.MD, 0, T.MD)))


class Section(Card):
    def __init__(self, title: str, subtitle: str = ""):
        super().__init__(padding=20)
        self.lay.setSpacing(0)
        self.lay.addWidget(Text(title, T.headline(), "text"))
        if subtitle:
            self.lay.addSpacing(T.XS)
            self.lay.addWidget(Text(subtitle, T.small(), "text3", wrap=True))
        self.lay.addSpacing(T.SM)
        self._first = True

    def row(self, title: str, text: str, control: QWidget) -> QWidget:
        if not self._first:
            self.lay.addWidget(Separator())
        self._first = False
        r = SettingRow(title, text, control)
        self.lay.addWidget(r)
        return r


class SettingsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        s = win.settings
        self.t_verify = Toggle(s.value("verify", True, bool))
        self.t_eject = Toggle(s.value("eject", True, bool))
        self.t_simulate = Toggle(s.value("simulate", False, bool))
        self.speed = Segmented([("safe", "Safest"), ("balanced", "Balanced"), ("fast", "Fastest")],
                               s.value("speed", "safe"))
        self.t_libcrypt = Toggle(s.value("libcrypt", True, bool))
        self.t_region = Toggle(s.value("dc_region_free", True, bool))
        self.t_vga = Toggle(s.value("dc_vga", True, bool))
        self.theme = Segmented([("system", "System"), ("dark", "Dark"), ("light", "Light")], s.value("theme", "system"))
        self.t_motion = Toggle(s.value("reduce_motion", False, bool))
        self.t_intro = Toggle(s.value("intro", True, bool))
        for key, t in (("verify", self.t_verify), ("eject", self.t_eject), ("simulate", self.t_simulate),
                       ("libcrypt", self.t_libcrypt), ("dc_region_free", self.t_region), ("dc_vga", self.t_vga),
                       ("reduce_motion", self.t_motion), ("intro", self.t_intro)):
            t.toggled.connect(lambda on, k=key: win.setting_changed(k, on))
        self.speed.changed.connect(lambda v: win.setting_changed("speed", v))
        self.theme.changed.connect(lambda v: win.setting_changed("theme", v))

        burning = Section("Burning")
        burning.row("Check discs after burning", "Reads the whole disc back and compares it with the game. "
                    "Strongly recommended.", self.t_verify)
        burning.row("Burn speed", "Safest uses the slow speed old console lasers read best. Fastest uses "
                    "whatever the drive allows.", self.speed)
        burning.row("Eject when done", "Opens the tray when the disc is ready.", self.t_eject)
        burning.row("Practice runs only", "Everything happens except the laser writing — a dry run to check the "
                    "drive (CD-R and DVD-R).", self.t_simulate)
        consoles = Section("Consoles")
        consoles.row("Remove LibCrypt protection", "PAL PlayStation games with LibCrypt stop working on a burned "
                     "copy. DiscForge patches a copy of the game automatically (229 releases supported); your "
                     "original file is never changed.", self.t_libcrypt)
        consoles.row("Dreamcast: play on any region's console", "Patches the boot header of Dreamcast discs "
                     "to be region-free.", self.t_region)
        consoles.row("Dreamcast: allow the VGA box", "Lets Dreamcast games run through a VGA adapter.", self.t_vga)

        self.folders = vbox(spacing=T.XS)
        add = Button("Add folder…", "secondary", "plus", "sm")
        add.clicked.connect(win.add_library_folder)
        lib = Section("Library", "Where DiscForge looks for games.")
        lib.lay.addSpacing(T.SM)
        lib.lay.addLayout(self.folders)
        lib.lay.addSpacing(T.SM)
        lib.lay.addLayout(hbox(add, None))
        appearance = Section("Appearance")
        appearance.row("Theme", "Follow the system, or always dark or light.", self.theme)
        appearance.row("Reduce motion", "Keeps animations to a minimum.", self.t_motion)
        appearance.row("Intro animation", "Plays the short DiscForge intro at start-up.", self.t_intro)

        self.cache = Text("", T.small(), "text3")
        b_cache = Button("Clear", "secondary", "trash-2", "sm")
        b_cache.clicked.connect(win.clear_cache)
        storage = Section("Storage")
        storage.row("Prepared games", "Unpacked and converted images are kept for a while so a second copy "
                    "burns straight away.", widget_of(hbox(self.cache, T.MD, b_cache)))

        self.tools_box = vbox(spacing=T.XS)
        self.b_tools = Button("Install missing tools", "secondary", "package", "sm")
        self.b_tools.clicked.connect(win.install_tools)
        tools = Section("Burning tools", "The programs DiscForge uses to talk to your drive.")
        tools.lay.addSpacing(T.SM)
        tools.lay.addLayout(self.tools_box)
        tools.lay.addSpacing(T.MD)
        tools.lay.addLayout(hbox(self.b_tools, None))

        about = Section("About")
        self.about = Text("", T.small(), "text2", wrap=True)
        b_lic = Button("Licences", "ghost", "file", "sm")
        b_lic.clicked.connect(win.show_licences)
        b_site = Button("Project page", "ghost", "external-link", "sm")
        b_site.clicked.connect(win.open_project_page)
        about.lay.addSpacing(T.SM)
        about.lay.addWidget(self.about)
        about.lay.addSpacing(T.MD)
        about.lay.addLayout(hbox(b_site, T.SM, b_lic, None))
        col = vbox(burning, T.LG, consoles, T.LG, lib, T.LG, appearance, T.LG, storage, T.LG, tools, T.LG, about, None)
        inner = widget_of(col)
        inner.setMaximumWidth(880)
        inner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row = hbox(margins=(T.XL, T.SM, T.XL, T.XL))
        row.addWidget(inner, 1)
        row.addStretch(0)                       # only takes what's left once the column is at its widest
        self.setLayout(vbox(scroll_of(widget_of(row))))

    def set_folders(self, folders: list[str]):
        clear_layout(self.folders)
        for f in folders:
            name = Elided(f, T.small(), "text")
            rm = IconButton("x", "Stop looking here", 28)
            rm.clicked.connect(lambda _c=False, path=f: self.win.remove_library_folder(path))
            row = widget_of(hbox(I_folder(), T.SM, (name, 1), T.SM, rm))
            self.folders.addWidget(row)

    def set_tools(self, rows: list[tuple[str, str, bool]], missing: bool):
        clear_layout(self.tools_box)
        for name, where, ok in rows:
            dot = Badge("Ready" if ok else "Missing", T.P.ok if ok else T.P.warn)
            self.tools_box.addLayout(hbox(Text(name, T.small_medium(), "text"), T.SM, (Elided(where, T.caption(),
                                                                                              "text3"), 1), T.SM, dot))
        self.b_tools.setVisible(missing)


def I_folder() -> QWidget:  # noqa: N802
    w = QWidget()
    w.setFixedSize(18, 18)
    w.paintEvent = lambda _e: I.paint(QPainter(w), "folder", QRectF(1, 1, 16, 16), T.P.text3)
    return w
