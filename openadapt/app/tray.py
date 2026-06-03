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

try:
    from PySide6.QtCore import (
        QObject,
        Qt,
        QThread,
        QTimer,
        Signal,
    )
    from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QFrame,
        QHBoxLayout,
        QLabel,
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
# Native macOS dialogs via osascript (no Qt windows → tray icon stays visible)
# ---------------------------------------------------------------------------

def _osascript_input(prompt: str, title: str = "OpenAdapt") -> Optional[str]:
    """Show a native macOS text-input dialog. Returns text or None on cancel.

    Runs synchronously – call from a background thread so the Qt loop is free.
    """
    script = (
        f'display dialog "{prompt}" '
        f'default answer "" '
        f'with title "{title}" '
        f'buttons {{"Cancel", "OK"}} '
        f'default button "OK"'
    )
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            return None
        for part in r.stdout.strip().split(", "):
            if part.startswith("text returned:"):
                text = part[len("text returned:"):].strip()
                return text or None
    except Exception:
        pass
    return None


def _osascript_confirm(message: str, title: str = "OpenAdapt") -> bool:
    """Show a native macOS warning alert. Returns True if the user confirms."""
    script = (
        f'display alert "{title}" '
        f'message "{message}" '
        f'buttons {{"Cancel", "Delete"}} '
        f'default button "Cancel" '
        f'cancel button "Cancel" '
        f'as warning'
    )
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=60,
        )
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Lightweight toast (no external deps, non-activating)
# ---------------------------------------------------------------------------

_TOAST_STACK: list["_Toast"] = []
_TOAST_WIDTH = 320
_TOAST_MARGIN = 12
_TOAST_OFFSET_TOP = 50
_PANEL_FLAGS = (
    Qt.WindowType.Tool
    | Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
)


def _restack_toasts() -> None:
    screen = QApplication.primaryScreen().availableGeometry()
    y = screen.top() + _TOAST_OFFSET_TOP
    for t in list(_TOAST_STACK):
        if t.isVisible():
            t.move(screen.right() - _TOAST_WIDTH - _TOAST_MARGIN, y)
            y += t.height() + _TOAST_MARGIN


class _Toast(QWidget):
    """Floating notification card – non-activating so the tray stays visible."""

    def __init__(
        self,
        message: str,
        title: str = "OpenAdapt",
        duration: int = 5000,
        closeable: bool = True,
    ) -> None:
        super().__init__(None, _PANEL_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setFixedWidth(_TOAST_WIDTH)

        card = QFrame(self)
        card.setObjectName("card")
        card.setStyleSheet(
            "#card { background:#E7F4F9; border:1px solid #C5DDE8; border-radius:6px; }"
        )
        inner = QVBoxLayout(card)
        inner.setContentsMargins(14, 10, 14, 10)
        inner.setSpacing(4)

        hdr = QHBoxLayout()
        title_lbl = QLabel(f"<b>{title}</b>")
        title_lbl.setFont(QFont("Arial", 11))
        hdr.addWidget(title_lbl)
        hdr.addStretch()
        if closeable:
            close_btn = QPushButton("✕")
            close_btn.setFlat(True)
            close_btn.setFixedSize(20, 20)
            close_btn.setStyleSheet("color:#666; font-size:10px;")
            close_btn.clicked.connect(self.close)
            hdr.addWidget(close_btn)
        inner.addLayout(hdr)

        msg_lbl = QLabel(message)
        msg_lbl.setFont(QFont("Arial", 10))
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet("color:#444;")
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
            # log_memory=True spawns mem_writer which calls psutil.Process(os.getpid())
            # but the PID is stale in Python 3.14 "spawn" subprocesses, causing a
            # crash that blocks wait_for_ready() for its full 60-second timeout.
            log_memory=False,
        ) as recorder:
            recorder.wait_for_ready(timeout=15)  # don't block forever if a worker fails
            _send({"type": "record.started"})
            stop_event.wait()
            recorder.stop()

    except ImportError:
        _send({
            "type": "record.error",
            "error": "openadapt-capture not installed.\nRun: pip install openadapt-capture",
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
# System tray
# QObject inheritance is required to define Signals on this class.
# ---------------------------------------------------------------------------

class SystemTrayIcon(QObject):
    """PySide6 system tray icon for OpenAdapt."""

    # Emitted from background threads to safely trigger actions on Qt thread
    _do_start = Signal(str)   # task_description
    _do_delete = Signal(Path) # capture path confirmed for deletion

    def __init__(self) -> None:
        super().__init__()

        self.app = QApplication.instance() or QApplication([])
        self.app.setQuitOnLastWindowClosed(False)

        if sys.platform == "darwin":
            try:
                from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
                NSApplication.sharedApplication().setActivationPolicy_(
                    NSApplicationActivationPolicyAccessory
                )
            except ImportError:
                pass

        # recording state
        self._recording = False
        self._recorder_proc: Optional[multiprocessing.Process] = None
        self._stop_event: Optional[multiprocessing.Event] = None
        self._capture_path: Optional[str] = None

        # sticky toasts (persistent until manually closed)
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

        # Wire cross-thread signals to their Qt-thread slots
        self._do_start.connect(self._launch_recorder)
        self._do_delete.connect(self._perform_delete)

        self._icon = _make_icon()
        self.tray = QSystemTrayIcon(self._icon, self.app)
        self.tray.setVisible(True)
        self._build_menu()

    # ── menu ────────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        self.menu = QMenu()

        self._record_action = QAction("Record")
        self._record_action.triggered.connect(self._start_recording)
        self.menu.addAction(self._record_action)

        self._stop_action = QAction("Stop Recording")
        self._stop_action.triggered.connect(self._stop_recording)
        self._stop_action.setEnabled(False)
        self.menu.addAction(self._stop_action)

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

    # ── IPC signal handler (runs in main Qt thread) ──────────────────────

    def _on_signal(self, signal: dict) -> None:
        kind = signal.get("type", "")

        if kind == "record.starting":
            self._recording = True
            self._record_action.setEnabled(False)
            self._stop_action.setEnabled(True)
            self._sticky["record.starting"] = self._toast(
                "Recording starting, please wait…",
                duration=0,
                closeable=False,
            )

        elif kind == "record.started":
            self._close_sticky("record.starting")
            self._toast("Recording started.")

        elif kind == "record.stopped":
            # _stop_recording() may have already updated the UI; still
            # refresh the captures list so the new capture appears.
            self._recording = False
            self._record_action.setEnabled(True)
            self._stop_action.setEnabled(False)
            self._close_sticky("record.starting")
            self._close_sticky("record.stopping")
            path = signal.get("path")
            if path:
                self._toast(f"Recording saved to:\n{path}")
            else:
                self._toast("Recording saved.")
            self._populate_captures()

        elif kind == "record.error":
            self._recording = False
            self._record_action.setEnabled(True)
            self._stop_action.setEnabled(False)
            self._close_sticky("record.starting")
            self._close_sticky("record.stopping")
            self._toast(f"Recording error: {signal.get('error', '')}")

    # ── recording ────────────────────────────────────────────────────────

    def _start_recording(self) -> None:
        """Ask for task description via osascript (no Qt window opened)."""
        def _ask() -> None:
            task = _osascript_input(
                "Briefly describe the task to be recorded:",
                title="New Recording",
            )
            if task:
                self._do_start.emit(task)   # delivered to Qt thread

        Thread(target=_ask, daemon=True).start()

    def _launch_recorder(self, task_description: str) -> None:
        """Runs on Qt thread (via _do_start signal). Starts the subprocess."""
        settings.capture_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = task_description.replace(" ", "_")[:40]
        capture_path = str(settings.capture_dir / f"{ts}_{safe}")
        self._capture_path = capture_path  # remembered for the saved toast

        self._stop_event = multiprocessing.Event()
        self._recorder_proc = multiprocessing.Process(
            target=_recorder_worker,
            args=(capture_path, task_description, self._writer, self._stop_event),
        )
        self._recorder_proc.start()

    def _stop_recording(self) -> None:
        if not self._recording:
            return

        # ── 1. Update the UI immediately so the user gets instant feedback ──
        self._recording = False
        self._record_action.setEnabled(True)
        self._stop_action.setEnabled(False)
        self._close_sticky("record.starting")
        self._toast("Stopping recording…")

        proc = self._recorder_proc
        stop_event = self._stop_event
        writer = self._writer

        capture_path = self._capture_path

        def _stop_bg() -> None:
            # 1. Signal the recorder loop to exit
            if stop_event:
                try:
                    stop_event.set()
                except Exception:
                    pass

            if proc:
                # 2. Give up to 60 s for video finalisation + queue drain.
                #    The UI already updated so the user isn't blocked.
                proc.join(timeout=60)
                if proc.is_alive():
                    proc.terminate()          # SIGTERM – allows cleanup
                    proc.join(timeout=10)
                    if proc.is_alive():
                        proc.kill()           # SIGKILL – last resort
                        proc.join(timeout=3)

            # 3. Always push a stopped signal so _on_signal refreshes the menu
            try:
                writer.send({"type": "record.stopped", "path": capture_path})
            except Exception:
                pass

        Thread(target=_stop_bg, daemon=True).start()

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
            [sys.executable, "-m", "openadapt.cli",
             "train", "start", "--capture", str(capture_path)],
            close_fds=True,
        )

    def _delete_capture(self, capture_path: Path) -> None:
        """Ask for confirmation via osascript (no Qt window opened)."""
        def _ask() -> None:
            if _osascript_confirm(
                f"Delete capture '{capture_path.name}'?",
                title="Confirm Delete",
            ):
                self._do_delete.emit(capture_path)  # delivered to Qt thread

        Thread(target=_ask, daemon=True).start()

    def _perform_delete(self, capture_path: Path) -> None:
        """Runs on Qt thread (via _do_delete signal). Deletes the capture."""
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
            webbrowser.open(f"http://{settings.server_host}:{settings.server_port}")
            return

        self._toast("Starting dashboard…")
        self._serve_proc = subprocess.Popen(
            [sys.executable, "-m", "openadapt.cli",
             "serve", "--port", str(settings.server_port)],
            close_fds=True,
        )

    # ── quit ─────────────────────────────────────────────────────────────

    def _quit(self) -> None:
        if self._recorder_proc and self._recorder_proc.is_alive():
            if self._stop_event:
                self._stop_event.set()
            self._recorder_proc.terminate()
            self._recorder_proc.join(timeout=3)
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
        import signal

        # Qt swallows SIGINT by default. Install a handler that routes it
        # through the Qt event loop so _quit() runs cleanly.
        signal.signal(signal.SIGINT, lambda *_: self._quit())

        # A 200 ms timer keeps the Python interpreter "awake" so it can
        # deliver the SIGINT to our handler (Qt would otherwise block it).
        wakeup = QTimer()
        wakeup.setInterval(200)
        wakeup.timeout.connect(lambda: None)
        wakeup.start()

        self.app.exec()


# ---------------------------------------------------------------------------

def _run() -> None:
    tray = SystemTrayIcon()
    tray.run()


if __name__ == "__main__":
    _run()
