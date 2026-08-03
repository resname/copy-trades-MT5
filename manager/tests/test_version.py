def test_version_is_string():
    from manager._version import __version__
    assert isinstance(__version__, str)
    assert __version__ and __version__[0].isdigit()