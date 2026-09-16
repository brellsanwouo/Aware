"""SQLite knowledge persistence for Assess runs."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class SQLiteKnowledgeStore:
    """Tiny SQLite store for runs, events, task results and findings."""

    db_path: Path

    @classmethod
    def from_url(cls, db_url: str) -> "SQLiteKnowledgeStore":
        if not db_url.startswith("sqlite:///"):
            raise ValueError(f"Unsupported DB URL `{db_url}`. Only sqlite:///... is supported.")
        raw_path = db_url.removeprefix("sqlite:///")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        store = cls(db_path=path)
        store._ensure_schema()
        return store

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT,
                    source_path TEXT,
                    query TEXT,
                    db_url TEXT,
                    summary TEXT,
                    confidence REAL,
                    preliminary_causes_json TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    content TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    severity TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS component_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    component TEXT NOT NULL,
                    component_key TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_component_memory_key
                ON component_memory(component_key, created_at);
                """
            )

    def start_run(
        self,
        *,
        run_id: str,
        source_path: str,
        query: str,
        db_url: str | None,
    ) -> None:
        started_at = _now_utc()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO runs
                (run_id, started_at, status, source_path, query, db_url)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, started_at, "running", source_path, query, db_url or ""),
            )

    def append_event(
        self,
        *,
        run_id: str,
        ts: str,
        sender: str,
        recipient: str,
        phase: str,
        content: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO events (run_id, ts, sender, recipient, phase, content)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, ts, sender, recipient, phase, content),
            )

    def append_task_result(
        self,
        *,
        run_id: str,
        agent_name: str,
        target_path: str,
        status: str,
        detail: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_results (run_id, agent_name, target_path, status, detail)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, agent_name, target_path, status, detail),
            )

    def append_finding(
        self,
        *,
        run_id: str,
        agent_name: str,
        kind: str,
        source: str,
        summary: str,
        evidence: list[str],
        severity: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO findings
                (run_id, agent_name, kind, source, summary, evidence_json, severity)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    agent_name,
                    kind,
                    source,
                    summary,
                    json.dumps(evidence, ensure_ascii=False),
                    severity,
                ),
            )

    def append_component_memory(
        self,
        *,
        component: str,
        run_id: str,
        agent_name: str,
        source: str,
        summary: str,
        evidence: list[str],
        severity: str,
    ) -> None:
        """Persist one memory entry tied to a specific component."""
        clean_component = component.strip()
        if not clean_component:
            return
        component_key = clean_component.lower()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO component_memory
                (
                    component,
                    component_key,
                    run_id,
                    agent_name,
                    source,
                    summary,
                    evidence_json,
                    severity,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_component,
                    component_key,
                    run_id,
                    agent_name,
                    source,
                    summary,
                    json.dumps(evidence, ensure_ascii=False),
                    severity,
                    _now_utc(),
                ),
            )

    def search_component_memory(self, *, component: str, limit: int = 20) -> list[dict[str, object]]:
        """Return most recent memory entries for one component (cross-run)."""
        clean = component.strip()
        if not clean:
            return []
        key = clean.lower()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT component, run_id, agent_name, source, summary, evidence_json, severity, created_at
                FROM component_memory
                WHERE component_key = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (key, max(1, int(limit))),
            ).fetchall()
        out: list[dict[str, object]] = []
        for row in rows:
            try:
                evidence = json.loads(str(row["evidence_json"]))
            except Exception:
                evidence = []
            out.append(
                {
                    "component": str(row["component"]),
                    "run_id": str(row["run_id"]),
                    "agent_name": str(row["agent_name"]),
                    "source": str(row["source"]),
                    "summary": str(row["summary"]),
                    "evidence": evidence if isinstance(evidence, list) else [],
                    "severity": str(row["severity"]),
                    "created_at": str(row["created_at"]),
                }
            )
        return out

    def finish_run(
        self,
        *,
        run_id: str,
        status: str,
        summary: str | None = None,
        confidence: float | None = None,
        preliminary_causes: list[str] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET ended_at = ?, status = ?, summary = ?, confidence = ?, preliminary_causes_json = ?
                WHERE run_id = ?
                """,
                (
                    _now_utc(),
                    status,
                    summary or "",
                    confidence if confidence is not None else None,
                    json.dumps(preliminary_causes or [], ensure_ascii=False),
                    run_id,
                ),
            )


def resolve_db_url(explicit_db_url: str | None = None) -> str:
    """Resolve DB URL from explicit value or env, with safe SQLite default."""
    if explicit_db_url and explicit_db_url.strip():
        return explicit_db_url.strip()
    env_url = os.getenv("ASSESS_DB_URL", "").strip()
    if env_url:
        return env_url
    filename = "assess_v3.db"
    default_path = (Path.cwd() / "output" / filename).resolve()
    return f"sqlite:///{default_path}"


def maybe_create_knowledge_store(explicit_db_url: str | None = None) -> SQLiteKnowledgeStore | None:
    """Create SQLite knowledge store or return None if configuration is invalid."""
    db_url = resolve_db_url(explicit_db_url)
    try:
        return SQLiteKnowledgeStore.from_url(db_url)
    except Exception:
        return None


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()
