# manager/worker/ib/contracts.py
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime

_ALLOWED_SECTYPES = ("FUT", "STK", "CASH", "IND", "CMDTY")


@dataclass(frozen=True)
class ContractSpec:
    """One slave contract as configured in an IB slave's contract map row."""
    symbol: str
    exchange: str
    sec_type: str
    master_point_value: float   # master CFD: dollars per point per lot
    currency: str = "USD"


def parse_contract_map(raw=None) -> dict[str, ContractSpec]:
    out: dict[str, ContractSpec] = {}
    for symbol, d in (raw or {}).items():
        if not isinstance(d, dict):
            raise ValueError(f"contract map: row for {symbol!r} is not a mapping")
        try:
            spec = ContractSpec(
                symbol=str(symbol),
                exchange=str(d["exchange"]).upper(),
                sec_type=str(d["sec_type"]).upper(),
                master_point_value=float(d["master_point_value"]),
                currency=str(d.get("currency", "USD")).upper(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"contract map: bad row for {symbol!r}: {exc}") from exc
        if spec.sec_type not in _ALLOWED_SECTYPES:
            raise ValueError(
                f"contract map: sec_type {spec.sec_type!r} not allowed "
                f"for {symbol!r} (allowed: {', '.join(_ALLOWED_SECTYPES)})")
        if spec.master_point_value <= 0.0:
            raise ValueError(
                f"contract map: master_point_value must be > 0 for {symbol!r}")
        out[spec.symbol] = spec
    return out


def pick_front_month(candidates, today: str, roll_days: int = 5):
    """candidates: [(expiry, open_interest|None)], expiry 'YYYYMMDD' or 'YYYYMM';
    today 'YYYYMMDD'. Among contracts not yet expired: highest open interest
    wins (ties -> later month); with no usable interest data, nearest expiry.
    Inside the roll window (<= roll_days to expiry) the NEXT month is picked
    for new orders instead. Returns (expiry, rolling) or None."""
    future = sorted((c, oi) for c, oi in candidates
                    if c[:6] >= today[:6] and (oi is None or oi is not False))
    if not future:
        return None
    with_oi = [(c, float(oi)) for c, oi in future if oi and float(oi) > 0.0]
    chosen = (max(with_oi, key=lambda t: (t[1], t[0]))[0] if with_oi
              else future[0][0])
    rolling = False
    if _days_until(chosen, today) <= roll_days:
        later = [c for c, _ in future if c > chosen]
        if later:
            chosen, rolling = later[0], True
    return chosen, rolling


def _days_until(expiry: str, today: str) -> int:
    if len(expiry) == 8:
        ed = datetime.strptime(expiry, "%Y%m%d").date()
    else:
        y, m = int(expiry[:4]), int(expiry[4:6])
        ed = date(y, m, calendar.monthrange(y, m)[1])
    return (ed - datetime.strptime(today, "%Y%m%d").date()).days