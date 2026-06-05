"""Ingest an OpenAdapt recording.db into pandas DataFrames.

Usage
-----
from Screenshot_Recordingdb_Video_Ingestion import RecordingIngestion

ing = RecordingIngestion("~/.openadapt/captures/20260603_101543_abc/recording.db")
ing.load()

# Access individual DataFrames
ing.recording          # 1-row metadata
ing.action_events      # every mouse/keyboard event
ing.screenshots        # PNG blobs (decoded to PIL images on demand)
ing.window_events      # active-window info
ing.audio_info         # audio chunks + transcription
ing.performance_stats  # per-event timing
ing.memory_stats       # RAM samples
ing.browser_events     # browser DOM events

# Or get a dict of all tables at once
frames = ing.all_tables()
"""

from __future__ import annotations

import io
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd


class RecordingIngestion:
    """Load every table from an OpenAdapt recording.db into pandas DataFrames."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        if not self.db_path.exists():
            raise FileNotFoundError(f"recording.db not found: {self.db_path}")

        # Public DataFrames – populated after .load()
        self.recording: Optional[pd.DataFrame] = None
        self.action_events: Optional[pd.DataFrame] = None
        self.screenshots: Optional[pd.DataFrame] = None
        self.window_events: Optional[pd.DataFrame] = None
        self.audio_info: Optional[pd.DataFrame] = None
        self.performance_stats: Optional[pd.DataFrame] = None
        self.memory_stats: Optional[pd.DataFrame] = None
        self.browser_events: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self) -> "RecordingIngestion":
        """Read all tables. Returns self for chaining."""
        conn = sqlite3.connect(self.db_path)
        try:
            self.recording = self._load_recording(conn)
            self.action_events = self._load_action_events(conn)
            self.screenshots = self._load_screenshots(conn)
            self.window_events = self._load_window_events(conn)
            self.audio_info = self._load_audio_info(conn)
            self.performance_stats = self._load_performance_stats(conn)
            self.memory_stats = self._load_memory_stats(conn)
            self.browser_events = self._load_browser_events(conn)
        finally:
            conn.close()
        return self

    def all_tables(self) -> dict[str, pd.DataFrame]:
        """Return all DataFrames in a single dict."""
        if self.recording is None:
            self.load()
        return {
            "recording": self.recording,
            "action_events": self.action_events,
            "screenshots": self.screenshots,
            "window_events": self.window_events,
            "audio_info": self.audio_info,
            "performance_stats": self.performance_stats,
            "memory_stats": self.memory_stats,
            "browser_events": self.browser_events,
        }

    def summary(self) -> pd.DataFrame:
        """One-line per table: name, row count, column count."""
        tables = self.all_tables()
        rows = [
            {
                "table": name,
                "rows": len(df),
                "columns": len(df.columns),
                "columns_list": ", ".join(df.columns.tolist()),
            }
            for name, df in tables.items()
        ]
        return pd.DataFrame(rows).set_index("table")

    def decode_screenshot(self, row: pd.Series) -> "PIL.Image.Image":
        """Decode a row from self.screenshots into a PIL Image.

        Example
        -------
        img = ing.decode_screenshot(ing.screenshots.iloc[0])
        img.show()
        """
        try:
            from PIL import Image
        except ImportError:
            raise ImportError("pip install Pillow to decode screenshots")
        return Image.open(io.BytesIO(row["png_data"]))

    # ------------------------------------------------------------------
    # Private loaders
    # ------------------------------------------------------------------

    def _load_recording(self, conn: sqlite3.Connection) -> pd.DataFrame:
        df = pd.read_sql("SELECT * FROM recording", conn)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
        df["video_start_time"] = pd.to_datetime(df["video_start_time"], unit="s")
        # Parse JSON config column into a Python dict
        if "config" in df.columns:
            df["config"] = df["config"].apply(
                lambda v: json.loads(v) if isinstance(v, str) else v
            )
        return df

    def _load_action_events(self, conn: sqlite3.Connection) -> pd.DataFrame:
        df = pd.read_sql("SELECT * FROM action_event where mouse_pressed=True", conn)

        # Timestamp columns → datetime
        for col in ("timestamp", "recording_timestamp",
                    "screenshot_timestamp", "window_event_timestamp",
                    "browser_event_timestamp"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], unit="s")

        # Boolean columns
        for col in ("mouse_pressed", "disabled"):
            if col in df.columns:
                df[col] = df[col].astype("boolean")

        # JSON columns → dict / list
        for col in ("element_state", "available_segment_descriptions"):
            if col in df.columns:
                df[col] = df[col].apply(
                    lambda v: json.loads(v) if isinstance(v, str) else v
                )

        # Numeric mouse coords
        for col in ("mouse_x", "mouse_y", "mouse_dx", "mouse_dy"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        return df

    def _load_screenshots(self, conn: sqlite3.Connection) -> pd.DataFrame:
        """Load screenshot metadata + raw PNG blobs.

        Blob columns (png_data, png_diff_data, png_diff_mask_data) are kept
        as bytes. Use decode_screenshot() to turn them into PIL Images.
        """
        df = pd.read_sql("SELECT * FROM screenshot", conn)
        for col in ("timestamp", "recording_timestamp"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], unit="s")
        return df

    def _load_window_events(self, conn: sqlite3.Connection) -> pd.DataFrame:
        df = pd.read_sql("SELECT * FROM window_event", conn)
        for col in ("timestamp", "recording_timestamp"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], unit="s")
        if "state" in df.columns:
            df["state"] = df["state"].apply(
                lambda v: json.loads(v) if isinstance(v, str) else v
            )
        return df

    def _load_audio_info(self, conn: sqlite3.Connection) -> pd.DataFrame:
        df = pd.read_sql("SELECT * FROM audio_info", conn)
        for col in ("timestamp", "recording_timestamp"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], unit="s")
        if "words_with_timestamps" in df.columns:
            df["words_with_timestamps"] = df["words_with_timestamps"].apply(
                lambda v: json.loads(v) if isinstance(v, str) else v
            )
        return df

    def _load_performance_stats(self, conn: sqlite3.Connection) -> pd.DataFrame:
        df = pd.read_sql("SELECT * FROM performance_stat", conn)
        if "recording_timestamp" in df.columns:
            df["recording_timestamp"] = pd.to_datetime(
                df["recording_timestamp"], unit="s"
            )
        # start_time / end_time are stored as integer nanoseconds or microseconds
        for col in ("start_time", "end_time"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if {"start_time", "end_time"}.issubset(df.columns):
            df["duration_ms"] = (df["end_time"] - df["start_time"]) / 1e6
        return df

    def _load_memory_stats(self, conn: sqlite3.Connection) -> pd.DataFrame:
        df = pd.read_sql("SELECT * FROM memory_stat", conn)
        for col in ("timestamp", "recording_timestamp"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], unit="s")
        if "memory_usage_bytes" in df.columns:
            df["memory_usage_mb"] = df["memory_usage_bytes"] / (1024 ** 2)
        return df

    def _load_browser_events(self, conn: sqlite3.Connection) -> pd.DataFrame:
        df = pd.read_sql("SELECT * FROM browser_event", conn)
        for col in ("timestamp", "recording_timestamp"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], unit="s")
        if "message" in df.columns:
            df["message"] = df["message"].apply(
                lambda v: json.loads(v) if isinstance(v, str) else v
            )
        return df


# ------------------------------------------------------------------
# Quick demo when run directly
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from openadapt.config import settings

    # Use the most recent capture or a path passed as argument
    if len(sys.argv) == 1:
        print("here123")
        db_path=settings.capture_dir / "20260603_122250_Vid_Check" / "recording.db"
        #db_path = Path(sys.argv[1])
    else:
        print("here456")
        captures = sorted(settings.capture_dir.iterdir(), reverse=True)
        db_candidates = [d / "recording.db" for d in captures if (d / "recording.db").exists()]
        if not db_candidates:
            print("No recordings found in", settings.capture_dir)
            sys.exit(1)
        db_path = db_candidates[0]

    print(f"Loading: {db_path}\n")
    ing = RecordingIngestion(db_path).load()

    print("=== Summary ===")
    print(ing.summary().to_string())

    print("\n=== Recording metadata ===")
    meta = ing.recording.iloc[0]
    print(f"  Task:      {meta['task_description']}")
    print(f"  Recorded:  {meta['timestamp']}")
    print(f"  Platform:  {meta['platform']}")
    print(f"  Monitor:   {meta['monitor_width']}x{meta['monitor_height']}")

    print("\n=== Action events (first 5) ===")
    cols = ["id", "timestamp", "name", "mouse_x", "mouse_y",
            "key_name", "key_char", "mouse_button_name", "mouse_pressed"]
    print(ing.action_events[cols].head(20).to_string(index=False))

    print("\n=== Action event types ===")
    print(ing.action_events["name"].value_counts().to_string())
