"""Kanban Recovery Controller — observe-only audit ledger.

Append-only ledger that records every guardrail-halt and budget-exhausted
event for offline analysis and future automated recovery.  This module is a
**pure library**: ``record_recovery_event`` writes an audit line and applies
dedup/cooldown checks but never mutates kanban task state.

Ledger location: ``$HERMES_HOME/kanban/recovery/``

Ledger entries are keyed by ``(task_id, run_id, exit_class, fingerprint,
action_type)``.  Duplicate entries (same key within the cooldown window)
are silently dropped.

``action_type`` is informational only — the controller never applies
non-readonly actions yet.  The field exists so the ledger schema is
ready for the enforcement phase.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None  # Windows — use create-exclusive lockfile fallback


# ---------------------------------------------------------------------------
# Cross-platform file locking
# ---------------------------------------------------------------------------

class _FileLock:
    """Cross-platform exclusive file lock using create-exclusive on all platforms.

    Uses os.open(O_CREAT | O_EXCL) which is atomic on POSIX and Windows.
    Lock is represented by the existence of the lock file.  Stale locks
    (leftover from crashed processes) are detected by age and cleaned up.
    """

    def __init__(self, lock_path: Path, timeout: float = 2.0, stale_age: float = 30.0):
        self._lock_path = lock_path
        self._timeout = timeout
        self._stale_age = stale_age
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        """Acquire the exclusive lock, retrying until timeout.

        Raises TimeoutError if the lock cannot be acquired within the timeout.
        Raises no broad exceptions — only TimeoutError or OSError on real I/O failure.
        """
        deadline = time.monotonic() + self._timeout
        while True:
            # Clean stale lock files older than stale_age
            self._clean_stale()
            try:
                # Atomic create-exclusive: fails if file already exists
                self._fd = os.open(
                    str(self._lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o666,
                )
                # Write PID + timestamp for stale detection
                os.write(self._fd, f"{os.getpid()}\n{time.time()}\n".encode())
                os.fsync(self._fd)
                return
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Could not acquire lock {self._lock_path} within {self._timeout}s"
                    )
                time.sleep(0.01)  # 10ms backoff

    def release(self) -> None:
        """Release the lock by closing the fd and removing the lock file."""
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            os.unlink(str(self._lock_path))
        except FileNotFoundError:
            pass
        except OSError:
            pass  # Lock file may have been cleaned by another process

    def _clean_stale(self) -> None:
        """Remove lock files older than stale_age seconds.

        A newly created lock may be visible before its owner writes the metadata,
        so malformed/empty contents are never considered stale on their own.
        """
        try:
            stat = self._lock_path.stat()
        except FileNotFoundError:
            return
        if time.time() - stat.st_mtime <= self._stale_age:
            return
        try:
            os.unlink(str(self._lock_path))
        except FileNotFoundError:
            pass

    def __enter__(self) -> "_FileLock":
        self.acquire()
        return self

    def __exit__(self, *args: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Ledger helpers
# ---------------------------------------------------------------------------

def _recovery_ledger_dir(hermes_home: Optional[str] = None) -> Path:
    """Return the recovery ledger directory, creating it if necessary."""
    home = hermes_home or os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    d = Path(home) / "kanban" / "recovery"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ledger_entry_key(
    task_id: str,
    run_id: Any,
    exit_class: str,
    fingerprint: str,
    action_type: str,
) -> str:
    """Deterministic string key for dedup."""
    parts = f"{task_id}:{run_id}:{exit_class}:{fingerprint}:{action_type}"
    return hashlib.sha256(parts.encode()).hexdigest()[:16]


def _load_dedup_index(dedup_file: Path) -> dict[str, float]:
    """Load dedup index, failing closed on malformed data."""
    if not dedup_file.exists():
        return {}
    try:
        raw = json.loads(dedup_file.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {k: float(v) for k, v in raw.items()}
    except (json.JSONDecodeError, ValueError, TypeError, OSError):
        pass  # Malformed dedup index — fail closed (treat as empty)
    return {}


def _save_dedup_index(dedup_file: Path, index: dict[str, float]) -> None:
    """Atomically save dedup index using temp file replace."""
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(dedup_file.parent), suffix=".tmp", prefix=".dedup_"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(dedup_file))
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Core API — pure library, no side effects beyond ledger writes
# ---------------------------------------------------------------------------

def record_recovery_event(
    *,
    task_id: str,
    run_id: Any,
    exit_class: str,
    fingerprint: str,
    action_type: str,
    event_data: Optional[dict] = None,
    cooldown_seconds: int = 300,
    hermes_home: Optional[str] = None,
) -> dict:
    """Record a recovery event in the append-only audit ledger.

    Uses create-exclusive lockfile with atomic temp-file replace for
    concurrency safety.  Fail-closed on malformed dedup index.

    Returns a dict with:
      * ``recorded``: True if the entry was written, False if dropped by dedup.
      * ``entry_key``: the dedup key.
      * ``ledger_path``: the ledger file path.
      * ``reason``: why the entry was recorded or dropped.
    """
    entry_key = _ledger_entry_key(
        task_id, run_id, exit_class, fingerprint, action_type,
    )
    ledger_dir = _recovery_ledger_dir(hermes_home)
    lock_file_path = ledger_dir / ".recovery.lock"
    dedup_file = ledger_dir / ".dedup_index.json"
    ledger_file = ledger_dir / "recovery_ledger.jsonl"

    lock = _FileLock(lock_file_path)
    try:
        lock.acquire()
    except TimeoutError:
        return {
            "recorded": False,
            "entry_key": entry_key,
            "ledger_path": str(ledger_file),
            "reason": "lock_timeout",
        }

    try:
        # Load dedup index atomically
        dedup_index = _load_dedup_index(dedup_file)

        # Cooldown check
        now = time.time()
        last_seen = dedup_index.get(entry_key)
        if last_seen is not None and (now - last_seen) < cooldown_seconds:
            return {
                "recorded": False,
                "entry_key": entry_key,
                "ledger_path": str(ledger_file),
                "reason": f"cooldown_active (last_seen={last_seen:.0f}, "
                          f"cooldown={cooldown_seconds}s)",
            }

        # Build entry
        entry = {
            "ts": now,
            "task_id": task_id,
            "run_id": run_id,
            "exit_class": exit_class,
            "fingerprint": fingerprint,
            "action_type": action_type,
            "event_data": event_data or {},
        }

        # Append to ledger (one JSONL line while holding lock)
        with open(ledger_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

        # Update dedup index atomically
        dedup_index[entry_key] = now
        _save_dedup_index(dedup_file, dedup_index)

        return {
            "recorded": True,
            "entry_key": entry_key,
            "ledger_path": str(ledger_file),
            "reason": "recorded",
        }
    finally:
        lock.release()


def is_cooling_down(
    *,
    task_id: str,
    run_id: Any,
    exit_class: str,
    fingerprint: str,
    action_type: str,
    cooldown_seconds: int = 300,
    hermes_home: Optional[str] = None,
) -> bool:
    """Check if a recovery event is within its cooldown window.

    Read-only: does not write to the ledger or dedup index.
    """
    entry_key = _ledger_entry_key(
        task_id, run_id, exit_class, fingerprint, action_type,
    )
    ledger_dir = _recovery_ledger_dir(hermes_home)
    dedup_file = ledger_dir / ".dedup_index.json"
    dedup_index = _load_dedup_index(dedup_file)
    last_seen = dedup_index.get(entry_key)
    if last_seen is None:
        return False
    return (time.time() - last_seen) < cooldown_seconds


def read_ledger(
    *,
    hermes_home: Optional[str] = None,
    task_id: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Read recent ledger entries, optionally filtered by task_id.

    Read-only: does not modify the ledger.
    """
    ledger_dir = _recovery_ledger_dir(hermes_home)
    ledger_file = ledger_dir / "recovery_ledger.jsonl"
    if not ledger_file.exists():
        return []
    entries: list[dict] = []
    try:
        with open(ledger_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if task_id and entry.get("task_id") != task_id:
                    continue
                entries.append(entry)
    except Exception:
        return []
    # Return most recent first, capped at limit
    return list(reversed(entries[-limit:]))


def clear_cooldown(
    *,
    task_id: str,
    run_id: Any,
    exit_class: str,
    fingerprint: str,
    action_type: str,
    hermes_home: Optional[str] = None,
) -> bool:
    """Remove a single cooldown entry from the dedup index.

    Read-only in the sense that it only touches the dedup index,
    never the ledger itself.
    """
    entry_key = _ledger_entry_key(
        task_id, run_id, exit_class, fingerprint, action_type,
    )
    ledger_dir = _recovery_ledger_dir(hermes_home)
    lock_file_path = ledger_dir / ".recovery.lock"
    dedup_file = ledger_dir / ".dedup_index.json"

    lock = _FileLock(lock_file_path)
    try:
        lock.acquire()
    except TimeoutError:
        return False

    try:
        dedup_index = _load_dedup_index(dedup_file)
        if entry_key not in dedup_index:
            return False
        del dedup_index[entry_key]
        _save_dedup_index(dedup_file, dedup_index)
        return True
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Plugin registration — called by PluginManager when loading this plugin
# ---------------------------------------------------------------------------

def register(ctx: Any) -> None:
    """Register recovery controller hooks with the lifecycle system.

    Called by PluginManager._load_plugin_scoped via plugin.yaml manifest.
    Registers real callbacks with ctx.register_hook — never declares
    unsupported hooks.
    """
    from plugins.kanban.recovery.lifecycle_hooks import (
        on_worker_exited,
        on_dispatch_tick,
    )

    ctx.register_hook("on_kanban_worker_exited", on_worker_exited)
    ctx.register_hook("on_kanban_dispatch_tick", on_dispatch_tick)

