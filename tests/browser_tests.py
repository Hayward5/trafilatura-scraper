# pylint:disable-msg=W1401
"""
Unit tests for browser rendering functions from the trafilatura library.
"""

import pytest


def test_browser_import_error_is_clear():
    """Test that missing Playwright gives a clear actionable error."""
    # Import the module - this should succeed even without Playwright installed
    from trafilatura import browser

    # The flag should tell us if Playwright is available
    if not browser.PLAYWRIGHT_AVAILABLE:
        # Calling ensure should raise RuntimeError with clear guidance
        with pytest.raises(RuntimeError) as exc_info:
            browser.ensure_playwright_available()

        error_msg = str(exc_info.value)
        # Verify error message contains installation guidance
        assert "playwright" in error_msg.lower()
        assert "install" in error_msg.lower()


def test_render_page_signature_smoke():
    """Test render_page function exists with correct signature."""
    from trafilatura import browser

    # Function should exist
    assert hasattr(browser, "render_page")

    # Check basic signature (this will fail if Playwright is not installed,
    # but that's OK - we just want to ensure the function signature exists)
    import inspect

    sig = inspect.signature(browser.render_page)
    params = list(sig.parameters.keys())

    # Required parameters
    assert "url" in params
    assert "timeout" in params
    assert "wait_until" in params

    # Optional parameters
    assert "user_agent" in params
    assert "extra_headers" in params
    assert "no_ssl" in params


def test_browser_semaphore_limits_parallel_render():
    """Test that browser rendering respects parallel limit via semaphore."""
    from trafilatura import browser
    from threading import BoundedSemaphore
    
    # Get semaphore with limit 2
    sem1 = browser.get_semaphore(2)
    assert isinstance(sem1, BoundedSemaphore)
    
    # Getting same limit should return cached instance
    sem2 = browser.get_semaphore(2)
    assert sem1 is sem2
    
    # Different limit should return different instance
    sem3 = browser.get_semaphore(3)
    assert sem3 is not sem1


def test_browser_response_respects_max_file_size():
    """Test that browser rendering returns None when rendered HTML exceeds MAX_FILE_SIZE."""
    from trafilatura.downloads import _send_browser_request
    from unittest.mock import patch, MagicMock
    from configparser import ConfigParser
    
    config = ConfigParser()
    config.read_string("""
[DEFAULT]
MAX_FILE_SIZE = 100
RENDER_PARALLEL = 1
COOKIE =
USER_AGENTS =
""")
    
    # Mock render_page to return oversized HTML
    huge_html = b"x" * 150  # Exceeds MAX_FILE_SIZE=100
    
    with patch('trafilatura.browser.render_page') as mock_render:
        with patch('trafilatura.browser.get_semaphore') as mock_semaphore:
            # Mock semaphore context manager
            mock_sem_instance = MagicMock()
            mock_semaphore.return_value = mock_sem_instance
            
            mock_render.return_value = (huge_html, 200, "http://example.com")
            
            result = _send_browser_request(
                url="http://example.com",
                no_ssl=False,
                with_headers=False,
                config=config
            )
            
            # Should return None when size exceeds limit
            assert result is None
            
            # Verify render_page was called
            mock_render.assert_called_once()


def test_browser_respects_options_render_parallel():
    """Test that browser rendering uses options.render_parallel when provided."""
    from trafilatura.downloads import _send_browser_request
    from trafilatura.settings import Extractor, DEFAULT_CONFIG
    from unittest.mock import patch, MagicMock
    
    # Create options with custom render_parallel using DEFAULT_CONFIG
    options = Extractor(config=DEFAULT_CONFIG, render_parallel=5)
    
    with patch('trafilatura.browser.render_page') as mock_render:
        with patch('trafilatura.browser.get_semaphore') as mock_semaphore:
            # Mock semaphore
            mock_sem_instance = MagicMock()
            mock_semaphore.return_value = mock_sem_instance
            
            mock_render.return_value = (b"<html></html>", 200, "http://example.com")
            
            result = _send_browser_request(
                url="http://example.com",
                no_ssl=False,
                with_headers=False,
                config=DEFAULT_CONFIG,
                render_parallel=options.render_parallel  # Pass options value
            )
            
            # Verify get_semaphore was called with options value (5), not config default (2)
            mock_semaphore.assert_called_once_with(5)
            
            # Should return valid response
            assert result is not None
