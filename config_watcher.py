"""
config_watcher.py — Hot-reloading config singleton for strategy-config.json.

Usage:
    from config_watcher import config

    val = config.get("notional_limits.total_notional_cap", 3710)
    config.register_callback(lambda cfg: print("Config reloaded!", cfg))
    config.reload_now()
"""

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional


class ConfigWatcher:
    """
    Watches a JSON config file for changes (via mtime polling) and
    reloads it automatically.  Thread-safe reads via a RLock.
    """

    def __init__(self, path: str, interval: float = 5.0) -> None:
        self._path = path
        self._interval = interval
        self._lock = threading.RLock()
        self._config: Dict[str, Any] = {}
        self._mtime: float = 0.0
        self._callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Load once synchronously at import time so it's immediately usable.
        self._load()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Start the background polling thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name=f"ConfigWatcher:{self._path}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the background thread to stop."""
        self._stop_event.set()

    def reload_now(self) -> None:
        """Force an immediate reload regardless of mtime."""
        self._load(force=True)

    def get(self, key: str, default: Any = None) -> Any:
        """
        Read a config value.  Supports dotted nested keys, e.g.
        ``config.get("notional_limits.total_notional_cap", 3710)``
        """
        with self._lock:
            parts = key.split(".")
            node: Any = self._config
            for part in parts:
                if not isinstance(node, dict):
                    return default
                node = node.get(part)
                if node is None:
                    return default
            return node

    def register_callback(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        """Register *fn* to be called (with the new config dict) after each reload."""
        with self._lock:
            self._callbacks.append(fn)

    @property
    def data(self) -> Dict[str, Any]:
        """Return a shallow copy of the current config dict."""
        with self._lock:
            return dict(self._config)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _load(self, force: bool = False) -> None:
        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            # File not yet present — leave existing config in place.
            return

        with self._lock:
            if not force and mtime == self._mtime:
                return  # Nothing changed.

            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    new_cfg = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                # Don't clobber the good config if the file is mid-write.
                print(f"[ConfigWatcher] Failed to reload {self._path}: {exc}")
                return

            self._config = new_cfg
            self._mtime = mtime
            callbacks = list(self._callbacks)  # snapshot under lock

        # Fire callbacks outside the lock to avoid deadlocks.
        for cb in callbacks:
            try:
                cb(new_cfg)
            except Exception as exc:  # noqa: BLE001
                print(f"[ConfigWatcher] Callback {cb!r} raised: {exc}")

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(self._interval):
            self._load()


# --------------------------------------------------------------------------- #
# Global singleton — import and use directly.                                 #
# --------------------------------------------------------------------------- #

config = ConfigWatcher("strategy-config.json")
config.start()
