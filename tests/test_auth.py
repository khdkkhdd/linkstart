from unittest.mock import MagicMock

from linkstart.auth import get_browser_cookies


def test_unsupported_browser_returns_empty():
    assert get_browser_cookies("example.com", browser="netscape") == {}


def test_missing_dependency_returns_empty(monkeypatch):
    import sys
    # Setting sys.modules[name] = None makes `import name` raise ImportError.
    monkeypatch.setitem(sys.modules, "browser_cookie3", None)
    assert get_browser_cookies("example.com", browser="chrome") == {}


def test_loader_exception_returns_empty(monkeypatch):
    fake = MagicMock()
    fake.chrome.side_effect = RuntimeError("locked db")
    monkeypatch.setitem(__import__("sys").modules, "browser_cookie3", fake)
    assert get_browser_cookies("example.com", browser="chrome") == {}


def test_browser_cookie3_missing_loader_returns_empty(monkeypatch):
    """Defensive: a supported-browser name with no corresponding loader returns empty."""
    fake = MagicMock(spec=[])   # no attributes set → getattr returns None
    monkeypatch.setitem(__import__("sys").modules, "browser_cookie3", fake)
    assert get_browser_cookies("example.com", browser="chrome") == {}


def test_returns_cookie_dict_when_loader_succeeds(monkeypatch):
    class FakeCookie:
        def __init__(self, name, value):
            self.name = name
            self.value = value

    fake = MagicMock()
    fake.chrome.return_value = [FakeCookie("k1", "v1"), FakeCookie("k2", "v2")]
    monkeypatch.setitem(__import__("sys").modules, "browser_cookie3", fake)
    result = get_browser_cookies("example.com", browser="chrome")
    assert result == {"k1": "v1", "k2": "v2"}
