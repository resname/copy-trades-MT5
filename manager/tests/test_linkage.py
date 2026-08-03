from manager.engine.linkage import (
    MAGIC_BASE, MAGIC_MOD, magic_for, encode_comment, decode_comment,
)


def test_magic_base_and_mod_match_ea():
    assert MAGIC_BASE == 1000000
    assert MAGIC_MOD == 900000


def test_magic_for_basic():
    assert magic_for(12345) == 1012345


def test_magic_for_wraps_modulo():
    # 912345 % 900000 == 12345 -> same magic as 12345 (EA collision behavior preserved)
    assert magic_for(912345) == 1012345
    # large ticket wraps
    assert magic_for(123456789) == MAGIC_BASE + (123456789 % MAGIC_MOD)


def test_encode_comment_format():
    assert encode_comment(12345, 0.5, 0.05) == "CPY#12345|MV0.50000000|SV0.05000000"


def test_decode_comment_full():
    assert decode_comment("CPY#12345|MV0.50000000|SV0.05000000") == (12345, 0.5, 0.05)


def test_decode_comment_ticket_only_no_pipe():
    assert decode_comment("CPY#12345") == (12345, None, None)


def test_decode_comment_missing_sv():
    # EA requires BOTH |MV and |SV to parse volumes; missing SV -> no volumes (None)
    assert decode_comment("CPY#12345|MV0.50000000") == (12345, None, None)


def test_decode_comment_no_prefix():
    assert decode_comment("manual trade") is None


def test_decode_comment_no_digits_after_prefix():
    assert decode_comment("CPY#abc") is None