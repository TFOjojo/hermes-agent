"""Guarded ``state_meta`` transactions for SessionDB.

``set_meta`` is a blind upsert: two writers that read the same row both "win", the second
silently clobbering the first. Callers that must not lose an update (the goal subsystem's
compare-and-swap and its atomic session transfer) need read-compare-write as ONE transaction, so
a row that moved between the caller's read and the write loses the guard instead of being
overwritten — and a failure mid-transaction leaves nothing behind.

Mixin bound onto ``SessionDB`` via the MRO; built on its ``_execute_write`` primitive, which owns
BEGIN IMMEDIATE, the jittered lock retry and the commit. The callback re-reads inside the
transaction, so it stays idempotent under ``_execute_write``'s whole-callback retry.
"""

from __future__ import annotations

import logging
from typing import Dict, Mapping, Optional

# Log-record parity with the origin module (caplog tests pin "hermes_state").
logger = logging.getLogger("hermes_state")

COMMITTED = "committed"
CONFLICT = "conflict"

_META_VALUE_SQL = "SELECT value FROM state_meta WHERE key = ?"


class SessionMetaTxnMixin:
    """Atomic guarded exchange over ``state_meta`` keys."""

    def exchange_meta(
        self, guards: Mapping[str, Optional[str]], writes: Mapping[str, str],
        *, patience_s: Optional[float] = None,
    ) -> str:
        """Upsert every ``writes`` entry only while every ``guards`` entry still holds exactly
        the value it was read with (``None`` means the row must be absent). Returns
        ``"committed"`` or ``"conflict"``; on conflict NOTHING is written.

        The comparison and the writes share one ``BEGIN IMMEDIATE``, so a concurrent writer that
        lands between the caller's read and this call is detected here rather than after the
        damage. Raises whatever the store raises (lock exhaustion, I/O) — the transaction is
        rolled back and no partial state survives.
        """
        guards, writes = dict(guards or {}), dict(writes or {})

        def _exchange(conn):
            for key, expected in guards.items():
                row = conn.execute(_META_VALUE_SQL, (key,)).fetchone()
                if (None if row is None else row[0]) != expected:
                    return CONFLICT
            for key, value in writes.items():
                # cursor= keeps the upsert inline: a nested BEGIN IMMEDIATE would deadlock.
                self.set_meta(key, value, cursor=conn)
            return COMMITTED

        return self._execute_write(_exchange, patience_s=patience_s)
