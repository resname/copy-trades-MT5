from manager.engine.transform import parse_symbol_map, SymbolMapper


def test_parse_symbol_map_basic():
    assert parse_symbol_map("EURUSD=EURUSD,GBPUSD=GBPUSD") == {
        "EURUSD": "EURUSD", "GBPUSD": "GBPUSD",
    }


def test_parse_symbol_map_strips_spaces_and_skips_invalid():
    # spaces stripped; pair without '=' skipped; pair with 3 sides skipped
    assert parse_symbol_map("EURUSD = EURUSD , GBPUSD, X=Y=Z") == {"EURUSD": "EURUSD"}


def test_parse_symbol_map_empty():
    assert parse_symbol_map("") == {}


def test_resolve_explicit_mapping_wins():
    mapper = SymbolMapper("EURUSD=EURUSD_Z", exists_check=lambda s: False)
    assert mapper.resolve("EURUSD") == "EURUSD_Z"


def test_resolve_same_name_fallback_when_exists():
    mapper = SymbolMapper("", exists_check=lambda s: s == "USDJPY")
    assert mapper.resolve("USDJPY") == "USDJPY"


def test_resolve_returns_empty_when_missing_and_no_fallback():
    mapper = SymbolMapper("", exists_check=lambda s: False)
    assert mapper.resolve("XYZABC") == ""


def test_resolve_explicit_overrides_fallback():
    # explicit map present but slave symbol does not exist -> still returns the
    # explicit mapping (the EA returns the explicit value without an existence
    # check on the mapped symbol).
    mapper = SymbolMapper("EURUSD=EURUSD_Z", exists_check=lambda s: False)
    assert mapper.resolve("EURUSD") == "EURUSD_Z"