import pytest

from trafilatura import extract, fetch_url
from trafilatura.core import Extractor
from trafilatura.settings import DEFAULT_CONFIG


try:
    from playwright.sync_api import sync_playwright  # noqa: F401

    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False


@pytest.mark.playwright
@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
def test_force_render_extracts_js_injected_content(local_js_server_url):
    options = Extractor(config=DEFAULT_CONFIG, render="force")
    html = fetch_url(local_js_server_url, options=options)
    text = extract(html or "", url=local_js_server_url)
    assert "JS injected article body" in (text or "")


@pytest.mark.playwright
@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
def test_render_off_does_not_extract_js_injected_content(local_js_server_url):
    options = Extractor(config=DEFAULT_CONFIG, render="off")
    html = fetch_url(local_js_server_url, options=options)
    text = extract(html or "", url=local_js_server_url)
    assert "JS injected article body" not in (text or "")
