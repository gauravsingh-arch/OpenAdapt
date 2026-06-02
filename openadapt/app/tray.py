"""System tray icon for OpenAdapt.

Usage: python -m openadapt.app.tray

Dependencies: pip install PySide6
"""

from datetime import datetime
from functools import partial
from pathlib import Path
from threading import Thread
from typing import Any, Optional
import multiprocessing
import subprocess
import sys
import time

try:
    from PySide6.QtCore import (
        QObject,
        QPoint,
        QRect,
        QSize,
        Qt,
        QThread,
        QTimer,
        Signal,
    )
    from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QFrame,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QMainWindow,
        QMenu,
        QPushButton,
        QSystemTrayIcon,
        QVBoxLayout,
        QWidget,
    )
except ImportError as _e:
    print(
        f"Missing GUI dependency: {_e}\n"
        "Install with: pip install PySide6",
        file=sys.stderr,
    )
    sys.exit(1)

from openadapt.config import settings
from openadapt.version import __version__

# ---------------------------------------------------------------------------
# Icon
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
_LOGO_CANDIDATES = [
    _HERE / "assets" / "logo.png",
    _HERE.parent.parent.parent / "legacy" / "openadapt" / "app" / "assets" / "logo.png",
]
ICON_PATH: Optional[str] = next((str(p) for p in _LOGO_CANDIDATES if p.exists()), None)


def _make_icon() -> QIcon:
    if ICON_PATH:
        return QIcon(ICON_PATH)
    px = QPixmap(32, 32)
    px.fill(QColor("#1E90FF"))
    return QIcon(px)


# ---------------------------------------------------------------------------
# Lightweight toast widget (no external deps)
# ---------------------------------------------------------------------------

_TOAST_STACK: list["_Toast"] = []
_TOAST_WIDTH = 320
_TOAST_MARGIN = 12
_TOAST_OFFSET_TOP = 50


def _restack_toasts() -> None:
    """Reposition all visible toasts so they stack without overlap."""
    screen = QApplication.primaryScreen().availableGeometry()
    y = screen.top() + _TOAST_OFFSET_TOP
    for t in list(_TOAST_STACK):
        if t.isVisible():
            t.move(screen.right() - _TOAST_WIDTH - _TOAST_MARGIN, y)
            y += t.height() + _TOAST_MARGIN


class _Toast(QWidget):
    """Small floating notification card anchored to the top-right corner."""

    _closed = Signal()

    def __init__(
        self,
        message: str,
        title: str = "OpenAdapt",
        duration: int = 5000,
        closeable: bool = True,
    ) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setFixedWidth(_TOAST_WIDTH)

        # card frame
        card = QFrame(self)
        card.setObjectName("card")
        card.setStyleSheet(
            "#card {"
            "  background: #E7F4F9;"
            "  border: 1px solid #C5DDE8;"
            "  border-radius: 6px;"
            "}"
        )
        inner = QVBoxLayout(card)
        inner.setContentsMargins(14, 10, 14, 10)
        inner.setSpacing(4)

        # header row
        hdr = QHBoxLayout()
        title_lbl = QLabel(f"<b>{title}</b>")
        title_lbl.setFont(QFont("Arial", 11))
        hdr.addWidget(title_lbl)
        hdr.addStretch()
        if closeable:
            close_btn = QPushButton("✕")
            close_btn.setFlat(True)
            close_btn.setFixedSize(20, 20)
            close_btn.setStyleSheet("color: #666; font-size: 10px;")
            close_btn.clicked.connect(self.close)
            hdr.addWidget(close_btn)
        inner.addLayout(hdr)

        msg_lbl = QLabel(message)
        msg_lbl.setFont(QFont("Arial", 10))
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet("color: #444;")
        inner.addWidget(msg_lbl)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self.adjustSize()

        _TOAST_STACK.append(self)
        _restack_toasts()

        if duration > 0:
            QTimer.singleShot(duration, self.close)

    def closeEvent(self, event: Any) -> None:
        if self in _TOAST_STACK:
            _TOAST_STACK.remove(self)
        super().closeEvent(event)
        _restack_toasts()


# ---------------------------------------------------------------------------
# Recorder subprocess entry point
# ---------------------------------------------------------------------------

def _recorder_worker(
    capture_path: str,
    task_description: str,
    conn: Any,
    stop_event: Any,
) -> None:
    """Record a capture session in a subprocess, piping status events back."""

    def _send(msg: dict) -> None:
        try:
            conn.send(msg)
        except Exception:
            pass

    try:
        from openadapt_capture import Recorder

        _send({"type": "record.starting"})
        with Recorder(
            capture_path,
            task_description=task_description,
            capture_video=True,
            capture_audio=settings.capture_audio,
        ) as recorder:
            recorder.wait_for_ready()
            _send({"type": "record.started"})
            while not stop_event.is_set() and recorder.is_recording:
                time.sleep(0.2)

    except ImportError:
        _send({
            "type": "record.error",
            "error": (
                "openadapt-capture not installed.\n"
                "Run: pip install openadapt-capture"
            ),
        })
    except Exception as exc:
        _send({"type": "record.error", "error": str(exc)})
    finally:
        _send({"type": "record.stopped"})
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# IPC Worker – polls the pipe in a QThread, emits Qt signals to main thread
# ---------------------------------------------------------------------------

class _PipeWorker(QObject):
    data = Signal(dict)

    def __init__(self, reader: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._reader = reader

    def run(self) -> None:
        while True:
            try:
                if self._reader.poll(timeout=0.1):
                    self.data.emit(self._reader.recv())
            except EOFError:
                break
            except Exception:
                continue


# ---------------------------------------------------------------------------
# Confirm-delete dialog
# ---------------------------------------------------------------------------

class _ConfirmDeleteDialog(QDialog):
    def __init__(self, capture_name: str) -> None:
        super().__init__()
        self.setWindowTitle("Confirm Delete")
        layout = QVBoxLayout(self)
        lbl = QLabel(f"Delete capture '{capture_name}'?")
        lbl.setWordWrap(True)
        layout.addWidget(lbl)
        btns = QHBoxLayout()
        yes = QPushButton("Delete")
        no = QPushButton("Cancel")
        btns.addWidget(yes)
        btns.addWidget(no)
        layout.addLayout(btns)
        yes.clicked.connect(self.accept)
        no.clicked.connect(self.reject)

    def confirmed(self) -> bool:
        return self.exec() == QDialog.DialogCode.Accepted


# ---------------------------------------------------------------------------
# System tray
# ---------------------------------------------------------------------------

class SystemTrayIcon:
    """PySide6 system tray icon for OpenAdapt."""

    def __init__(self) -> None:
        self.app = QApplication.instance() or QApplication([])

        if sys.platform == "darwin":
            try:
                from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
                NSApplication.sharedApplication().setActivationPolicy_(
                    NSApplicationActivationPolicyAccessory
                )
            except ImportError:
                pass

        self.app.setQuitOnLastWindowClosed(False)

        # recording state
        self._recording = False
        self._recorder_proc: Optional[multiprocessing.Process] = None
        self._stop_event: Optional[multiprocessing.Event] = None

        # sticky toasts keyed by event name (so we can close them later)
        self._sticky: dict[str, _Toast] = {}

        # background serve process
        self._serve_proc: Optional[subprocess.Popen] = None

        # QActions kept alive to prevent GC
        self._capture_actions: list = []

        # one-way pipe: recorder subprocess writes, Worker thread reads
        self._reader, self._writer = multiprocessing.Pipe(duplex=False)

        self._notifier = QThread()
        self._worker = _PipeWorker(reader=self._reader)
        self._worker.moveToThread(self._notifier)
        self._notifier.started.connect(self._worker.run)
        self._notifier.start()
        self._worker.data.connect(self._on_signal)

        # tray icon + menu
        self._icon = _make_icon()
        self.tray = QSystemTrayIcon(self._icon, self.app)
        self.tray.setVisible(True)
        self._build_menu()

    # ── menu ────────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        self.menu = QMenu()

        self._record_action = QAction("Record")
        self._record_action.triggered.connect(self._toggle_record)
        self.menu.addAction(self._record_action)

        self.menu.addSeparator()

        self._captures_menu = self.menu.addMenu("Captures")
        self._populate_captures()

        self.menu.addSeparator()

        serve_action = QAction("Serve Dashboard")
        serve_action.triggered.connect(self._serve)
        self.menu.addAction(serve_action)

        ver_action = QAction(f"Version {__version__}")
        ver_action.setEnabled(False)
        self.menu.addAction(ver_action)

        self.menu.addSeparator()

        quit_action = QAction("Quit")
        quit_action.triggered.connect(self._quit)
        self.menu.addAction(quit_action)

        self.tray.setContextMenu(self.menu)

    # ── signal handler (runs in main Qt thread) ──────────────────────────

    def _on_signal(self, signal: dict) -> None:
        kind = signal.get("type", "")

        if kind == "record.starting":
            self._recording = True
            self._record_action.setText("Stop Recording")
            self._sticky["record.starting"] = self._toast(
                "Recording starting, please wait…",
                duration=0,
                closeable=False,
            )

        elif kind == "record.started":
            self._close_sticky("record.starting")
            self._toast("Recording started.")

        elif kind == "record.stopped":
            if not self._recording:
                return  # guard against duplicate
            self._recording = False
            self._record_action.setText("Record")
            self._close_sticky("record.starting")
            self._close_sticky("record.stopping")
            self._toast("Recording saved.")
            self._populate_captures()

        elif kind == "record.error":
            self._recording = False
            self._record_action.setText("Record")
            self._close_sticky("record.starting")
            self._close_sticky("record.stopping")
            self._toast(f"Recording error: {signal.get('error', '')}")

    # ── recording ────────────────────────────────────────────────────────

    def _toggle_record(self) -> None:
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        task_description, ok = QInputDialog.getText(
            None,
            "New Recording",
            "Briefly describe the task to be recorded:",
        )
        if not ok or not task_description.strip():
            return

        settings.capture_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = task_description.strip().replace(" ", "_")[:40]
        capture_path = str(settings.capture_dir / f"{ts}_{safe}")

        self._stop_event = multiprocessing.Event()
        self._recorder_proc = multiprocessing.Process(
            target=_recorder_worker,
            args=(capture_path, task_description, self._writer, self._stop_event),
            daemon=True,
        )
        self._recorder_proc.start()

    def _stop_recording(self) -> None:
        self._sticky["record.stopping"] = self._toast(
            "Recording stopping, please wait…",
            duration=0,
            closeable=False,
        )
        proc = self._recorder_proc
        stop_event = self._stop_event

        def _wait_and_kill() -> None:
            if stop_event:
                stop_event.set()
            if proc:
                proc.join(timeout=5)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=2)
                    # subprocess won't reach its finally after terminate
                    try:
                        self._writer.send({"type": "record.stopped"})
                    except Exception:
                        pass

        Thread(target=_wait_and_kill, daemon=True).start()

    # ── captures menu ────────────────────────────────────────────────────

    def _populate_captures(self) -> None:
        self._captures_menu.clear()
        self._capture_actions.clear()

        captures = self._list_captures()
        if not captures:
            empty = QAction("No captures found")
            empty.setEnabled(False)
            self._captures_menu.addAction(empty)
            self._capture_actions.append(empty)
            return

        for cap in captures:
            sub = self._captures_menu.addMenu(cap.name)
            view_a = QAction("View in Browser")
            view_a.triggered.connect(partial(self._view_capture, cap))
            train_a = QAction("Train Model")
            train_a.triggered.connect(partial(self._train_capture, cap))
            delete_a = QAction("Delete")
            delete_a.triggered.connect(partial(self._delete_capture, cap))
            sub.addAction(view_a)
            sub.addAction(train_a)
            sub.addSeparator()
            sub.addAction(delete_a)
            self._capture_actions.extend([view_a, train_a, delete_a])

    def _list_captures(self) -> list[Path]:
        d = settings.capture_dir
        if not d.exists():
            return []
        return sorted(
            [p for p in d.iterdir() if p.is_dir() and (p / "recording.db").exists()],
            reverse=True,
        )

    # ── capture actions ──────────────────────────────────────────────────

    def _view_capture(self, capture_path: Path) -> None:
        self._toast(f"Opening {capture_path.name}…")

        def _run() -> None:
            try:
                from openadapt_capture import create_html
                import webbrowser
                output = capture_path / "viewer.html"
                create_html(str(capture_path), str(output))
                webbrowser.open(f"file://{output.absolute()}")
            except Exception:
                pass

        Thread(target=_run, daemon=True).start()

    def _train_capture(self, capture_path: Path) -> None:
        self._toast(f"Starting training on {capture_path.name}…")
        subprocess.Popen(
            [
                sys.executable, "-m", "openadapt.cli",
                "train", "start", "--capture", str(capture_path),
            ],
            close_fds=True,
        )

    def _delete_capture(self, capture_path: Path) -> None:
        if _ConfirmDeleteDialog(capture_path.name).confirmed():
            import shutil
            try:
                shutil.rmtree(capture_path)
                self._toast(f"Deleted {capture_path.name}.")
                self._populate_captures()
            except Exception as exc:
                self._toast(f"Delete failed: {exc}")

    # ── dashboard ────────────────────────────────────────────────────────

    def _serve(self) -> None:
        if self._serve_proc and self._serve_proc.poll() is None:
            import webbrowser
            self._toast("Dashboard already running – opening browser.")
            webbrowser.open(
                f"http://{settings.server_host}:{settings.server_port}"
            )
            return

        self._toast("Starting dashboard…")
        self._serve_proc = subprocess.Popen(
            [
                sys.executable, "-m", "openadapt.cli",
                "serve", "--port", str(settings.server_port),
            ],
            close_fds=True,
        )

    # ── quit ─────────────────────────────────────────────────────────────

    def _quit(self) -> None:
        if self._serve_proc and self._serve_proc.poll() is None:
            self._serve_proc.terminate()
        self.app.quit()

    # ── helpers ──────────────────────────────────────────────────────────

    def _close_sticky(self, key: str) -> None:
        toast = self._sticky.pop(key, None)
        if toast is not None:
            try:
                toast.close()
            except Exception:
                pass

    def _toast(
        self,
        message: str,
        title: str = "OpenAdapt",
        duration: int = 5000,
        closeable: bool = True,
    ) -> _Toast:
        t = _Toast(message, title=title, duration=duration, closeable=closeable)
        t.show()
        return t

    def run(self) -> None:
        self.app.exec()


# ---------------------------------------------------------------------------

def _run() -> None:
    tray = SystemTrayIcon()
    tray.run()


if __name__ == "__main__":
    _run()
