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


def _modify_cmd(cmd, ack, sl, tp, master_ticket=None):
    # MODIFY/PARTIAL/CLOSE route by the OPEN ack's slave_ticket (the
    # synthetic ticket); SL/TP ride raw and _prep_sltp normalizes/rounds.
    from manager.engine.linkage import magic_for
    return CommandMsg(slave_id="ib1", action="MODIFY",
                      master_ticket=cmd.master_ticket
                      if master_ticket is None else master_ticket,
                      slave_ticket=ack.slave_ticket,
                      sl=sl, tp=tp, master_open_price=45_000.0, side=BUY,
                      magic=magic_for(cmd.master_ticket))


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