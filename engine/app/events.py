"""State-change events: stored in the events table, pushed to SSE subscribers only after commit."""
import asyncio
import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from .db import utcnow
from .models import Event

_subscribers: set[asyncio.Queue[str]] = set()
_loop: asyncio.AbstractEventLoop | None = None
_on_change: list[Callable[[], None]] = []        # e.g. spreadsheet exporter: called after any committed change


def on_change(fn: Callable[[], None]) -> None:
    _on_change.append(fn)


def clear_on_change() -> None:
    _on_change.clear()


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def emit(s: Session, type_: str, **data: Any) -> None:
    ev = Event(ts=utcnow(), type=type_, data=data)
    s.add(ev)
    s.info.setdefault("pending_events", []).append(ev)


@sa_event.listens_for(Session, "after_commit")
def _after_commit(s: Session) -> None:
    evs = s.info.pop("pending_events", [])
    if evs:
        for fn in list(_on_change):
            try:
                fn()
            except RuntimeError:           # loop already closed (shutdown)
                pass
    for ev in evs:
        _publish(f"event: {ev.type}\ndata: {json.dumps({'type': ev.type, 'ts': ev.ts.isoformat() + 'Z', **ev.data})}\n\n")


@sa_event.listens_for(Session, "after_rollback")
def _after_rollback(s: Session) -> None:
    s.info.pop("pending_events", None)


def _publish(msg: str) -> None:
    if _loop is None:
        return
    for q in list(_subscribers):
        _loop.call_soon_threadsafe(_offer, q, msg)


def _offer(q: asyncio.Queue[str], msg: str) -> None:
    if q.full():                       # slow client: drop oldest
        q.get_nowait()
    q.put_nowait(msg)


def subscribe() -> asyncio.Queue[str]:
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue[str]) -> None:
    _subscribers.discard(q)
