# pylint:disable-msg=E0611,I1101
"""
Browser rendering functions using Playwright.
"""

import logging
from typing import Dict, Optional, Tuple
from threading import BoundedSemaphore

LOGGER = logging.getLogger(__name__)

# Semaphore cache for bounded browser concurrency
_semaphore_cache: Dict[int, BoundedSemaphore] = {}


def get_semaphore(limit: int) -> BoundedSemaphore:
    """
    Get or create a cached BoundedSemaphore for the given parallel limit.
    
    Args:
        limit: Maximum number of parallel browser operations.
    
    Returns:
        BoundedSemaphore instance for the given limit.
    """
    if limit < 1:
        limit = 1  # Ensure minimum of 1
    
    if limit not in _semaphore_cache:
        _semaphore_cache[limit] = BoundedSemaphore(limit)
    
    return _semaphore_cache[limit]

# Lazy import of Playwright - only load if actually used
try:
    from playwright.sync_api import (
        sync_playwright,
        TimeoutError as PlaywrightTimeoutError,
    )

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    sync_playwright = None
    PlaywrightTimeoutError = None


def ensure_playwright_available() -> None:
    """
    Raise RuntimeError with clear installation guidance if Playwright is not available.

    Raises:
        RuntimeError: If Playwright is not installed with installation instructions.
    """
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            "Playwright is not installed. To use browser rendering, install it with:\n"
            "  pip install playwright\n"
            "  playwright install chromium\n"
            "For more information, see: https://playwright.dev/python/docs/intro"
        )


def render_page(
    url: str,
    timeout: int,
    wait_until: str,
    user_agent: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    no_ssl: bool = False,
) -> Tuple[bytes, int, str]:
    """
    Render a web page using Playwright and return the HTML content.

    Args:
        url: The URL to render
        timeout: Timeout in milliseconds
        wait_until: When to consider navigation successful ('load', 'domcontentloaded', 'networkidle', etc.)
        user_agent: Optional custom user agent string
        extra_headers: Optional additional HTTP headers
        no_ssl: If True, ignore SSL certificate errors

    Returns:
        Tuple of (html_bytes, status_code, final_url)

    Raises:
        RuntimeError: If Playwright is not installed
    """
    ensure_playwright_available()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=user_agent,
            ignore_https_errors=no_ssl,
            extra_http_headers=extra_headers or {},
        )
        page = context.new_page()
        
        try:
            response = page.goto(url, timeout=timeout, wait_until=wait_until)
            html = page.content().encode("utf-8")
            status = response.status if response is not None else 200
            final_url = page.url
        finally:
            browser.close()
        
        return (html, status, final_url)
