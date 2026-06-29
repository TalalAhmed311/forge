"""Short-term episodic memory — ordered, tagged session events.

Every agent action becomes an `EpisodicEvent` tagged with the agent, the task, and
the event type, appended to an ordered log. That tagging is what lets each agent
get a precise slice at handoff (e.g. "the failure trace for T4", "the backend's
tool_results for the endpoints this screen calls") instead of the whole transcript.

Two implementations behind one interface:
  * `InMemoryEpisodicLog` — process-local; the fully-tested reference + the
    graceful fallback when Redis is unreachable (the tracker on disk is the real
    durability backstop, so losing this is not fatal).
  * `RedisEpisodicLog` — Redis Streams, one per project session, with a TTL so
    short-term memory is genuinely ephemeral and survives `forge resume` (a new
    process) while Redis is up.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Optional

# Event types we record. Durable kinds (spec/decision/tool_result/summary) are the
# ones promoted to long-term; "reasoning" is kept short-term only.
EVENT_TYPES = (
    "plan", "reasoning", "tool_call", "tool_result",
    "test_result", "escalation", "decision", "handoff", "summary",
)


@dataclass
class EpisodicEvent:
    session_id: str
    agent: str                 # architect | engineer | frontend_engineer | clarifier | orchestrator
    type: str                  # one of EVENT_TYPES
    content: str
    task_id: Optional[str] = None
    seq: int = 0               # assigned by the log on append
    ts: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "EpisodicEvent":
        return cls(**json.loads(raw))


class EpisodicLog(ABC):
    @abstractmethod
    def append(self, event: EpisodicEvent) -> int: ...

    @abstractmethod
    def tail(self, n: int = 12) -> list[EpisodicEvent]: ...

    @abstractmethod
    def all(self) -> list[EpisodicEvent]: ...

    def by_task(self, task_id: str, types: Optional[tuple] = None) -> list[EpisodicEvent]:
        out = [e for e in self.all() if e.task_id == task_id]
        if types is not None:
            out = [e for e in out if e.type in types]
        return out

    def by_agent(self, agent: str) -> list[EpisodicEvent]:
        return [e for e in self.all() if e.agent == agent]


class InMemoryEpisodicLog(EpisodicLog):
    def __init__(self) -> None:
        self._events: list[EpisodicEvent] = []

    def append(self, event: EpisodicEvent) -> int:
        event.seq = len(self._events)
        self._events.append(event)
        return event.seq

    def tail(self, n: int = 12) -> list[EpisodicEvent]:
        return self._events[-n:]

    def all(self) -> list[EpisodicEvent]:
        return list(self._events)


class RedisEpisodicLog(EpisodicLog):
    """Redis Streams backend. One stream per project session, TTL-refreshed.

    `client` is a `redis.Redis` (or compatible). Import is the caller's job so the
    package stays importable without the redis dependency.
    """

    def __init__(self, client, project: str, session_id: str, ttl_seconds: int = 604800) -> None:
        self.client = client
        self.key = f"forge:{project}:session:{session_id}:events"
        self.ttl = ttl_seconds

    def append(self, event: EpisodicEvent) -> int:
        # Stream id is time-ordered; we also keep our own seq for stable ordering.
        event.seq = self.client.xlen(self.key)
        self.client.xadd(self.key, {"e": event.to_json()})
        self.client.expire(self.key, self.ttl)  # refresh ephemerality on activity
        return event.seq

    def all(self) -> list[EpisodicEvent]:
        rows = self.client.xrange(self.key)
        return [EpisodicEvent.from_json(_field(fields, "e")) for _id, fields in rows]

    def tail(self, n: int = 12) -> list[EpisodicEvent]:
        rows = self.client.xrevrange(self.key, count=n)
        events = [EpisodicEvent.from_json(_field(fields, "e")) for _id, fields in rows]
        return list(reversed(events))


def _field(fields: dict, key: str) -> str:
    """Read a stream field, tolerating bytes keys/values from redis-py."""

    if key in fields:
        val = fields[key]
    else:
        val = fields.get(key.encode())
    return val.decode() if isinstance(val, (bytes, bytearray)) else val
