from __future__ import annotations

from collections.abc import Sequence

from manager.engine.models import Event, Position

_EPS = 1e-8  # mirrors the EA's NormalizeDouble(..., 8) comparison tolerance


def diff(prev: Sequence[Position], curr: Sequence[Position]) -> list[Event]:
    """Diff two snapshots keyed by master ticket.

    - ticket in curr but not prev -> NEW
    - ticket in both, volume decreased -> PARTIAL (current position)
    - ticket in both, sl or tp changed -> MODIFY (current position)
      (PARTIAL and MODIFY can both fire for one position)
    - ticket in prev but not curr -> CLOSE (previous position)

    Events are emitted in curr order (NEW/PARTIAL/MODIFY) then prev order
    (CLOSE), matching SlaveSubscriber::DiffAndProcess.
    """
    prev_by_ticket = {p.ticket: p for p in prev}
    curr_by_ticket = {p.ticket: p for p in curr}
    events: list[Event] = []

    for c in curr:
        p = prev_by_ticket.get(c.ticket)
        if p is None:
            events.append(Event(kind="NEW", position=c))
            continue
        if round(p.volume - c.volume, 8) > _EPS:
            events.append(Event(kind="PARTIAL", position=c))
        if abs(round(p.sl - c.sl, 8)) > _EPS or abs(round(p.tp - c.tp, 8)) > _EPS:
            events.append(Event(kind="MODIFY", position=c))

    for p in prev:
        if p.ticket not in curr_by_ticket:
            events.append(Event(kind="CLOSE", position=p))

    return events
