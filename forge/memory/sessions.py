"""Session registry — `.forge/sessions.json`.

Each `forge run` is a session with a stable id (`S1`, `S2`, …). The registry is
what makes a run on an existing project a *continuation* rather than a fresh
start: task ids are namespaced by session (so they never collide across runs),
and a new session can see what prior ones did.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Session:
    id: str
    started: str
    prompt: str = ""
    goal: str = ""
    tasks: list = field(default_factory=list)   # task ids produced this session
    status: str = "in_progress"                 # in_progress | done | failed | needs_user


class SessionRegistry:
    def __init__(self, path: str) -> None:
        self.path = path

    def all(self) -> list[Session]:
        if not os.path.isfile(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return []
        return [Session(**s) for s in data if isinstance(s, dict)]

    def has_prior(self) -> bool:
        return len(self.all()) > 0

    def next_id(self) -> str:
        """Next session id as `S{n}`, one past the highest existing number."""

        highest = 0
        for s in self.all():
            m = re.fullmatch(r"S(\d+)", s.id)
            if m:
                highest = max(highest, int(m.group(1)))
        return f"S{highest + 1}"

    def get(self, session_id: str) -> Optional[Session]:
        return next((s for s in self.all() if s.id == session_id), None)

    def start(self, session_id: str, prompt: str) -> None:
        sessions = self.all()
        if any(s.id == session_id for s in sessions):
            return  # idempotent
        sessions.append(Session(id=session_id, started=_now(), prompt=prompt[:300]))
        self._write(sessions)

    def finish(self, session_id: str, status: str, goal: str = "",
               tasks: Optional[list] = None) -> None:
        sessions = self.all()
        for s in sessions:
            if s.id == session_id:
                s.status = status
                if goal:
                    s.goal = goal[:300]
                if tasks is not None:
                    s.tasks = tasks
        self._write(sessions)

    def _write(self, sessions: list[Session]) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".sessions-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump([asdict(s) for s in sessions], fh, indent=2)
        os.replace(tmp, self.path)


# Schema is also in db/init.sql (which only runs on a fresh docker volume). We
# CREATE IF NOT EXISTS here too so the table appears on already-running volumes.
_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT        NOT NULL,
    project     TEXT        NOT NULL,
    prompt      TEXT        NOT NULL DEFAULT '',
    goal        TEXT        NOT NULL DEFAULT '',
    status      TEXT        NOT NULL DEFAULT 'in_progress',
    tasks       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    UNIQUE (project, session_id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions (project);
"""


class PgSessionStore:
    """Mirrors the session registry into Postgres (table `sessions`).

    A thin companion to the JSON `SessionRegistry`: the file stays the durability
    backstop, this makes sessions queryable next to the documents they produced
    (join on project + session_id). Best-effort by design — a DB hiccup must never
    break a run — but failures are surfaced via `last_error` instead of swallowed
    silently, so "no rows" is diagnosable.
    """

    def __init__(self, conn, project: str) -> None:
        self.conn = conn
        self.project = project
        self.last_error: Optional[str] = None
        self.ensure_schema()

    def ensure_schema(self) -> bool:
        try:
            with self.conn.cursor() as cur:
                cur.execute(_SESSIONS_DDL)
            self.conn.commit()
            return True
        except Exception as exc:  # noqa: BLE001 — best-effort, but recorded
            self.last_error = f"ensure_schema: {exc}"
            try:
                self.conn.rollback()
            except Exception:
                pass
            return False

    def start(self, session_id: str, prompt: str) -> bool:
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sessions (session_id, project, prompt)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (project, session_id) DO NOTHING
                    """,
                    (session_id, self.project, prompt[:2000]),
                )
            self.conn.commit()
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"start: {exc}"
            try:
                self.conn.rollback()
            except Exception:
                pass
            return False

    def finish(self, session_id: str, status: str, goal: str = "",
               tasks: Optional[list] = None) -> bool:
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sessions
                       SET status = %s, goal = %s, tasks = %s::jsonb,
                           finished_at = now()
                     WHERE project = %s AND session_id = %s
                    """,
                    (status, goal[:2000], json.dumps(tasks or []),
                     self.project, session_id),
                )
            self.conn.commit()
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"finish: {exc}"
            try:
                self.conn.rollback()
            except Exception:
                pass
            return False

    def all(self) -> list[dict]:
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT session_id, prompt, goal, status, tasks, "
                    "started_at, finished_at FROM sessions "
                    "WHERE project = %s ORDER BY started_at",
                    (self.project,),
                )
                cols = ("session_id", "prompt", "goal", "status", "tasks",
                        "started_at", "finished_at")
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"all: {exc}"
            return []
