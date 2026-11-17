from __future__ import annotations

import re
from dataclasses import dataclass

from manager.engine.transform import SymbolMapper

MONTH_CODES = "FGHJKMNQUVXZ"


class SymbolAssistError(ValueError):
    """The examples cannot be turned into a rule (or a rule does not validate)."""


@dataclass(frozen=True)
class SymbolRule:
    master_pattern: str
    slave_template: str


@dataclass(frozen=True)
class RuleConflict:
    """An existing row that wins for one of the new rule's examples."""

    master_example: str
    expected_slave: str
    actual_slave: str
    row_index: int
    exact_tier: bool


def _escape_template(text: str) -> str:
    """Escape literal dollars for Match.expand templates (`$n` is a group,
    `$$` a literal `$`)."""
    return text.replace("$", "$$")


def _split_master(master: str) -> tuple[str, str, str, str]:
    """Return (prefix, month, year, suffix) for a dated contract symbol."""
    for i in range(len(master) - 2, -1, -1):
        if master[i] in MONTH_CODES and master[i + 1].isdigit():
            run = re.match(r"\d+", master[i + 1:])
            assert run is not None
            year = run.group(0)
            if not 2 <= len(year) <= 4:
                raise SymbolAssistError(
                    f"the master example {master!r} needs a 2-4 digit year")
            prefix = master[:i]
            if not prefix:
                raise SymbolAssistError(
                    f"the master example {master!r} has no symbol root")
            return prefix, master[i], year, master[i + 1 + len(year):]
    raise SymbolAssistError(
        f"the master example {master!r} needs a month code and a year, "
        "e.g. YMZ26")


def _split_slave(slave: str, year_width: int
                 ) -> tuple[str, str, str]:
    """Return (prefix, year, suffix) for the slave example."""
    idx = max((i for i, ch in enumerate(slave) if ch in MONTH_CODES),
              default=-1)
    if idx < 0:
        raise SymbolAssistError(
            f"the slave example {slave!r} needs a month code, e.g. YM_Z")
    prefix, tail = slave[:idx], slave[idx + 1:]
    run = re.match(r"\d+", tail)
    if run is not None:
        year = run.group(0)
        if len(year) != year_width:
            raise SymbolAssistError(
                f"the slave example {slave!r} has a year width that does not "
                "match the master's")
        return prefix, year, tail[len(year):]
    return prefix, "", tail


def _one(values: list[tuple[str, str, str]], what: str) -> tuple[str, str, str]:
    if not values:
        raise SymbolAssistError("at least one complete example is required")
    first = values[0]
    if any(value != first for value in values):
        raise SymbolAssistError(f"the examples describe different {what}")
    return first


def infer_symbol_rule(examples: list[tuple[str, str]]) -> SymbolRule:
    """Derive a master regex + slave template from example pairs."""
    pairs = [(master.strip(), slave.strip())
             for master, slave in examples
             if master.strip() and slave.strip()]
    if not pairs:
        raise SymbolAssistError("at least one complete example is required")

    masters = [_split_master(master) for master, _ in pairs]
    year_widths = {len(year) for _, _, year, _ in masters}
    if len(year_widths) != 1:
        raise SymbolAssistError("the examples use different year widths")
    year_width = next(iter(year_widths))
    prefix, _, master_suffix = _one(
        [(p, "", s) for p, _, _, s in masters], "master symbol shapes")

    slaves = [_split_slave(slave, year_width) for _, slave in pairs]
    slave_prefix, slave_year, slave_suffix = _one(slaves, "slave symbol shapes")

    pattern = ("^(" + re.escape(prefix) + ")([" + MONTH_CODES + "])(\\d{"
               + str(year_width) + "})" + re.escape(master_suffix) + "$")
    template = (_escape_template(slave_prefix) + "$2"
                + ("$3" if slave_year else "")
                + _escape_template(slave_suffix))
    if any(ch in pattern or ch in template for ch in ",="):
        raise SymbolAssistError("the generated rule cannot contain ',' or '='")
    rule = SymbolRule(pattern, template)
    validate_rule(rule, pairs)
    return rule


def validate_rule(rule: SymbolRule, examples: list[tuple[str, str]]) -> None:
    """Every example must resolve through the rule with no same-name fallback."""
    mapper = SymbolMapper(f"{rule.master_pattern}={rule.slave_template}",
                          lambda s: False)
    for master, slave in examples:
        got = mapper.resolve(master.strip())
        if got != slave.strip():
            raise SymbolAssistError(
                f"the rule maps {master!r} to {got!r}, expected {slave!r}")


def find_conflicts(rule: SymbolRule, examples: list[tuple[str, str]],
                   existing_rows: list[tuple[str, str]]
                   ) -> list[RuleConflict]:
    """Report examples where an existing row would win instead of the new rule.

    `exact_tier` marks a winner whose master text literally equals the
    example: that row always wins, regardless of row order.
    """
    complete = [(i, master.strip(), slave.strip())
                for i, (master, slave) in enumerate(existing_rows)
                if master.strip() and slave.strip()
                and "," not in master and "=" not in master
                and "," not in slave and "=" not in slave]
    if not complete:
        return []
    new_mapper = SymbolMapper(f"{rule.master_pattern}={rule.slave_template}",
                              lambda s: False)
    conflicts: list[RuleConflict] = []
    for master_example, _ in examples:
        master_example = master_example.strip()
        expected = new_mapper.resolve(master_example)
        for index, master, slave in complete:
            one = SymbolMapper(f"{master}={slave}", lambda s: False)
            actual = one.resolve(master_example)
            if not actual:
                continue
            if actual != expected:
                conflicts.append(RuleConflict(
                    master_example, expected, actual, index,
                    exact_tier=master_example == master))
            break
    return conflicts
