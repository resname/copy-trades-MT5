# manager/tests/test_ib_worker.py
import pytest

from manager.engine.models import BUY, SELL
from manager.ipc.messages import CommandMsg
from manager.worker.ib.adapter import FakeIbGateway
from manager.worker.ib.contracts import parse_contract_map
from manager.worker.ib.worker import (
    _contracts_for, _holdings, _symbol_info_for, execute_command,
)

CONTRACTS = parse_contract_map({"YM": {"exchange": "CME", "sec_type": "FUT",
                                        "master_point_value": 1.0}})


def make_gw(**kw):
    kw.setdefault("prices", {"YM": (45_000.0, 45_001.0)})
    kw.setdefault("contract_multipliers", {"YM": 5.0})
    kw.setdefault("tick_sizes", {"YM": 0.25})
    kw.setdefault("months", {"YM": ["202612"]})
    kw.setdefault("margin_per_contract", {"YM": 12_000.0})
    return FakeIbGateway(**kw)


def _cfg():
    """The StartMsg-config shape Task 2's `build_worker_configs` emits for
    an IB slave (the loop test passes it straight to `_slave_loop`)."""
    return {"slave_id": "ib1", "platform": "ib", "symbol_map_csv": "US30=YM",
            "normalize_sltp": True, "sizing_mode": "balance_step",
            "contract_map": {"YM": {"exchange": "CME", "sec_type": "FUT",
                                     "master_point_value": 1.0}},
            "ib_host": "127.0.0.1", "ib_port": 4002,
            "ib_client_id": 7, "roll_days": 5, "ack_timeout_ms": 15000,
            "slave_status_interval_ms": 200}


def _open_cmd(volume=0.28, sl=44_950.0, tp=45_100.0, **kw):
    from manager.engine.linkage import encode_comment, magic_for
    defaults = dict(slave_id="ib1", action="OPEN", master_ticket=123,
                    symbol="YM", volume=volume, sl=sl, tp=tp,
                    master_open_price=45_000.0, side=BUY,
                    magic=magic_for(123), master_open_volume=0.28,
                    comment=encode_comment(123, 0.28, volume))
    defaults.update(kw)
    return CommandMsg(**defaults)


def test_contracts_epsilon_floor():
    # 0.28 units at ratio 0.2 is 0.056 -> 0; 2.9999999999/1.0 -> 3
    assert _contracts_for(0.28, 0.2) == 0
    assert _contracts_for(2.9999999999, 1.0) == 3
    assert _contracts_for(0.29, 1.0) == 0        # epsilon-below-grid case


def test_ratio_balance_step_units_are_contracts():
    from manager.engine.transform import SIZING_BALANCE_STEP
    from manager.worker.ib.worker import _ratio
    assert _ratio(SIZING_BALANCE_STEP, CONTRACTS["YM"],
                  {"multiplier": 5.0}) == 1.0


def test_ratio_copy_master_uses_point_value_ratio():
    from manager.engine.transform import SIZING_COPY_MASTER
    from manager.worker.ib.worker import _ratio
    # master CFD $1/pt per lot, YM $5/pt per contract -> 1 lot = 0.2 contract
    assert _ratio(SIZING_COPY_MASTER, CONTRACTS["YM"],
                  {"multiplier": 5.0}) == pytest.approx(0.2)


def test_open_below_one_contract_is_a_logged_skip():
    gw = make_gw(); gw.initialize("h", 1, 1)
    ack = execute_command(gw, _open_cmd(volume=0.28), CONTRACTS,
                          normalize_sltp=True, sizing_mode="copy_master",
                          roll_days=5, timeout_ms=15_000)
    assert not ack.ok and ack.retcode == 0
    assert "0 contracts" in ack.error
    assert gw.tagged_orders() == [] and gw.net_positions() == []


def test_open_balance_step_units_are_contracts():
    gw = make_gw(); gw.initialize("h", 1, 1)
    cmd = _open_cmd(volume=2.0)     # balance_step: 2 units == 2 contracts
    ack = execute_command(gw, cmd, CONTRACTS, normalize_sltp=True,
                          sizing_mode="balance_step", roll_days=5,
                          timeout_ms=15_000)
    assert ack.ok
    assert gw.net_positions()[0].qty == 2.0
    assert ack.slave_ticket > 1_999_999_999      # synthetic ticket space


def test_open_normalizes_sl_onto_slave_fill():
    gw = make_gw(); gw.initialize("h", 1, 1)
    cmd = _open_cmd(volume=2.0)     # master open 45000, sl 44950 -> dist 50
    ack = execute_command(gw, cmd, CONTRACTS, normalize_sltp=True,
                          sizing_mode="balance_step", roll_days=5,
                          timeout_ms=15_000)
    stp = next(o for o in gw.tagged_orders() if o.order_type == "STP")
    assert stp.stop_price == pytest.approx(44_951.0)   # fill 45001 - 50


def test_open_margin_guard():
    gw = make_gw(account={"margin_available": 10_000.0})
    gw.initialize("h", 1, 1)
    ack = execute_command(gw, _open_cmd(volume=2.0), CONTRACTS,
                          normalize_sltp=True, sizing_mode="balance_step",
                          roll_days=5, timeout_ms=15_000)
    assert not ack.ok and "insufficient margin" in ack.error
    assert gw.net_positions() == []


def _holdings_setup(gw, volume=2.0, **execute_kw):
    gw.initialize("h", 1, 1)
    cmd = _open_cmd(volume=volume)
    ack = execute_command(gw, cmd, CONTRACTS, normalize_sltp=True,
                          sizing_mode="balance_step", roll_days=5,
                          timeout_ms=15_000, **execute_kw)
    return cmd, ack


def _partial_cmd(cmd, ack, new_master_volume, mv=0.28):
    # MODIFY/PARTIAL/CLOSE route by the OPEN ack's slave_ticket (the
    # synthetic ticket), exactly like an MT5 slave — not by tag lookup.
    return CommandMsg(slave_id="ib1", action="PARTIAL_CLOSE",
                      master_ticket=cmd.master_ticket,
                      slave_ticket=ack.slave_ticket,
                      new_master_volume=new_master_volume,
                      master_open_volume=mv,
                      slave_open_volume=cmd.volume)


def test_partial_resizes_children():
    gw = make_gw(); cmd, ack = _holdings_setup(gw, volume=4.0)
    ack2 = execute_command(gw, _partial_cmd(cmd, ack, 0.14), CONTRACTS,
                           normalize_sltp=True, sizing_mode="balance_step",
                           roll_days=5, timeout_ms=15_000)
    assert ack2.ok
    assert gw.net_positions()[0].qty == 2.0        # closed half: 4 -> 2
    live = [o for o in gw.tagged_orders()
            if o.order_type in ("STP", "LMT") and not o.filled]
    assert all(o.qty == 2.0 for o in live)         # REVIEW-FOCUS #2
    assert live and {o.qty for o in live} == {2.0}


def _close_cmd(cmd, ack):
    from manager.engine.linkage import magic_for
    return CommandMsg(slave_id="ib1", action="CLOSE",
                      master_ticket=cmd.master_ticket,
                      slave_ticket=ack.slave_ticket,
                      magic=magic_for(cmd.master_ticket))


def test_close_only_closes_own_share():
    # REVIEW-FOCUS #1: two records on the same contract+side net together;
    # CLOSE of one must leave the other's share standing.
    gw = make_gw(); gw.initialize("h", 1, 1)
    from manager.engine.linkage import encode_comment, magic_for
    c1 = CommandMsg(slave_id="ib1", action="OPEN", master_ticket=1,
                    symbol="YM", volume=2.0, sl=44_950.0, tp=45_100.0,
                    master_open_price=45_000.0, side=BUY, magic=magic_for(1),
                    master_open_volume=0.28, comment=encode_comment(1, 0.28, 2.0))
    c2 = CommandMsg(slave_id="ib1", action="OPEN", master_ticket=2,
                    symbol="YM", volume=2.0, sl=44_950.0, tp=45_100.0,
                    master_open_price=45_000.0, side=BUY, magic=magic_for(2),
                    master_open_volume=0.28, comment=encode_comment(2, 0.28, 2.0))
    ack1 = execute_command(gw, c1, CONTRACTS, True, "balance_step", 5, 15_000)
    execute_command(gw, c2, CONTRACTS, True, "balance_step", 5, 15_000)
    assert gw.net_positions()[0].qty == 4.0        # one net long of 4
    close1 = _close_cmd(c1, ack1)
    ack = execute_command(gw, close1, CONTRACTS, True, "balance_step", 5, 15_000)
    assert ack.ok
    assert gw.net_positions()[0].qty == 2.0        # record 2's share survives
    # and the closed record's children are gone (the net did NOT go flat, so
    # the fake's own close-cancel didn't run — the worker's post-close
    # protection sync retires record 1's stale stop/limit)
    assert not any(o.tag == c1.comment and not o.filled
                   and o.order_type in ("STP", "LMT")
                   for o in gw.tagged_orders())
    # ...while record 2's children stay live, sized to its surviving share
    assert all(o.qty == 2.0 for o in gw.tagged_orders()
               if o.tag == c2.comment and not o.filled
               and o.order_type in ("STP", "LMT"))


def test_command_timeout_rejects_not_hangs():
    # REVIEW-FOCUS #4: a gateway that never fills (fail_reduce_timeout models
    # a hung Gateway restart) surfaces as a failed ack with "timeout" in the
    # error — never a hung pipe loop; the position is untouched.
    gw = make_gw(fail_reduce_timeout=True)
    cmd, ack = _holdings_setup(gw, volume=2.0)
    assert ack.ok
    ack2 = execute_command(gw, _partial_cmd(cmd, ack, 0.14), CONTRACTS, True,
                           "balance_step", 5, 15_000)
    assert not ack2.ok and "timeout" in ack2.error
    assert gw.net_positions()[0].qty == 2.0        # nothing reduced


def _modify_cmd(cmd, ack, sl, tp, master_ticket=None, master_open_price=45_000.0):
    # MODIFY/PARTIAL/CLOSE route by the OPEN ack's slave_ticket (the
    # synthetic ticket); SL/TP ride raw and _prep_sltp normalizes/rounds.
    from manager.engine.linkage import magic_for
    return CommandMsg(slave_id="ib1", action="MODIFY",
                      master_ticket=cmd.master_ticket
                      if master_ticket is None else master_ticket,
                      slave_ticket=ack.slave_ticket,
                      sl=sl, tp=tp, master_open_price=master_open_price,
                      side=BUY, magic=magic_for(cmd.master_ticket))


def test_modify_updates_tag_children_prices_keeps_quantities():
    gw = make_gw(); cmd, ack = _holdings_setup(gw, volume=4.0)
    assert ack.ok
    ack2 = execute_command(gw, _modify_cmd(cmd, ack, 44_500.0, 45_500.0),
                           CONTRACTS, normalize_sltp=False,
                           sizing_mode="balance_step", roll_days=5,
                           timeout_ms=15_000)
    assert ack2.ok and ack2.retcode == 0 and ack2.action == "MODIFY"
    live = [o for o in gw.tagged_orders()
            if o.tag == cmd.comment and not o.filled
            and o.order_type in ("STP", "LMT")]
    by_type = {o.order_type: o for o in live}
    assert set(by_type) == {"STP", "LMT"}   # one live child each: the old
    # pair was cancelled and replaced, not patched in place
    assert by_type["STP"].stop_price == pytest.approx(44_500.0)
    assert by_type["LMT"].limit_price == pytest.approx(45_500.0)
    assert {o.qty for o in live} == {4.0}   # price-only MODIFY: children keep
    # their share; resizing is PARTIAL_CLOSE's job
    assert gw.net_positions()[0].qty == 4.0


def test_modify_does_not_swap_sl_and_tp_children():
    # SL=44950 must land on the STP child, TP=45100 on the LMT child —
    # a swapped pair would sell-limit the stop away and stop past the target.
    gw = make_gw(); cmd, ack = _holdings_setup(gw, volume=2.0)
    ack2 = execute_command(gw, _modify_cmd(cmd, ack, 44_950.0, 45_100.0),
                           CONTRACTS, normalize_sltp=False,
                           sizing_mode="balance_step", roll_days=5,
                           timeout_ms=15_000)
    assert ack2.ok
    stp = next(o for o in gw.tagged_orders()
               if o.tag == cmd.comment and o.order_type == "STP"
               and not o.filled)
    lmt = next(o for o in gw.tagged_orders()
               if o.tag == cmd.comment and o.order_type == "LMT"
               and not o.filled)
    assert stp.stop_price == pytest.approx(44_950.0)
    assert lmt.limit_price == pytest.approx(45_100.0)
    assert stp.limit_price == 0.0 and lmt.stop_price == 0.0


def test_modify_without_a_position_fails_and_places_nothing():
    gw = make_gw(); gw.initialize("h", 1, 1)
    bad = CommandMsg(slave_id="ib1", action="MODIFY", master_ticket=123,
                     slave_ticket=999_999, sl=44_950.0, tp=45_100.0,
                     master_open_price=45_000.0, side=BUY)
    ack = execute_command(gw, bad, CONTRACTS, normalize_sltp=False,
                          sizing_mode="balance_step", roll_days=5,
                          timeout_ms=15_000)
    assert not ack.ok and ack.retcode == -1
    assert "not found" in ack.error
    assert gw.tagged_orders() == [] and gw.net_positions() == []


def test_modify_unknown_master_ticket_leaves_children_untouched():
    gw = make_gw(); cmd, ack = _holdings_setup(gw, volume=4.0)
    before = [(o.order_type, o.stop_price, o.limit_price, o.qty)
              for o in gw.tagged_orders()
              if o.tag == cmd.comment and not o.filled]
    ack2 = execute_command(gw, _modify_cmd(cmd, ack, 44_500.0, 45_500.0,
                                           master_ticket=777), CONTRACTS,
                           normalize_sltp=False, sizing_mode="balance_step",
                           roll_days=5, timeout_ms=15_000)
    assert not ack2.ok and ack2.retcode == -1
    assert "tag not found for master #777" in ack2.error
    after = [(o.order_type, o.stop_price, o.limit_price, o.qty)
             for o in gw.tagged_orders()
             if o.tag == cmd.comment and not o.filled]
    assert after == before          # failed lookup: no orders placed/cancelled


def test_reduce_order_is_not_in_the_oca_group():
    # U3: CLOSE/PARTIAL reduce orders are bare MKT sells — only the SL/TP
    # bracket children share an OCA group, so a reduce fill can never
    # cancel a survivor's bracket.
    from manager.worker.ib.adapter import OCA_PREFIX
    gw = make_gw(); cmd, ack = _holdings_setup(gw, volume=4.0)
    ack2 = execute_command(gw, _partial_cmd(cmd, ack, 0.14), CONTRACTS,
                           normalize_sltp=True, sizing_mode="balance_step",
                           roll_days=5, timeout_ms=15_000)
    assert ack2.ok
    mkts = [o for o in gw.tagged_orders() if o.order_type == "MKT"]
    reduce_o = mkts[-1]
    assert (reduce_o.action, reduce_o.qty) == ("SELL", 2.0)  # the reduce fill
    assert reduce_o.oca_group == ""
    live_oca = {o.oca_group for o in gw.tagged_orders()
                if o.order_type in ("STP", "LMT") and not o.filled}
    assert live_oca and all(g.startswith(OCA_PREFIX) for g in live_oca)
    assert reduce_o.oca_group not in live_oca


class _SpyGw(FakeIbGateway):
    """Records which contract month each command routed to (the fake books
    one contract per symbol, but the month argument is what Task 6's real
    gateway turns into the IB contract — Review Focus #5)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.open_months: list[str] = []
        self.protect_months: list[str] = []
        self.reduce_months: list[str] = []

    def open_bracket(self, symbol, side, qty, sl, tp, tag,
                     month="", timeout_s=15.0):
        self.open_months.append(month)
        return super().open_bracket(symbol, side, qty, sl, tp, tag,
                                    month=month, timeout_s=timeout_s)

    def set_protective(self, symbol, side, tag, sl, tp, qty,
                       month="", timeout_s=15.0):
        self.protect_months.append(month)
        return super().set_protective(symbol, side, tag, sl, tp, qty,
                                      month=month, timeout_s=timeout_s)

    def reduce(self, symbol, side, qty, tag, close,
               month="", timeout_s=15.0):
        self.reduce_months.append(month)
        return super().reduce(symbol, side, qty, tag, close,
                              month=month, timeout_s=timeout_s)


def _spy_gw(months, **kw):
    return _SpyGw(prices={"YM": (45_000.0, 45_001.0)},
                  contract_multipliers={"YM": 5.0},
                  tick_sizes={"YM": 0.25},
                  months={"YM": months},
                  open_interest={"YM": {m: 5000.0 - 100.0 * i  # earlier month
                                        for i, m in enumerate(months)}},
                  margin_per_contract={"YM": 12_000.0}, **kw)


def test_open_uses_rolled_contract():
    # REVIEW-FOCUS #5: inside the roll window the OPEN resolves to the NEXT
    # month and the route to the gateway carries it.
    gw = _spy_gw(["202612", "202703"])
    gw.initialize("h", 1, 1)
    ack = execute_command(gw, _open_cmd(volume=2.0), CONTRACTS, True,
                          "balance_step", 5, 15_000, today="20261226")
    assert ack.ok
    assert gw.open_months == ["202703"]


def test_close_targets_record_month_not_rolled_front():
    # The record opened before the roll window must keep its own contract:
    # a CLOSE evaluated inside the roll window still routes month "202612".
    gw = _spy_gw(["202612", "202703"])
    gw.initialize("h", 1, 1)
    cmd = _open_cmd(volume=2.0)
    ack = execute_command(gw, cmd, CONTRACTS, True, "balance_step", 5,
                          15_000, today="20261002")
    assert ack.ok and gw.open_months == ["202612"]
    close = _close_cmd(cmd, ack)
    ack2 = execute_command(gw, close, CONTRACTS, True, "balance_step", 5,
                           15_000, today="20261226")    # roll window today
    assert ack2.ok
    assert gw.reduce_months == ["202612"]


def test_modify_targets_recorded_month_not_rolled_front():
    # _target_month on MODIFY prefers the record's parent MKT month: an
    # existing position keeps its contract even when today sits in the roll
    # window (a fresh resolution would already point at 202703).
    gw = _spy_gw(["202612", "202703"])
    gw.initialize("h", 1, 1)
    cmd = _open_cmd(volume=2.0)
    ack = execute_command(gw, cmd, CONTRACTS, True, "balance_step", 5,
                          15_000, today="20261002")
    assert ack.ok and gw.open_months == ["202612"]
    # sanity: the MODIFY's "today" IS inside the roll window — resolution
    # alone would roll ahead — so the assertion below is really on _target_month
    res = gw.resolve_contract("YM", "20261226", exchange="CME",
                              sec_type="FUT", roll_days=5)
    assert res["month"] == "202703" and res["rolling"]
    ack2 = execute_command(gw, _modify_cmd(cmd, ack, 44_950.0, 45_100.0),
                           CONTRACTS, True, "balance_step", 5, 15_000,
                           today="20261226")
    assert ack2.ok
    assert gw.protect_months == ["202612"]  # record month, not the rolled 202703


def test_recovery_builds_records_from_tags():
    from manager.worker.ib.worker import build_recovery_records
    from manager.engine.linkage import magic_for
    gw = make_gw(); cmd, _ = _holdings_setup(gw, volume=4.0)
    records = build_recovery_records(gw)
    assert len(records) == 1
    rec = records[0]
    assert rec.master_ticket == 123
    assert rec.slave_open_volume == 4.0
    assert rec.slave_ticket > 1_999_999_999   # resolved from the parent MKT order
    assert rec.magic == magic_for(123)


def test_symbol_info_reports_virtual_volume_units():
    from manager.engine.models import SymbolInfo
    specs = CONTRACTS
    gw = make_gw(); gw.initialize("h", 1, 1)
    info = _symbol_info_for(gw, specs["YM"], today="20261002", roll_days=5)
    assert info is not None
    assert info.volume_step == 0.01 and info.volume_min == 0.01
    assert info.tick_size == 0.25


def test_status_detail_marks_rolling_front():
    # U13: the _status detail literal is "<sym> <month>[ rolling]" — the
    # " rolling" suffix is what the GUI's slave detail column shows.
    from manager.worker.ib.worker import _status
    gw = _spy_gw(["202612", "202703"])
    gw.initialize("h", 1, 1)
    spec = CONTRACTS["YM"]
    rolling = gw.resolve_contract("YM", "20261226", exchange=spec.exchange,
                                  sec_type=spec.sec_type, roll_days=5)
    assert rolling["rolling"] is True
    msg = _status(gw, "ib1", connected=True, front={"YM": rolling})
    assert msg.detail == "YM 202703 rolling"
    settled = gw.resolve_contract("YM", "20261002", exchange=spec.exchange,
                                  sec_type=spec.sec_type, roll_days=5)
    msg2 = _status(gw, "ib1", connected=True, front={"YM": settled})
    assert msg2.detail == "YM 202612"        # no suffix off the roll window


def test_slave_loop_symbol_info_request_replies_over_pipe():
    """Loop behavior end to end: init messages arrive (Recovery, SymbolInfo,
    Status — same order as the MT5 worker), and a SymbolInfoRequestMsg gets a
    SymbolInfoMsg naming every requested symbol."""
    import multiprocessing
    import threading
    from manager.ipc.messages import (RecoveryMsg, SymbolInfoMsg, StatusMsg,
                                      SymbolInfoRequestMsg)
    from manager.ipc.pipe_framing import send_msg, recv_msg
    from manager.worker.ib.worker import _slave_loop

    gw = make_gw(); gw.initialize("h", 1, 1)
    cfg = _cfg()

    parent, child = multiprocessing.Pipe(duplex=True)
    t = threading.Thread(target=_slave_loop,
                         args=(child, gw, cfg), daemon=True)
    t.start()
    try:
        init = [recv_msg(parent) for _ in range(3)]
        assert isinstance(init[0], RecoveryMsg)
        assert isinstance(init[1], SymbolInfoMsg)
        assert isinstance(init[2], StatusMsg)

        send_msg(parent, SymbolInfoRequestMsg(source_id="ib1",
                                              symbols=["YM", "NQ"]))
        replies = []
        while len(replies) < 1:
            msg = recv_msg(parent)
            if isinstance(msg, SymbolInfoMsg):
                replies.append(msg)
        si = replies[0]
        assert set(si.infos).issuperset({"YM"})     # NQ unknown: named-only
        assert si.requested == ["YM", "NQ"]
    finally:
        parent.close()  # -> worker reads EOFError -> graceful return
        t.join(timeout=2.0)
        assert not t.is_alive(), "slave loop must exit when the pipe closes"


# ---- final review round: F1 stop validation -------------------------------

def _orders_snapshot(gw, tag=None):
    return [(o.order_type, o.stop_price, o.limit_price, o.qty, o.filled,
             o.active) for o in gw.tagged_orders()
            if tag is None or o.tag == tag]


def test_open_rejects_master_stop_past_master_open():
    # F1, OPEN path: a master SL already at-or-past the master's open
    # (breakeven/trailed) anchors to a child at-or-past the slave market —
    # failed ack before any placement.
    gw = make_gw(); gw.initialize("h", 1, 1)
    ack = execute_command(gw, _open_cmd(volume=2.0, sl=45_050.0), CONTRACTS,
                          normalize_sltp=True, sizing_mode="balance_step",
                          roll_days=5, timeout_ms=15_000)
    assert not ack.ok and ack.retcode == -1
    assert "nonsensical stop" in ack.error
    assert "45050" in ack.error                     # the anchor is named
    assert gw.tagged_orders() == [] and gw.net_positions() == []


def test_modify_rejects_trailed_breakeven_master_stop():
    # F1 probe scenario: master long opened 44000, SL trailed to 44800.
    # copy_loop sends the master's CURRENT sl with the ORIGINAL open price, so
    # the anchored slave SL is 45001 + (44800 - 44000) = 45801 — past the bid
    # 45000: on the fake it fires instantly (closing the in-profit position);
    # on IB it is rejected or instantly filled, stripping the protection.
    # Must reject with ok=False and touch nothing.
    gw = make_gw(); cmd, ack = _holdings_setup(gw, volume=2.0)
    before = _orders_snapshot(gw, tag=cmd.comment)
    ack2 = execute_command(
        gw, _modify_cmd(cmd, ack, 44_800.0, 45_100.0,
                        master_open_price=44_000.0), CONTRACTS,
        normalize_sltp=True, sizing_mode="balance_step", roll_days=5,
        timeout_ms=15_000)
    assert not ack2.ok and ack2.retcode == -1
    assert "nonsensical stop" in ack2.error
    assert "44800" in ack2.error and "44000" in ack2.error
    assert _orders_snapshot(gw, tag=cmd.comment) == before   # no cancel/replace
    assert gw.net_positions()[0].qty == 2.0                  # left as-is


def test_modify_rejects_raw_stop_at_market_without_normalize():
    # F1, normalize-OFF: a raw stop at the market fires as placed
    # (SELL STP: bid <= stop) — rejected, children untouched.
    gw = make_gw(); cmd, ack = _holdings_setup(gw, volume=2.0)
    before = _orders_snapshot(gw, tag=cmd.comment)
    ack2 = execute_command(gw, _modify_cmd(cmd, ack, 45_000.0, 45_100.0),
                           CONTRACTS, normalize_sltp=False,
                           sizing_mode="balance_step", roll_days=5,
                           timeout_ms=15_000)
    assert not ack2.ok and "nonsensical stop" in ack2.error
    assert _orders_snapshot(gw, tag=cmd.comment) == before
    assert gw.net_positions()[0].qty == 2.0


def test_short_side_stop_guard_uses_the_ask():
    # F1: for a short the protective children are BUY orders — the guard
    # compares against the ask, not the bid.
    gw = make_gw(); gw.initialize("h", 1, 1)
    bad = _open_cmd(volume=2.0, side=SELL, sl=44_950.0, tp=44_900.0)
    ack = execute_command(gw, bad, CONTRACTS, normalize_sltp=True,
                          sizing_mode="balance_step", roll_days=5,
                          timeout_ms=15_000)
    assert not ack.ok and "nonsensical stop" in ack.error
    assert gw.net_positions() == []
    good = _open_cmd(volume=2.0, side=SELL, sl=45_050.0, tp=44_900.0)
    ack2 = execute_command(gw, good, CONTRACTS, normalize_sltp=True,
                           sizing_mode="balance_step", roll_days=5,
                           timeout_ms=15_000)
    assert ack2.ok


# ---- F3: early-retire paths run the protection sync ------------------------

def test_close_flat_race_retires_stale_children():
    # F3: a CLOSE reduce timed out and then filled late (flattening the net);
    # the record's children are still full-size. The next CLOSE rides the
    # already-flat tolerance path (ok=True) but must retire the orphans.
    gw = make_gw(fail_reduce_timeout=True); cmd, ack = _holdings_setup(
        gw, volume=2.0)
    assert ack.ok
    ack2 = execute_command(gw, _close_cmd(cmd, ack), CONTRACTS, True,
                           "balance_step", 5, 15_000)
    assert not ack2.ok and "timeout" in ack2.error
    gw._fail_reduce_timeout = False
    gw.reduce("YM", BUY, 2, cmd.comment, close=False)      # the late fill
    assert gw.net_positions() == []                        # flat, orphans live
    ack3 = execute_command(gw, _close_cmd(cmd, ack), CONTRACTS, True,
                           "balance_step", 5, 15_000)
    assert ack3.ok
    live = [o for o in gw.tagged_orders()
            if o.tag == cmd.comment and not o.filled
            and o.order_type in ("STP", "LMT")]
    assert live == [], "orphaned full-size children must be retired"


def test_close_zero_holdings_runs_protection_sync():
    # F3 (by <= 0 path): a timed-out reduce filled late and closed this
    # record's whole share while the net still holds other records' shares —
    # the early retire (ok=True, by<=0) must cancel this record's children.
    gw = make_gw(); gw.initialize("h", 1, 1)
    from manager.engine.linkage import encode_comment, magic_for
    c1 = CommandMsg(slave_id="ib1", action="OPEN", master_ticket=1,
                    symbol="YM", volume=2.0, sl=44_950.0, tp=45_100.0,
                    master_open_price=45_000.0, side=BUY, magic=magic_for(1),
                    master_open_volume=0.28,
                    comment=encode_comment(1, 0.28, 2.0))
    c2 = CommandMsg(slave_id="ib1", action="OPEN", master_ticket=2,
                    symbol="YM", volume=2.0, sl=44_950.0, tp=45_100.0,
                    master_open_price=45_000.0, side=BUY, magic=magic_for(2),
                    master_open_volume=0.28,
                    comment=encode_comment(2, 0.28, 2.0))
    ack1 = execute_command(gw, c1, CONTRACTS, True, "balance_step", 5, 15_000)
    execute_command(gw, c2, CONTRACTS, True, "balance_step", 5, 15_000)
    gw.reduce("YM", BUY, 2, c1.comment, close=False)  # late fill: A's share
    ack = execute_command(gw, _close_cmd(c1, ack1), CONTRACTS, True,
                          "balance_step", 5, 15_000)
    assert ack.ok and ack.remaining_volume == 0.0
    assert gw.net_positions()[0].qty == 2.0           # B's share survives
    assert not any(o.tag == c1.comment and not o.filled
                   and o.order_type in ("STP", "LMT")
                   for o in gw.tagged_orders())       # A's orphans retired
    live2 = [o for o in gw.tagged_orders()
             if o.tag == c2.comment and not o.filled
             and o.order_type in ("STP", "LMT")]
    assert live2 and all(o.qty == 2.0 for o in live2)


def test_partial_noop_reruns_protection_sync():
    # F3: a PARTIAL whose reduce timed out filled late; the repeat PARTIAL
    # computes by <= 0 — that no-op path must still re-sync the children
    # (they sit at the pre-reduce size otherwise).
    gw = make_gw(fail_reduce_timeout=True); cmd, ack = _holdings_setup(
        gw, volume=2.0)
    ack2 = execute_command(gw, _partial_cmd(cmd, ack, 0.14), CONTRACTS, True,
                           "balance_step", 5, 15_000)
    assert not ack2.ok
    gw._fail_reduce_timeout = False
    gw.reduce("YM", BUY, 1, cmd.comment, close=False)    # the late 1-lot fill
    ack3 = execute_command(gw, _partial_cmd(cmd, ack, 0.14), CONTRACTS, True,
                           "balance_step", 5, 15_000)
    assert ack3.ok and ack3.remaining_volume == 1.0
    live = [o for o in gw.tagged_orders()
            if o.tag == cmd.comment and not o.filled
            and o.order_type in ("STP", "LMT")]
    assert live and {o.qty for o in live} == {1.0}


# ---- F4: recovery anchoring + clamped holdings ------------------------------

def test_recovery_skips_cancelled_parent_orders():
    # F4: a parent MKT that was Cancelled (rejected / margin-refused) never
    # opened anything — recovery must not resurrect a phantom record.
    from manager.engine.linkage import encode_comment
    from manager.worker.ib.adapter import IbOrder
    from manager.worker.ib.worker import build_recovery_records
    gw = make_gw(); gw.initialize("h", 1, 1)
    gw.orders.append(IbOrder(order_id=9001, tag=encode_comment(123, 0.28, 4.0),
                             symbol="YM", action="BUY", order_type="MKT",
                             qty=4.0, month="202612", filled=True))
    gw.orders.append(IbOrder(order_id=9002, tag=encode_comment(999, 0.28, 2.0),
                             symbol="YM", action="BUY", order_type="MKT",
                             qty=2.0, month="202612", status="Cancelled"))
    records = build_recovery_records(gw)
    assert [r.master_ticket for r in records] == [123]


def test_holdings_cannot_sweep_another_record_after_history_loss():
    # F4: the Gateway restart rolls reqExecutions' day scope — the close
    # history vanishes and the record's derived share re-inflates; CLOSE must
    # clamp to the record's slice of the live net (a share no sweep of
    # record 2's contracts), never reduce record 2's holdings.
    from manager.worker.ib.worker import _holdings
    gw = make_gw(); gw.initialize("h", 1, 1)
    from manager.engine.linkage import encode_comment, magic_for
    c1 = CommandMsg(slave_id="ib1", action="OPEN", master_ticket=1,
                    symbol="YM", volume=2.0, sl=44_950.0, tp=45_100.0,
                    master_open_price=45_000.0, side=BUY, magic=magic_for(1),
                    master_open_volume=0.28,
                    comment=encode_comment(1, 0.28, 2.0))
    c2 = CommandMsg(slave_id="ib1", action="OPEN", master_ticket=2,
                    symbol="YM", volume=2.0, sl=44_950.0, tp=45_100.0,
                    master_open_price=45_000.0, side=BUY, magic=magic_for(2),
                    master_open_volume=0.28,
                    comment=encode_comment(2, 0.28, 2.0))
    ack1 = execute_command(gw, c1, CONTRACTS, True, "balance_step", 5, 15_000)
    execute_command(gw, c2, CONTRACTS, True, "balance_step", 5, 15_000)
    part1 = CommandMsg(slave_id="ib1", action="PARTIAL_CLOSE",
                       master_ticket=1, slave_ticket=ack1.slave_ticket,
                       new_master_volume=0.21, master_open_volume=0.28,
                       slave_open_volume=2.0)
    ackp = execute_command(gw, part1, CONTRACTS, True, "balance_step", 5, 15_000)
    assert ackp.ok and gw.net_positions()[0].qty == 3.0
    gw.executions.clear()                  # the session roll wiped the day
    # clamped: net 3 minus record 2's 2 -> record 1 cannot claim 2 again
    assert _holdings(gw, c1.comment, 1.0) == 1
    close1 = _close_cmd(c1, ack1)
    ack = execute_command(gw, close1, CONTRACTS, True, "balance_step", 5, 15_000)
    assert ack.ok
    assert gw.net_positions()[0].qty == 2.0        # record 2's share survives
    live2 = [o for o in gw.tagged_orders()
             if o.tag == c2.comment and not o.filled
             and o.order_type in ("STP", "LMT")]
    assert live2 and all(o.qty == 2.0 for o in live2)


def test_holdings_clamp_is_noop_when_books_are_consistent():
    # F4 (guard on the guard): with a complete close history the clamp must
    # not shrink anything — sum(share - closed) == net on consistent books.
    from manager.worker.ib.worker import _holdings
    gw = make_gw(); gw.initialize("h", 1, 1)
    from manager.engine.linkage import encode_comment, magic_for
    c1 = CommandMsg(slave_id="ib1", action="OPEN", master_ticket=1,
                    symbol="YM", volume=2.0, sl=44_950.0, tp=45_100.0,
                    master_open_price=45_000.0, side=BUY, magic=magic_for(1),
                    master_open_volume=0.28,
                    comment=encode_comment(1, 0.28, 2.0))
    c2 = CommandMsg(slave_id="ib1", action="OPEN", master_ticket=2,
                    symbol="YM", volume=2.0, sl=44_950.0, tp=45_100.0,
                    master_open_price=45_000.0, side=BUY, magic=magic_for(2),
                    master_open_volume=0.28,
                    comment=encode_comment(2, 0.28, 2.0))
    ack1 = execute_command(gw, c1, CONTRACTS, True, "balance_step", 5, 15_000)
    execute_command(gw, c2, CONTRACTS, True, "balance_step", 5, 15_000)
    assert _holdings(gw, c1.comment, 1.0) == 2       # untouched by the clamp
    part1 = CommandMsg(slave_id="ib1", action="PARTIAL_CLOSE",
                       master_ticket=1, slave_ticket=ack1.slave_ticket,
                       new_master_volume=0.21, master_open_volume=0.28,
                       slave_open_volume=2.0)
    assert execute_command(gw, part1, CONTRACTS, True, "balance_step", 5,
                           15_000).ok
    # A holds 1 of the net-3: the clamp must still report exactly 1
    assert _holdings(gw, c1.comment, 1.0) == 1
    assert _holdings(gw, c2.comment, 1.0) == 2


# ---- F6: unprotected recovered positions ------------------------------------

def _seed_unprotected(gw, ticket=123, qty=4.0, **kw):
    """A recovered record's world: the parent MKT filled, the net is live —
    and no children (the worker died between fill and bracket placement)."""
    from manager.engine.linkage import encode_comment
    from manager.worker.ib.adapter import IbOrder
    tag = encode_comment(ticket, 0.28, qty)
    gw.orders.append(IbOrder(order_id=9001, tag=tag, symbol="YM", action="BUY",
                             order_type="MKT", qty=qty, month="202612",
                             filled=True))
    gw.positions["YM"] = float(qty)
    gw.avg_price["YM"] = 45_001.0
    gw.pos_month["YM"] = "202612"
    return tag


def test_status_flags_unprotected_recovered_position():
    from manager.engine.models import BUY
    from manager.worker.ib.worker import _status
    gw = make_gw(); gw.initialize("h", 1, 1)
    tag = _seed_unprotected(gw)
    front = {"YM": {"month": "202612", "rolling": False, "multiplier": 5.0,
                    "tick_size": 0.25, "margin_est": 0.0}}
    st = _status(gw, "ib1", connected=True, front=front)
    assert st.detail == "YM 202612 UNPROTECTED"
    # re-arming (the master's next MODIFY paths place children) clears it
    gw.set_protective("YM", BUY, tag, 44_951.0, 45_100.0, 4.0, month="202612")
    assert _status(gw, "ib1", connected=True, front=front).detail == "YM 202612"


def test_slave_loop_surfaces_unprotected_recovered_position():
    import multiprocessing
    import threading
    from manager.engine.models import BUY
    from manager.ipc.messages import ErrorMsg, RecoveryMsg, StatusMsg
    from manager.ipc.pipe_framing import recv_msg
    from manager.worker.ib.worker import _slave_loop

    gw = make_gw(); gw.initialize("h", 1, 1)
    _seed_unprotected(gw)
    parent, child = multiprocessing.Pipe(duplex=True)
    t = threading.Thread(target=_slave_loop, args=(child, gw, _cfg()),
                         daemon=True)
    t.start()
    try:
        msgs = [recv_msg(parent) for _ in range(4)]
        assert isinstance(msgs[0], RecoveryMsg) and len(msgs[0].records) == 1
        assert isinstance(msgs[2], StatusMsg)
        assert "UNPROTECTED" in msgs[2].detail
        assert isinstance(msgs[3], ErrorMsg) and not msgs[3].fatal
        assert "without SL/TP protection" in msgs[3].message
        assert "YM" in msgs[3].message
    finally:
        parent.close()
        t.join(timeout=2.0)
        assert not t.is_alive()


# ---- F7: the reduce sync routes through _target_month -----------------------

def test_sync_after_close_keeps_survivor_month_when_parent_archived():
    # F7: with the parent MKT archived (post-restart trades() loss) inside
    # the roll window, _sync_protection_after_reduce must fall back to the
    # position's month (202612) — NOT "" (the real gateway would place the
    # replacement children on the freshly resolved 202703 front).
    gw = _spy_gw(["202612", "202703"]); gw.initialize("h", 1, 1)
    from manager.engine.linkage import encode_comment, magic_for
    c1 = CommandMsg(slave_id="ib1", action="OPEN", master_ticket=1,
                    symbol="YM", volume=2.0, sl=44_950.0, tp=45_100.0,
                    master_open_price=45_000.0, side=BUY, magic=magic_for(1),
                    master_open_volume=0.28,
                    comment=encode_comment(1, 0.28, 2.0))
    c2 = CommandMsg(slave_id="ib1", action="OPEN", master_ticket=2,
                    symbol="YM", volume=2.0, sl=44_950.0, tp=45_100.0,
                    master_open_price=45_000.0, side=BUY, magic=magic_for(2),
                    master_open_volume=0.28,
                    comment=encode_comment(2, 0.28, 2.0))
    ack1 = execute_command(gw, c1, CONTRACTS, True, "balance_step", 5, 15_000,
                           today="20261002")
    execute_command(gw, c2, CONTRACTS, True, "balance_step", 5, 15_000,
                    today="20261002")
    gw.orders = [o for o in gw.orders if o.order_type != "MKT"]  # archive
    ack = execute_command(gw, _close_cmd(c1, ack1), CONTRACTS, True,
                          "balance_step", 5, 15_000, today="20261226")
    assert ack.ok
    # the reduce routed the survivor's month, and BOTH sync calls (cancel +
    # re-size) carried it too
    assert gw.reduce_months == ["202612"]
    assert gw.protect_months == ["202612", "202612"]
    assert gw.net_positions()[0].qty == 2.0


# ---- F2: reconnect after a Gateway drop -------------------------------------

class _ReconnectGw(FakeIbGateway):
    """Fake that models a Gateway whose socket drops/connects on demand
    (the nightly IBC restart): initialize succeeds `_fail_after` times."""

    def __init__(self, fail_after=None, **kw):
        super().__init__(**kw)
        self.initialize_calls: list[tuple] = []
        self._fail_after = fail_after
        self._err = ""

    def initialize(self, host, port, client_id):
        self.initialize_calls.append((host, port, client_id))
        if self._fail_after is not None \
                and len(self.initialize_calls) > self._fail_after:
            self.connected = False
            self._err = "connect failed (test)"
            return False
        self._err = ""
        return super().initialize(host, port, client_id)

    def last_error(self):
        return self._err


def test_slave_loop_reconnects_after_gateway_drop():
    import time
    import multiprocessing
    import threading
    from manager.ipc.messages import ErrorMsg, StatusMsg
    from manager.ipc.pipe_framing import recv_msg
    from manager.worker.ib.worker import _slave_loop

    gw = _ReconnectGw(); gw.initialize("h", 1, 1)
    parent, child = multiprocessing.Pipe(duplex=True)
    t = threading.Thread(target=_slave_loop, args=(child, gw, _cfg()),
                         daemon=True)
    t.start()
    try:
        for _ in range(3):
            recv_msg(parent)                       # init: Recovery/SI/Status
        gw.connected = False                       # the Gateway drops
        end = time.time() + 5.0
        while len(gw.initialize_calls) < 2 and time.time() < end:
            time.sleep(0.05)
        assert gw.initialize_calls[1] == ("127.0.0.1", 4002, 7)  # own config
        st = None
        dead: list = []
        end = time.time() + 5.0
        while time.time() < end and st is None:
            if parent.poll(0.05):
                m = recv_msg(parent)
                if isinstance(m, StatusMsg):
                    st = m
                elif isinstance(m, ErrorMsg):
                    dead.append(m)
        assert st is not None and st.connected is True
        assert all(not m.fatal for m in dead)      # reconnect is never fatal
    finally:
        parent.close()
        t.join(timeout=2.0)
        assert not t.is_alive()


def test_slave_loop_counts_reconnect_failures_bounded_errors():
    import time
    import multiprocessing
    import threading
    from manager.ipc.messages import ErrorMsg, StatusMsg
    from manager.ipc.pipe_framing import recv_msg
    from manager.worker.ib.worker import _slave_loop

    gw = _ReconnectGw(fail_after=1)             # only the manual init succeeds
    gw.initialize("h", 1, 1)
    parent, child = multiprocessing.Pipe(duplex=True)
    t = threading.Thread(target=_slave_loop, args=(child, gw, _cfg()),
                         daemon=True)
    t.start()
    try:
        for _ in range(3):
            recv_msg(parent)
        gw.connected = False                    # the Gateway drops
        end = time.time() + 5.0
        while len(gw.initialize_calls) < 3 and time.time() < end:
            time.sleep(0.05)                   # >=2 failed reconnect attempts
        attempts_before_restore = len(gw.initialize_calls) - 1  # minus manual
        assert attempts_before_restore >= 2
        drained = []
        end = time.time() + 1.0
        while time.time() < end:
            if parent.poll(0.05):
                drained.append(recv_msg(parent))
        errors = [m for m in drained if isinstance(m, ErrorMsg)]
        assert all(not m.fatal for m in errors)
        # bounded surfacing: one "lost" message for the whole outage, no
        # per-attempt spam, despite the counted attempts
        assert len(errors) == 1 and "connection lost" in errors[0].message
        statuses = [m for m in drained if isinstance(m, StatusMsg)]
        assert statuses and statuses[-1].connected is False
        # the loop stays alive while the Gateway is down (no crash, no hang)
        assert t.is_alive()
        # and the restore is surfaced the same way (once), naming the count
        gw._fail_after = None
        restored = None
        end = time.time() + 5.0
        while time.time() < end and restored is None:
            if parent.poll(0.05):
                m = recv_msg(parent)
                if isinstance(m, ErrorMsg) and "reconnected" in m.message:
                    restored = m
        assert restored is not None and not restored.fatal
    finally:
        parent.close()
        t.join(timeout=2.0)
        assert not t.is_alive()