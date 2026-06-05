"""Browser cookie extraction for platforms whose live-state API requires login."""
import logging

log = logging.getLogger(__name__)


_SUPPORTED_BROWSERS = ("chrome", "firefox", "edge", "brave", "opera", "safari")


def get_browser_cookies(domain: str, browser: str = "chrome") -> dict[str, str]:
    """Return cookies for `domain` from `browser`'s cookie store.

    Returns an empty dict on any error (browser-cookie3 missing, unsupported
    browser, locked DB, no matches). Callers should treat empty as "auth
    unavailable" and decide whether to proceed.
    """
    if browser not in _SUPPORTED_BROWSERS:
        log.warning("unsupported browser: %s", browser)
        return {}

    try:
        import browser_cookie3 as bc3
    except ImportError:
        log.warning("browser-cookie3 not installed")
        return {}

    loader = getattr(bc3, browser, None)
    if loader is None:
        log.warning("browser-cookie3 has no loader for: %s", browser)
        return {}

    try:
        jar = loader(domain_name=domain)
    except Exception as e:
        log.warning("failed to load cookies for %s from %s: %s", domain, browser, e)
        return {}

    return {c.name: c.value for c in jar}
