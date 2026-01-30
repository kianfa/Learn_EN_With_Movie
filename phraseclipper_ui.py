# phraseclipper_ui.py
from __future__ import annotations

import os
import re
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from proglog import ProgressBarLogger
from PyQt5.QtCore import QThread, pyqtSignal, QSettings, QUrl, Qt
from PyQt5.QtGui import QDesktopServices, QTextCursor, QFont
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFileDialog,
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QLineEdit, QPushButton, QListWidget, QListWidgetItem,
    QLabel, QDoubleSpinBox, QCheckBox, QMessageBox,
    QTextEdit, QScrollArea, QFrame, QSplitter,
    QComboBox, QToolButton, QSizePolicy
)

from phraseclipper_core import (
    Settings,
    Match,
    find_phrase_matches,
    locate_video_for_subtitle,
    export_compilation,
    make_temp_preview,
)

SRT_EXTS = {".srt", ".txt"}


def sanitize_filename(name: str) -> str:
    name = name.strip()
    if not name:
        return "output"
    name = re.sub(r'[<>:"/\\|?*]+', "_", name)
    name = name.strip(" .")
    return name or "output"


LIGHT_QSS = """
QWidget { background: #f6f7fb; color: #111827; font-size: 14px; }
QFrame#Card { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 14px; }
QLabel#Hint { color: #6b7280; }

QLineEdit, QTextEdit, QListWidget, QComboBox {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 10px;
    padding: 6px;
    selection-background-color: #dbeafe;
}
QTextEdit { font-family: Consolas, "Cascadia Mono", monospace; font-size: 12px; padding: 6px; }

QPushButton, QToolButton {
    background: #111827;
    color: #ffffff;
    border: 0px;
    border-radius: 10px;
    padding: 7px 12px;
    font-weight: 700;
    min-height: 32px;
}
QPushButton:hover, QToolButton:hover { background: #1f2937; }
QPushButton:disabled { background: #9ca3af; color: #f3f4f6; }

QPushButton#Secondary, QToolButton#Secondary {
    background: #ffffff;
    color: #111827;
    border: 1px solid #e5e7eb;
}
QPushButton#Secondary:hover, QToolButton#Secondary:hover { background: #f3f4f6; }

QToolButton#PadClear {
    background: transparent;
    color: #6b7280;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    padding: 2px 6px;
    font-size: 12px;
    min-height: 22px;
}
QToolButton#PadClear:hover {
    background: #f3f4f6;
    color: #111827;
}

QCheckBox { spacing: 8px; }
QScrollArea { border: 0px; background: transparent; }

/* tighter match list rows */
QListWidget { padding: 6px; }
QListWidget::item { padding: 3px 6px; margin: 1px 0px; border-radius: 8px; }
QListWidget::item:selected { background: #dbeafe; color: #111827; }
"""

DARK_QSS = """
QWidget { background: #0b0f14; color: #e5e7eb; font-size: 14px; }
QFrame#Card { background: #10161d; border: 1px solid #252f3b; border-radius: 14px; }
QLabel#Hint { color: #a7b0bd; }

QLineEdit, QTextEdit, QListWidget, QComboBox {
    background: #0b0f14;
    border: 1px solid #252f3b;
    border-radius: 10px;
    padding: 6px;
    selection-background-color: #3a2a17;
}
QTextEdit { font-family: Consolas, "Cascadia Mono", monospace; font-size: 12px; padding: 6px; }

QPushButton, QToolButton {
    background: #ff8a00;
    color: #111827;
    border: 0px;
    border-radius: 10px;
    padding: 7px 12px;
    font-weight: 800;
    min-height: 32px;
}
QPushButton:hover, QToolButton:hover { background: #ff9d2e; }
QPushButton:disabled { background: #3a4452; color: #cbd5e1; }

QPushButton#Secondary, QToolButton#Secondary {
    background: #10161d;
    color: #e5e7eb;
    border: 1px solid #252f3b;
    font-weight: 700;
}
QPushButton#Secondary:hover, QToolButton#Secondary:hover { background: #141c25; }

QToolButton#PadClear {
    background: transparent;
    color: #a7b0bd;
    border: 1px solid #252f3b;
    border-radius: 8px;
    padding: 2px 6px;
    font-size: 12px;
    min-height: 22px;
}
QToolButton#PadClear:hover {
    background: #141c25;
    color: #ffffff;
    border-color: #ff8a00;
}

QCheckBox { spacing: 8px; }
QScrollArea { border: 0px; background: transparent; }

/* tighter match list rows */
QListWidget { padding: 6px; }
QListWidget::item { padding: 3px 6px; margin: 1px 0px; border-radius: 8px; }
QListWidget::item:selected { background: #3a2a17; color: #ffffff; }
"""


class QtMoviePyLogger10(ProgressBarLogger):
    """
    Emits progress at 0/10/20/.../100 and NEVER goes backwards.
    MoviePy sometimes resets bars for audio; we ignore regressions.
    """
    def __init__(self, pct_emit, main_bar: str = "t"):
        super().__init__()
        self._pct_emit = pct_emit
        self._main_bar = main_bar
        self._last_bucket = -10

    def reset(self):
        self._last_bucket = -10

    def bars_callback(self, bar, attr, value, old_value=None):
        if bar != self._main_bar:
            return
        try:
            total = self.bars.get(bar, {}).get("total", None)
            if not total:
                return
            pct = int((value / total) * 100)
            bucket = (pct // 10) * 10
            if bucket < 0:
                bucket = 0
            if bucket > 100:
                bucket = 100
            if bucket > self._last_bucket:
                self._last_bucket = bucket
                self._pct_emit(bucket)
        except Exception:
            pass

    def message(self, s):
        return


class ScanWorker(QThread):
    progress = pyqtSignal(str)
    done = pyqtSignal(list)      # list[Match]
    failed = pyqtSignal(str)

    def __init__(self, subtitle_dirs: list[Path], phrase: str, case_insensitive: bool):
        super().__init__()
        self.subtitle_dirs = subtitle_dirs
        self.phrase = phrase
        self.case_insensitive = case_insensitive

    def run(self):
        try:
            if not self.subtitle_dirs:
                raise ValueError("No subtitle folders selected.")
            for d in self.subtitle_dirs:
                if not d.exists():
                    raise FileNotFoundError(f"Subtitle folder does not exist: {d}")
            if not self.phrase.strip():
                raise ValueError("Phrase is empty.")

            files_set = set()
            for d in self.subtitle_dirs:
                for p in d.rglob("*"):
                    if p.is_file() and p.suffix.lower() in SRT_EXTS:
                        files_set.add(p)
            files = sorted(files_set)

            if not files:
                raise FileNotFoundError("No .srt/.txt files found in the selected subtitle folders.")

            total = len(files)
            self.progress.emit(f"Scanning {total} subtitle files...")

            max_workers = min(16, (os.cpu_count() or 4) * 2)
            matches: list[Match] = []
            done_count = 0

            def job(p: Path):
                return find_phrase_matches(p, self.phrase, case_insensitive=self.case_insensitive)

            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = [ex.submit(job, f) for f in files]
                for fut in as_completed(futures):
                    if self.isInterruptionRequested():
                        self.progress.emit("Scan cancelled.")
                        return
                    res = fut.result()
                    if res:
                        matches.extend(res)
                    done_count += 1
                    if done_count % 150 == 0 or done_count == total:
                        self.progress.emit(f"Scanned {done_count}/{total} files...")

            self.done.emit(matches)
        except Exception:
            self.failed.emit(traceback.format_exc())


class ResolveWorker(QThread):
    progress = pyqtSignal(str)
    resolved_one = pyqtSignal(int, str)  # index, video_name or "" if not found
    done = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, matches: list[Match], video_roots: list[Path]):
        super().__init__()
        self.matches = matches
        self.video_roots = video_roots

    def run(self):
        try:
            total = len(self.matches)
            self.progress.emit(f"Resolving videos for {total} matches...")

            for i, m in enumerate(self.matches):
                if self.isInterruptionRequested():
                    self.progress.emit("Video resolution cancelled.")
                    return

                vp = m.video_file or locate_video_for_subtitle(m.subtitle_file, self.video_roots)
                if vp:
                    self.matches[i] = Match(
                        subtitle_file=m.subtitle_file,
                        start=m.start,
                        end=m.end,
                        text=m.text,
                        video_file=vp
                    )
                    self.resolved_one.emit(i, vp.name)
                else:
                    self.resolved_one.emit(i, "")

                if (i + 1) % 60 == 0 or (i + 1) == total:
                    self.progress.emit(f"Resolved {i + 1}/{total} matches...")

            self.done.emit()
        except Exception:
            self.failed.emit(traceback.format_exc())


class ExportWorker(QThread):
    progress = pyqtSignal(str)
    failed = pyqtSignal(str)
    done = pyqtSignal(str)
    render_pct = pyqtSignal(int)

    def __init__(
        self,
        selected_matches: list[Match],
        video_roots: list[Path],
        output_path: Path,
        settings: Settings,
        pad_overrides: dict[int, tuple[float, float]],
    ):
        super().__init__()
        self.selected_matches = selected_matches
        self.video_roots = video_roots
        self.output_path = output_path
        self.settings = settings
        self.pad_overrides = pad_overrides

    def run(self):
        try:
            def cb(cur, total, msg):
                self.progress.emit(f"[{cur}/{total}] {msg}")

            logger = QtMoviePyLogger10(self.render_pct.emit, main_bar="t")
            logger.reset()

            export_compilation(
                matches=self.selected_matches,
                video_roots=self.video_roots,
                output_path=self.output_path,
                settings=self.settings,
                offsets=None,
                pad_overrides=self.pad_overrides,
                on_progress=cb,
                moviepy_logger=logger,
            )

            self.done.emit(str(self.output_path))
        except Exception:
            self.failed.emit(traceback.format_exc())


class PreviewWorker(QThread):
    progress = pyqtSignal(str)
    failed = pyqtSignal(str)
    done = pyqtSignal(str)
    render_pct = pyqtSignal(int)

    def __init__(
        self,
        match: Match,
        video_roots: list[Path],
        settings: Settings,
        pad_before_override: float | None,
        pad_after_override: float | None,
    ):
        super().__init__()
        self.match = match
        self.video_roots = video_roots
        self.settings = settings
        self.pad_before_override = pad_before_override
        self.pad_after_override = pad_after_override

    def run(self):
        try:
            self.progress.emit("Rendering preview...")

            logger = QtMoviePyLogger10(self.render_pct.emit, main_bar="t")
            logger.reset()

            tmp = make_temp_preview(
                self.match,
                self.video_roots,
                self.settings,
                pad_before_override=self.pad_before_override,
                pad_after_override=self.pad_after_override,
                moviepy_logger=logger,
            )
            self.done.emit(str(tmp))
        except Exception:
            self.failed.emit(traceback.format_exc())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.settings_store = QSettings("RandDCenter", "PhraseClipper")

        # restore window geometry (size + position)
        geo = self.settings_store.value("window_geometry", None)
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1020, 640)

        self.setWindowTitle("PhraseClipper")

        self.matches: list[Match] = []
        self.video_roots: list[Path] = []
        self.subtitle_roots: list[Path] = []
        self.pad_overrides: dict[int, tuple[float, float]] = {}

        self._render_progress_block = None
        self._preview_progress_block = None

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        # top header
        top = QHBoxLayout()
        top.setSpacing(8)

        title = QLabel("PhraseClipper")
        tf = QFont()
        tf.setPointSize(15)
        tf.setBold(True)
        title.setFont(tf)

        top.addWidget(title)
        top.addStretch(1)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Dark", "Light"])
        self.theme_combo.setFixedWidth(120)
        self.theme_combo.currentIndexChanged.connect(self.on_theme_changed)

        top.addWidget(QLabel("Theme:"))
        top.addWidget(self.theme_combo)
        outer.addLayout(top)

        # compact settings card (paths etc.)
        card = QFrame()
        card.setObjectName("Card")
        grid = QGridLayout(card)
        grid.setContentsMargins(10, 10, 10, 10)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)

        # subtitles
        self.subtitle_roots_edit = QLineEdit()
        self.subtitle_roots_edit.setReadOnly(True)
        b_add_sub = QToolButton(); b_add_sub.setText("Add"); b_add_sub.setObjectName("Secondary"); b_add_sub.clicked.connect(self.add_subtitle_root)
        b_clear_sub = QToolButton(); b_clear_sub.setText("Clear"); b_clear_sub.setObjectName("Secondary"); b_clear_sub.clicked.connect(self.clear_subtitle_roots)
        sub_row = QHBoxLayout(); sub_row.setSpacing(6)
        sub_row.addWidget(self.subtitle_roots_edit, 1)
        sub_row.addWidget(b_add_sub)
        sub_row.addWidget(b_clear_sub)

        grid.addWidget(QLabel("Subtitles:"), 0, 0)
        grid.addLayout(sub_row, 0, 1, 1, 3)

        # videos
        self.video_roots_edit = QLineEdit()
        self.video_roots_edit.setReadOnly(True)
        b_add_vid = QToolButton(); b_add_vid.setText("Add"); b_add_vid.setObjectName("Secondary"); b_add_vid.clicked.connect(self.add_video_root)
        b_clear_vid = QToolButton(); b_clear_vid.setText("Clear"); b_clear_vid.setObjectName("Secondary"); b_clear_vid.clicked.connect(self.clear_video_roots)
        vid_row = QHBoxLayout(); vid_row.setSpacing(6)
        vid_row.addWidget(self.video_roots_edit, 1)
        vid_row.addWidget(b_add_vid)
        vid_row.addWidget(b_clear_vid)

        grid.addWidget(QLabel("Videos:"), 1, 0)
        grid.addLayout(vid_row, 1, 1, 1, 3)

        # phrase + options
        self.phrase = QLineEdit()
        self.phrase.setPlaceholderText("Phrase…")
        self.case_insensitive = QCheckBox("Case-insensitive")
        self.case_insensitive.setChecked(True)
        phrase_row = QHBoxLayout(); phrase_row.setSpacing(8)
        phrase_row.addWidget(self.phrase, 1)
        phrase_row.addWidget(self.case_insensitive)

        grid.addWidget(QLabel("Search:"), 2, 0)
        grid.addLayout(phrase_row, 2, 1, 1, 3)

        # defaults
        self.pad_before = QDoubleSpinBox()
        self.pad_before.setRange(-30.0, 30.0)
        self.pad_before.setSingleStep(0.1)
        self.pad_before.setValue(0.5)

        self.pad_after = QDoubleSpinBox()
        self.pad_after.setRange(-30.0, 30.0)
        self.pad_after.setSingleStep(0.1)
        self.pad_after.setValue(1.5)

        self.burn_subs = QCheckBox("Burn subs"); self.burn_subs.setChecked(True)
        self.add_black = QCheckBox("Black gaps"); self.add_black.setChecked(True)

        row3 = QHBoxLayout(); row3.setSpacing(8)
        row3.addWidget(QLabel("Before:"))
        row3.addWidget(self.pad_before)
        row3.addWidget(QLabel("After:"))
        row3.addWidget(self.pad_after)
        row3.addSpacing(8)
        row3.addWidget(self.burn_subs)
        row3.addWidget(self.add_black)
        row3.addStretch(1)

        grid.addWidget(QLabel("Defaults:"), 3, 0)
        grid.addLayout(row3, 3, 1, 1, 3)

        # black clip + output folder
        self.black_path = QLineEdit()
        self.black_path.setPlaceholderText("Optional black clip…")
        b_black = QToolButton(); b_black.setText("Pick"); b_black.setObjectName("Secondary"); b_black.clicked.connect(self.pick_black_clip)
        black_row = QHBoxLayout(); black_row.setSpacing(6)
        black_row.addWidget(self.black_path, 1)
        black_row.addWidget(b_black)

        self.output_dir = QLineEdit()
        self.output_dir.setPlaceholderText("Output folder…")
        b_out = QToolButton(); b_out.setText("Pick"); b_out.setObjectName("Secondary"); b_out.clicked.connect(self.pick_output_folder)
        out_row = QHBoxLayout(); out_row.setSpacing(6)
        out_row.addWidget(self.output_dir, 1)
        out_row.addWidget(b_out)

        grid.addWidget(QLabel("Black:"), 4, 0)
        grid.addLayout(black_row, 4, 1)
        grid.addWidget(QLabel("Output:"), 4, 2)
        grid.addLayout(out_row, 4, 3)

        # action buttons row (Scan beside Stop resolving)
        self.btn_scan = QPushButton("Scan")
        self.btn_scan.clicked.connect(self.scan)

        self.btn_stop_resolve = QPushButton("Stop resolving")
        self.btn_stop_resolve.setObjectName("Secondary")
        self.btn_stop_resolve.setEnabled(False)
        self.btn_stop_resolve.clicked.connect(self.stop_resolving)

        self.btn_preview = QPushButton("Preview")
        self.btn_preview.setObjectName("Secondary")
        self.btn_preview.clicked.connect(self.preview_selected)

        self.btn_export = QPushButton("Export")
        self.btn_export.clicked.connect(self.export)

        actions = QHBoxLayout(); actions.setSpacing(8)
        actions.addWidget(self.btn_scan)
        actions.addWidget(self.btn_stop_resolve)
        actions.addStretch(1)
        actions.addWidget(self.btn_preview)
        actions.addWidget(self.btn_export)

        grid.addLayout(actions, 5, 0, 1, 4)

        outer.addWidget(card)

        # main splitter: matches (top) and bottom (padding+log)
        self.main_splitter = QSplitter(Qt.Vertical)

        # matches card
        matches_card = QFrame()
        matches_card.setObjectName("Card")
        mlay = QVBoxLayout(matches_card)
        mlay.setContentsMargins(10, 10, 10, 10)
        mlay.setSpacing(6)

        head = QHBoxLayout(); head.setSpacing(8)
        lblm = QLabel("Matches"); lblm.setFont(QFont("", 13, QFont.Bold))
        head.addWidget(lblm)
        head.addStretch(1)
        hint = QLabel("Multi-select. Overrides below."); hint.setObjectName("Hint")
        head.addWidget(hint)
        mlay.addLayout(head)

        self.listw = QListWidget()
        self.listw.setSelectionMode(QListWidget.ExtendedSelection)
        self.listw.setSpacing(1)
        self.listw.itemSelectionChanged.connect(self.rebuild_padding_panel)
        mlay.addWidget(self.listw, 1)

        # bottom card with its own splitter: overrides vs log
        bottom_card = QFrame()
        bottom_card.setObjectName("Card")
        blay = QVBoxLayout(bottom_card)
        blay.setContentsMargins(10, 10, 10, 10)
        blay.setSpacing(6)

        self.bottom_splitter = QSplitter(Qt.Vertical)

        # overrides pane
        pad_pane = QWidget()
        pad_layout = QVBoxLayout(pad_pane)
        pad_layout.setContentsMargins(0, 0, 0, 0)
        pad_layout.setSpacing(6)
        pad_layout.addWidget(QLabel("Padding overrides (selected)"))

        self.pad_scroll = QScrollArea()
        self.pad_scroll.setWidgetResizable(True)
        self.pad_scroll.setFrameShape(QFrame.NoFrame)

        self.pad_panel = QWidget()
        self.pad_layout = QVBoxLayout(self.pad_panel)
        self.pad_layout.setContentsMargins(0, 0, 0, 0)
        self.pad_layout.setSpacing(6)
        self.pad_scroll.setWidget(self.pad_panel)
        pad_layout.addWidget(self.pad_scroll, 1)

        # log pane
        log_pane = QWidget()
        log_layout = QVBoxLayout(log_pane)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(6)
        log_layout.addWidget(QLabel("Log"))

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        log_layout.addWidget(self.log, 1)

        self.bottom_splitter.addWidget(pad_pane)
        self.bottom_splitter.addWidget(log_pane)

        # defaults: big overrides, small log (user can drag)
        self.bottom_splitter.setSizes([260, 90])

        blay.addWidget(self.bottom_splitter, 1)

        self.main_splitter.addWidget(matches_card)
        self.main_splitter.addWidget(bottom_card)

        # defaults: big matches + big overrides area
        self.main_splitter.setSizes([420, 340])

        outer.addWidget(self.main_splitter, 1)

        self.statusBar().showMessage("Ready")

        # load persistence
        self.load_persistent_settings()
        self.apply_theme(self.settings_store.value("theme", "Dark", type=str))
        self.restore_splitters()
        self.rebuild_padding_panel()

    # --------- persistence: window + splitters ----------
    def closeEvent(self, event):
        self.save_persistent_settings()
        self.settings_store.setValue("window_geometry", self.saveGeometry())
        self.settings_store.setValue("main_splitter_sizes", self.main_splitter.sizes())
        self.settings_store.setValue("bottom_splitter_sizes", self.bottom_splitter.sizes())
        super().closeEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # keep last size even if app crashes
        self.settings_store.setValue("window_geometry", self.saveGeometry())

    def moveEvent(self, event):
        super().moveEvent(event)
        self.settings_store.setValue("window_geometry", self.saveGeometry())

    def restore_splitters(self):
        ms = self.settings_store.value("main_splitter_sizes", None)
        if ms:
            try:
                self.main_splitter.setSizes([int(x) for x in ms])
            except Exception:
                pass

        bs = self.settings_store.value("bottom_splitter_sizes", None)
        if bs:
            try:
                self.bottom_splitter.setSizes([int(x) for x in bs])
            except Exception:
                pass

    # --------- theme ----------
    def apply_theme(self, theme: str):
        t = (theme or "").strip().lower()
        if t == "light":
            self.setStyleSheet(LIGHT_QSS)
            self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentText("Light")
            self.theme_combo.blockSignals(False)
            self.settings_store.setValue("theme", "Light")
        else:
            self.setStyleSheet(DARK_QSS)
            self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentText("Dark")
            self.theme_combo.blockSignals(False)
            self.settings_store.setValue("theme", "Dark")

    def on_theme_changed(self):
        self.apply_theme(self.theme_combo.currentText())

    # --------- log + progress line editing ----------
    def log_line(self, s: str):
        self.log.append(str(s).rstrip("\n"))

    def _start_progress_line(self, which: str, title: str):
        attr = "_render_progress_block" if which == "render" else "_preview_progress_block"
        if getattr(self, attr) is None:
            self.log.append(f"{title}: 0%")
            setattr(self, attr, self.log.document().blockCount() - 1)

    def _update_progress_line(self, which: str, title: str, pct: int):
        attr = "_render_progress_block" if which == "render" else "_preview_progress_block"
        blockno = getattr(self, attr)
        if blockno is None:
            self._start_progress_line(which, title)
            blockno = getattr(self, attr)

        doc = self.log.document()
        block = doc.findBlockByNumber(blockno)
        if not block.isValid():
            setattr(self, attr, None)
            self._start_progress_line(which, title)
            return

        cursor = QTextCursor(block)
        cursor.select(QTextCursor.BlockUnderCursor)
        cursor.removeSelectedText()
        cursor.insertText(f"{title}: {pct}%")

    def _end_progress_line(self, which: str):
        attr = "_render_progress_block" if which == "render" else "_preview_progress_block"
        setattr(self, attr, None)

    # --------- persistence of fields ----------
    def _get_last_dir(self) -> str:
        d = self.settings_store.value("last_dialog_dir", "", type=str)
        if d and Path(d).exists():
            return d
        return str(Path.home())

    def _set_last_dir(self, path_str: str):
        p = Path(path_str)
        if p.is_file():
            p = p.parent
        if p.exists():
            self.settings_store.setValue("last_dialog_dir", str(p))

    def load_persistent_settings(self):
        self.phrase.setText(self.settings_store.value("phrase", "", type=str))
        self.black_path.setText(self.settings_store.value("black_path", "", type=str))
        self.output_dir.setText(self.settings_store.value("output_dir", "", type=str))

        self.case_insensitive.setChecked(self.settings_store.value("case_insensitive", True, type=bool))
        self.burn_subs.setChecked(self.settings_store.value("burn_subs", True, type=bool))
        self.add_black.setChecked(self.settings_store.value("add_black", True, type=bool))

        self.pad_before.setValue(float(self.settings_store.value("pad_before", 0.5)))
        self.pad_after.setValue(float(self.settings_store.value("pad_after", 1.5)))

        vroots = self.settings_store.value("video_roots", [], type=list)
        self.video_roots = [Path(r) for r in vroots if r]
        self.video_roots_edit.setText(" | ".join(str(x) for x in self.video_roots))

        sroots = self.settings_store.value("subtitle_roots", [], type=list)
        self.subtitle_roots = [Path(r) for r in sroots if r][:2]
        self.subtitle_roots_edit.setText(" | ".join(str(x) for x in self.subtitle_roots))

        theme = self.settings_store.value("theme", "Dark", type=str)
        self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentText(theme if theme in ("Light", "Dark") else "Dark")
        self.theme_combo.blockSignals(False)

    def save_persistent_settings(self):
        self.settings_store.setValue("phrase", self.phrase.text().strip())
        self.settings_store.setValue("black_path", self.black_path.text().strip())
        self.settings_store.setValue("output_dir", self.output_dir.text().strip())

        self.settings_store.setValue("case_insensitive", self.case_insensitive.isChecked())
        self.settings_store.setValue("burn_subs", self.burn_subs.isChecked())
        self.settings_store.setValue("add_black", self.add_black.isChecked())

        self.settings_store.setValue("pad_before", float(self.pad_before.value()))
        self.settings_store.setValue("pad_after", float(self.pad_after.value()))

        self.settings_store.setValue("video_roots", [str(p) for p in self.video_roots])
        self.settings_store.setValue("subtitle_roots", [str(p) for p in self.subtitle_roots])
        self.settings_store.setValue("theme", self.theme_combo.currentText())

    # --------- pickers ----------
    def add_subtitle_root(self):
        if len(self.subtitle_roots) >= 2:
            QMessageBox.information(self, "Limit", "You can add up to 2 subtitle folders.")
            return
        d = QFileDialog.getExistingDirectory(self, "Add subtitle folder", self._get_last_dir())
        if d:
            p = Path(d)
            if p not in self.subtitle_roots:
                self.subtitle_roots.append(p)
            self.subtitle_roots_edit.setText(" | ".join(str(x) for x in self.subtitle_roots))
            self._set_last_dir(d)
            self.save_persistent_settings()

    def clear_subtitle_roots(self):
        self.subtitle_roots.clear()
        self.subtitle_roots_edit.setText("")
        self.save_persistent_settings()

    def add_video_root(self):
        d = QFileDialog.getExistingDirectory(self, "Add a video root folder", self._get_last_dir())
        if d:
            p = Path(d)
            if p not in self.video_roots:
                self.video_roots.append(p)
            self.video_roots_edit.setText(" | ".join(str(x) for x in self.video_roots))
            self._set_last_dir(d)
            self.save_persistent_settings()

    def clear_video_roots(self):
        self.video_roots.clear()
        self.video_roots_edit.setText("")
        self.save_persistent_settings()

    def pick_black_clip(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Select black clip", self._get_last_dir(),
            filter="Video files (*.mp4 *.mkv *.m4v *.avi)"
        )
        if f:
            self.black_path.setText(f)
            self._set_last_dir(f)
            self.save_persistent_settings()

    def pick_output_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Choose output folder", self._get_last_dir())
        if d:
            self.output_dir.setText(d)
            self._set_last_dir(d)
            self.save_persistent_settings()

    # --------- helpers ----------
    def _settings(self) -> Settings:
        black = Path(self.black_path.text()) if self.black_path.text().strip() else None
        return Settings(
            pad_before=float(self.pad_before.value()),
            pad_after=float(self.pad_after.value()),
            resize_to=(1280, 720),
            add_black_between=self.add_black.isChecked(),
            black_clip_path=black,
            burn_subtitles=self.burn_subs.isChecked(),
            case_insensitive=self.case_insensitive.isChecked(),
        )

    def _selected_rows(self) -> list[int]:
        return [i.row() for i in self.listw.selectedIndexes()]

    def _validate_common(self) -> bool:
        if not self.subtitle_roots:
            QMessageBox.warning(self, "Missing", "Please add at least one subtitle folder (up to 2).")
            return False
        if not self.video_roots:
            QMessageBox.warning(self, "Missing", "Please add at least one video root folder.")
            return False
        if not self.phrase.text().strip():
            QMessageBox.warning(self, "Missing", "Please enter a phrase to search.")
            return False
        return True

    # --------- padding overrides UI ----------
    def clear_padding_panel(self):
        while self.pad_layout.count():
            item = self.pad_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def rebuild_padding_panel(self):
        self.clear_padding_panel()

        if not self.matches:
            hint = QLabel("No matches yet.")
            hint.setObjectName("Hint")
            self.pad_layout.addWidget(hint)
            self.pad_layout.addStretch(1)
            return

        rows = sorted(set(self._selected_rows()))
        if not rows:
            hint = QLabel("Select matches above to override their padding.")
            hint.setObjectName("Hint")
            self.pad_layout.addWidget(hint)
            self.pad_layout.addStretch(1)
            return

        default_b = float(self.pad_before.value())
        default_a = float(self.pad_after.value())

        for idx in rows:
            m = self.matches[idx]
            b0, a0 = self.pad_overrides.get(idx, (default_b, default_a))

            roww = QWidget()
            hl = QHBoxLayout(roww)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(6)

            title = QLabel(f"{idx}: {m.subtitle_file.name}  ({m.start:.1f}-{m.end:.1f})")
            title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

            sb_b = QDoubleSpinBox()
            sb_b.setRange(-30.0, 30.0)
            sb_b.setSingleStep(0.1)
            sb_b.setValue(float(b0))

            sb_a = QDoubleSpinBox()
            sb_a.setRange(-30.0, 30.0)
            sb_a.setSingleStep(0.1)
            sb_a.setValue(float(a0))

            sb_b.valueChanged.connect(lambda v, i=idx: self._set_override_before(i, v))
            sb_a.valueChanged.connect(lambda v, i=idx: self._set_override_after(i, v))

            btn_clear = QToolButton()
            btn_clear.setText("Clear")
            btn_clear.setObjectName("PadClear")
            btn_clear.setAutoRaise(True)
            btn_clear.setCursor(Qt.PointingHandCursor)
            btn_clear.setFixedHeight(24)
            btn_clear.setFixedWidth(54)
            btn_clear.clicked.connect(lambda _, i=idx: self._clear_override(i))

            hl.addWidget(title, 1)
            hl.addWidget(QLabel("B:"))
            hl.addWidget(sb_b)
            hl.addWidget(QLabel("A:"))
            hl.addWidget(sb_a)
            hl.addWidget(btn_clear)

            self.pad_layout.addWidget(roww)

        self.pad_layout.addStretch(1)

    def _set_override_before(self, idx: int, before: float):
        _, a = self.pad_overrides.get(idx, (float(self.pad_before.value()), float(self.pad_after.value())))
        self.pad_overrides[idx] = (float(before), float(a))

    def _set_override_after(self, idx: int, after: float):
        b, _ = self.pad_overrides.get(idx, (float(self.pad_before.value()), float(self.pad_after.value())))
        self.pad_overrides[idx] = (float(b), float(after))

    def _clear_override(self, idx: int):
        if idx in self.pad_overrides:
            del self.pad_overrides[idx]
        self.rebuild_padding_panel()

    # --------- background jobs ----------
    def cancel_background_jobs(self):
        if hasattr(self, "resolve_worker") and self.resolve_worker and self.resolve_worker.isRunning():
            self.resolve_worker.requestInterruption()
        if hasattr(self, "scan_worker") and self.scan_worker and self.scan_worker.isRunning():
            self.scan_worker.requestInterruption()
        if hasattr(self, "export_worker") and self.export_worker and self.export_worker.isRunning():
            # no clean cancel for moviepy; we just disable UI and let it finish
            pass
        if hasattr(self, "preview_worker") and self.preview_worker and self.preview_worker.isRunning():
            pass

    def stop_resolving(self):
        if hasattr(self, "resolve_worker") and self.resolve_worker and self.resolve_worker.isRunning():
            self.resolve_worker.requestInterruption()
            self.log_line("Stopping resolve thread...")

    # --------- actions ----------
    def scan(self):
        self.cancel_background_jobs()
        self.pad_overrides.clear()
        self.clear_padding_panel()

        if not self._validate_common():
            return

        phrase = self.phrase.text().strip()
        self.listw.clear()
        self.matches = []

        self.btn_scan.setEnabled(False)
        self.btn_stop_resolve.setEnabled(False)
        self.log_line("Starting scan...")

        self.scan_worker = ScanWorker(self.subtitle_roots, phrase, self.case_insensitive.isChecked())
        self.scan_worker.progress.connect(self.log_line)
        self.scan_worker.failed.connect(self._scan_failed)
        self.scan_worker.done.connect(self._scan_done)
        self.scan_worker.start()

    def _scan_failed(self, msg: str):
        self.btn_scan.setEnabled(True)
        self.btn_stop_resolve.setEnabled(False)
        QMessageBox.critical(self, "Scan failed", "See log for details.")
        self.log_line("Scan failed:\n" + msg)

    def _scan_done(self, matches: list):
        self.btn_scan.setEnabled(True)
        self.matches = matches

        self.listw.setUpdatesEnabled(False)
        self.listw.clear()
        for m in self.matches:
            self.listw.addItem(QListWidgetItem(
                f"{m.subtitle_file.name} | {m.start:.2f}-{m.end:.2f} | {m.text}"
            ))
        self.listw.setUpdatesEnabled(True)

        self.log_line(f"Scan done. Found {len(self.matches)} matches.")

        if not self.matches:
            QMessageBox.information(self, "No matches", "No subtitles contained that phrase.")
            self.rebuild_padding_panel()
            return

        self.log_line("Starting background video resolution...")
        self.btn_stop_resolve.setEnabled(True)

        self.resolve_worker = ResolveWorker(self.matches, self.video_roots)
        self.resolve_worker.progress.connect(self.log_line)
        self.resolve_worker.resolved_one.connect(self._resolved_one)
        self.resolve_worker.failed.connect(self._resolve_failed)
        self.resolve_worker.done.connect(self._resolve_done)
        self.resolve_worker.start()

        self.rebuild_padding_panel()

    def _resolved_one(self, idx: int, video_name: str):
        item = self.listw.item(idx)
        if not item:
            return
        base = item.text().split(" | ", 1)[-1]
        prefix = self.matches[idx].subtitle_file.name
        if video_name:
            item.setText(f"{prefix} | ✅ {video_name} | {base}")
        else:
            item.setText(f"{prefix} | ❌ NOT FOUND | {base}")

    def _resolve_failed(self, msg: str):
        self.btn_stop_resolve.setEnabled(False)
        QMessageBox.critical(self, "Resolve failed", "See log for details.")
        self.log_line("Resolve failed:\n" + msg)

    def _resolve_done(self):
        self.btn_stop_resolve.setEnabled(False)
        self.log_line("Video resolution completed.")

    def preview_selected(self):
        rows = self._selected_rows()
        if len(rows) != 1:
            QMessageBox.information(self, "Preview", "Select exactly one match to preview.")
            return

        idx = rows[0]
        m = self.matches[idx]
        b, a = self.pad_overrides.get(idx, (None, None))

        self.btn_preview.setEnabled(False)
        self._start_progress_line("preview", "Preview render")
        self._update_progress_line("preview", "Preview render", 0)

        self.preview_worker = PreviewWorker(m, self.video_roots, self._settings(), b, a)
        self.preview_worker.progress.connect(self.log_line)
        self.preview_worker.render_pct.connect(lambda p: self._update_progress_line("preview", "Preview render", p))
        self.preview_worker.failed.connect(self._preview_failed)
        self.preview_worker.done.connect(self._preview_done)
        self.preview_worker.start()

    def _preview_failed(self, msg: str):
        self.btn_preview.setEnabled(True)
        QMessageBox.critical(self, "Preview failed", "See log for details.")
        self.log_line("Preview failed:\n" + msg)
        self._end_progress_line("preview")

    def _preview_done(self, tmp_path: str):
        self.btn_preview.setEnabled(True)
        self._update_progress_line("preview", "Preview render", 100)
        self.log_line(f"Preview ready: {tmp_path}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(tmp_path))
        self._end_progress_line("preview")

    def export(self):
        rows = self._selected_rows()
        if not rows:
            QMessageBox.warning(self, "Export", "Select at least one match to export.")
            return

        out_dir_txt = self.output_dir.text().strip()
        if not out_dir_txt:
            QMessageBox.warning(self, "Export", "Choose an output folder.")
            return

        out_dir = Path(out_dir_txt)
        if not out_dir.exists():
            QMessageBox.warning(self, "Export", "Output folder does not exist.")
            return

        phrase = self.phrase.text().strip()
        filename = sanitize_filename(phrase) + ".mp4"
        out_path = out_dir / filename

        selected = [self.matches[i] for i in rows]

        # remap overrides to subset indices
        pad_overrides_for_subset: dict[int, tuple[float, float]] = {}
        for j, orig_idx in enumerate(rows):
            if orig_idx in self.pad_overrides:
                pad_overrides_for_subset[j] = self.pad_overrides[orig_idx]

        self.btn_export.setEnabled(False)
        self.log_line(f"Starting export -> {out_path}")

        self._start_progress_line("render", "Render")
        self._update_progress_line("render", "Render", 0)

        self.export_worker = ExportWorker(selected, self.video_roots, out_path, self._settings(), pad_overrides_for_subset)
        self.export_worker.progress.connect(self.log_line)
        self.export_worker.render_pct.connect(lambda p: self._update_progress_line("render", "Render", p))
        self.export_worker.failed.connect(self._export_failed)
        self.export_worker.done.connect(self._export_done)
        self.export_worker.start()

    def _export_failed(self, msg: str):
        self.btn_export.setEnabled(True)
        QMessageBox.critical(self, "Export failed", "See log for details.")
        self.log_line("Export failed:\n" + msg)
        self._end_progress_line("render")

    def _export_done(self, out_path: str):
        self.btn_export.setEnabled(True)
        self._update_progress_line("render", "Render", 100)
        QMessageBox.information(self, "Done", f"Export completed:\n{out_path}")
        self.log_line(f"Export completed: {out_path}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(out_path))
        self._end_progress_line("render")


def main():
    app = QApplication([])
    f = app.font()
    f.setPointSize(11)
    app.setFont(f)

    w = MainWindow()
    w.show()
    app.exec_()


if __name__ == "__main__":
    main()
