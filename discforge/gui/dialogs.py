"""Small dialogs: Dreamcast homebrew options and a read-only text viewer."""
from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QDialog, QLineEdit, QPlainTextEdit

from . import theme as T
from .widgets import Button, Text, Toggle, hbox, vbox


class HomebrewDialog(QDialog):
    def __init__(self, parent, opts: dict):
        super().__init__(parent)
        self.setWindowTitle("Homebrew options")
        self.setMinimumWidth(480)
        self.name = QLineEdit(opts.get("title") or "")
        self.name.setPlaceholderText("Taken from the file name")
        self.scr = QComboBox()
        self.scr.addItems(["Detect automatically", "Already scrambled (prepared for CD-R)", "Not scrambled yet"])
        self.scr.setCurrentIndex({None: 0, True: 1, False: 2}[opts.get("scrambled")])
        self.gd = Toggle(bool(opts.get("gd_lba")))
        ok = Button("Apply", "primary")
        ok.clicked.connect(self.accept)
        cancel = Button("Cancel", "secondary")
        cancel.clicked.connect(self.reject)
        self.setLayout(vbox(Text("Dreamcast homebrew", T.title()), T.LG,
                            Text("Title", T.small_medium(), "text2"), T.XS, self.name, T.LG,
                            Text("Program file", T.small_medium(), "text2"), T.XS, self.scr, T.LG,
                            hbox((vbox(Text("Boot like a GD-ROM", T.body_medium()),
                                       Text("For menus such as openMenu that expect the GD-ROM data position.",
                                            T.small(), "text3", wrap=True)), 1), T.LG, self.gd), T.XL,
                            hbox(None, cancel, T.SM, ok), margins=(T.XL, T.XL, T.XL, T.XL)))

    def values(self) -> dict:
        return {"title": self.name.text().strip() or None,
                "scrambled": {0: None, 1: True, 2: False}[self.scr.currentIndex()], "gd_lba": self.gd.isChecked()}


class TextDialog(QDialog):
    def __init__(self, parent, title: str, text: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 560)
        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        view.setFont(T.mono())
        close = Button("Close", "secondary")
        close.clicked.connect(self.accept)
        self.setLayout(vbox(Text(title, T.title()), T.MD, (view, 1), T.MD, hbox(None, close),
                            margins=(T.XL, T.XL, T.XL, T.XL)))
