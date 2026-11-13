"""Append-only, hash-chained audit log.

Every entry records its predecessor's hash, so any edit, deletion or
reordering breaks `verify()`. Entries are JSON lines so the log stays
human-readable and greppable.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AuditEntry:
    seq: int
    ts_ms: int
    run_id: str
    event: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str = ""

    def compute_hash(self) -> str:
        canonical = json.dumps(
            {
                "seq": self.seq,
                "ts_ms": self.ts_ms,
                "run_id": self.run_id,
                "event": self.event,
                "payload": self.payload,
                "prev_hash": self.prev_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts_ms": self.ts_ms,
            "run_id": self.run_id,
            "event": self.event,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditEntry":
        return cls(
            seq=data["seq"],
            ts_ms=data["ts_ms"],
            run_id=data["run_id"],
            event=data["event"],
            payload=data["payload"],
            prev_hash=data["prev_hash"],
            hash=data["hash"],
        )


class AuditLog:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.entries: list[AuditEntry] = []
        if self.path and self.path.exists():
            self.load()

    def append(self, ts_ms: int, run_id: str, event: str,
               payload: dict[str, Any] | None = None) -> AuditEntry:
        prev_hash = self.entries[-1].hash if self.entries else "0" * 64
        entry = AuditEntry(
            seq=len(self.entries) + 1,
            ts_ms=ts_ms,
            run_id=run_id,
            event=event,
            payload=payload or {},
            prev_hash=prev_hash,
        )
        entry.hash = entry.compute_hash()
        self.entries.append(entry)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")
        return entry

    def load(self) -> None:
        assert self.path is not None
        self.entries = [
            AuditEntry.from_dict(json.loads(line))
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def verify(self) -> tuple[bool, str]:
        """Recompute the chain. Returns (ok, message)."""
        prev = "0" * 64
        for i, entry in enumerate(self.entries, start=1):
            if entry.seq != i:
                return False, f"entry {i}: seq mismatch ({entry.seq})"
            if entry.prev_hash != prev:
                return False, f"entry {i}: prev_hash mismatch (chain broken)"
            if entry.hash != entry.compute_hash():
                return False, f"entry {i}: hash mismatch (tampered payload)"
            prev = entry.hash
        return True, f"chain verified: {len(self.entries)} entries"

    def for_run(self, run_id: str) -> list[AuditEntry]:
        return [e for e in self.entries if e.run_id == run_id]
