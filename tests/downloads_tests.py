# pylint:disable-msg=W1401
"""
Unit tests for download functions from the trafilatura library.
"""

import gzip
import logging
import os
import sys
import zlib

try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False

try:
    import zstandard
    HAS_ZSTD = True
except ImportError:
    HAS_ZSTD = False

from time import sleep
from unittest.mock import patch

import pytest

from courlan import UrlStore

from trafilatura.cli import parse_args
from trafilatura.cli_utils import (download_queue_processing,
                                   url_processing_pipeline)
from trafilatura.core import Extractor, extract
import trafilatura.downloads
from trafilatura.downloads import (DEFAULT_HEADERS, HAS_PYCURL, USER_AGENT, Response,
                                   _determine_headers, _handle_response,
                                   _parse_config, _pycurl_is_live_page,
                                   _send_pycurl_request, _send_urllib_request,
                                   _urllib3_is_live_page,
                                   add_to_compressed_dict, fetch_url,
                                   is_live_page, load_download_buffer)
from trafilatura.settings import DEFAULT_CONFIG, args_to_extractor, use_config
from trafilatura.utils import decode_file, handle_compressed_file, load_html


logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

ZERO_CONFIG = DEFAULT_CONFIG
ZERO_CONFIG['DEFAULT']['MIN_OUTPUT_SIZE'] = '0'
ZERO_CONFIG['DEFAULT']['MIN_EXTRACTED_SIZE'] = '0'

RESOURCES_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'resources')
UA_CONFIG = use_config(filename=os.path.join(RESOURCES_DIR, 'newsettings.cfg'))

DEFAULT_OPTS = Extractor(config=DEFAULT_CONFIG)


def _reset_downloads_global_objects():
    """
    Force global objects to be re-created
    """
    trafilatura.downloads.PROXY_URL = None
    trafilatura.downloads.HTTP_POOL = None
    trafilatura.downloads.NO_CERT_POOL = None
    trafilatura.downloads.RETRY_STRATEGY = None


def test_response_object():
    "Test if the Response class is functioning as expected."
    my_html = b"<html><body><p>ABC</p></body></html>"
    resp = Response(my_html, 200, "https://example.org")
    assert bool(resp) is True
    resp.store_headers({"X-Header": "xyz"})
    assert "x-header" in resp.headers
    resp.decode_data(True)
    assert my_html.decode("utf-8") == resp.html == str(resp)
    my_dict = resp.as_dict()
    assert sorted(my_dict) == ["data", "headers", "html", "status", "url"]

    # response object: data, status, url
    response = Response("", 200, 'https://httpbin.org/encoding/utf8')
    for size in (10000000, 1):
        response.data = b'ABC'*size
        assert _handle_response(response.url, response, False, DEFAULT_OPTS) is None
    # straight handling of response object
    with open(os.path.join(RESOURCES_DIR, 'utf8.html'), 'rb') as filehandle:
        response.data = filehandle.read()
    assert _handle_response(response.url, response, False, DEFAULT_OPTS) is not None
    assert load_html(response) is not None
    # nothing to see here
    assert extract(response, url=response.url, config=ZERO_CONFIG) is None


def test_is_live_page():
    '''Test if pages are available on the network.'''
    # is_live general tests
    assert _urllib3_is_live_page('https://httpbun.com/status/301') is True
    assert _urllib3_is_live_page('https://httpbun.com/status/404') is False
    assert is_live_page('https://httpbun.com/status/403') is False
    # is_live pycurl tests
    if HAS_PYCURL:
        assert _pycurl_is_live_page('https://httpbun.com/status/301') is True


def test_fetch():
    '''Test URL fetching.'''
    # sanity check
    assert _send_urllib_request('', True, False, DEFAULT_CONFIG) is None

    # fetch_url
    assert fetch_url('#@1234') is None
    assert fetch_url('https://httpbun.com/status/404') is None

    # test if the functions default to no_ssl
    # False doesn't work?
    url = 'https://expired.badssl.com/'
    assert _send_urllib_request(url, True, False, DEFAULT_CONFIG) is not None
    if HAS_PYCURL:
        assert _send_pycurl_request(url, False, False, DEFAULT_CONFIG) is not None
    # no SSL, no decoding
    url = 'https://httpbun.com/status/200'
    for no_ssl in (True, False):
        response = _send_urllib_request(url, no_ssl, True, DEFAULT_CONFIG)
        assert b"200" in response.data and b"OK" in response.data  # JSON
        assert response.headers["x-powered-by"].startswith("httpbun")
    if HAS_PYCURL:
        response1 = _send_pycurl_request(url, True, True, DEFAULT_CONFIG)
        assert response1.headers["x-powered-by"].startswith("httpbun")
        assert _handle_response(url, response1, False, DEFAULT_OPTS).data == _handle_response(url, response, False, DEFAULT_OPTS).data
        assert _handle_response(url, response1, True, DEFAULT_OPTS) == _handle_response(url, response, True, DEFAULT_OPTS)

    # test handling of redirects
    res = fetch_url('https://httpbun.com/redirect/2')
    assert len(res) > 100  # We followed redirects and downloaded something in the end
    new_config = use_config()  # get a new config instance to avoid mutating the default one
    # patch max directs: limit to 0. We won't fetch any page as a result
    new_config.set('DEFAULT', 'MAX_REDIRECTS', '0')
    _reset_downloads_global_objects()  # force Retry strategy and PoolManager to be recreated with the new config value
    res = fetch_url('https://httpbun.com/redirect/1', config=new_config)
    assert res is None
    # also test max redir implementation on pycurl if available
    if HAS_PYCURL:
        assert _send_pycurl_request('https://httpbun.com/redirect/1', True, False, new_config) is None

    # test timeout
    new_config.set('DEFAULT', 'DOWNLOAD_TIMEOUT', '1')
    args = ('https://httpbun.com/delay/2', True, False, new_config)
    assert _send_urllib_request(*args) is None
    if HAS_PYCURL:
        assert _send_pycurl_request(*args) is None

    # test MAX_FILE_SIZE
    backup = ZERO_CONFIG.getint('DEFAULT', 'MAX_FILE_SIZE')
    ZERO_CONFIG.set('DEFAULT', 'MAX_FILE_SIZE', '1')
    args = ('https://httpbun.com/html', True, False, ZERO_CONFIG)
    assert _send_urllib_request(*args) is None
    if HAS_PYCURL:
        assert _send_pycurl_request(*args) is None
    ZERO_CONFIG.set('DEFAULT', 'MAX_FILE_SIZE', str(backup))

    # reset global objects again to avoid affecting other tests
    _reset_downloads_global_objects()


IS_PROXY_TEST = os.environ.get("PROXY_TEST", "false") == "true"

PROXY_URLS = (
    ("socks5://localhost:1080", True),
    ("socks5://user:pass@localhost:1081", True),
    ("socks5://localhost:10/", False),
    ("bogus://localhost:1080", False),
)


def proxied(f):
    "Run the download using a potentially malformed proxy address."
    for proxy_url, is_working in PROXY_URLS:
        _reset_downloads_global_objects()
        trafilatura.downloads.PROXY_URL = proxy_url
        if is_working:
            f()
        else:
            with pytest.raises(AssertionError):
                f()
    _reset_downloads_global_objects()


@pytest.mark.skipif(not IS_PROXY_TEST, reason="proxy tests disabled")
def test_proxied_is_live_page():
    proxied(test_is_live_page)


@pytest.mark.skipif(not IS_PROXY_TEST, reason="proxy tests disabled")
def test_proxied_fetch():
    proxied(test_fetch)


def test_config():
    '''Test how configuration options are read and stored.'''
    # default config is none
    assert _parse_config(DEFAULT_CONFIG) == (None, None)
    # default accept-encoding
    accepted = ['deflate', 'gzip']
    if HAS_BROTLI:
        accepted.append('br')
    if HAS_ZSTD:
        accepted.append('zstd')
    assert sorted(DEFAULT_HEADERS['accept-encoding'].split(',')) == sorted(accepted)
    # default user-agent
    default = _determine_headers(DEFAULT_CONFIG)
    assert default['User-Agent'] == USER_AGENT
    assert 'Cookie' not in default
    # user-agents rotation
    assert _parse_config(UA_CONFIG) == (['Firefox', 'Chrome'], 'yummy_cookie=choco; tasty_cookie=strawberry')
    custom = _determine_headers(UA_CONFIG)
    assert custom['User-Agent'] in ['Chrome', 'Firefox']
    assert custom['Cookie'] == 'yummy_cookie=choco; tasty_cookie=strawberry'


def test_decode():
    '''Test how responses are being decoded.'''
    html_string = "<html><head/><body><div>ABC</div></body></html>"
    assert decode_file(b" ") is not None

    compressed_strings = [
        gzip.compress(html_string.encode("utf-8")),
        zlib.compress(html_string.encode("utf-8")),
    ]
    if HAS_BROTLI:
        compressed_strings.append(brotli.compress(html_string.encode("utf-8")))
    if HAS_ZSTD:
        compressed_strings.append(zstandard.compress(html_string.encode("utf-8")))

    for compressed_string in compressed_strings:
        assert handle_compressed_file(compressed_string) == html_string.encode("utf-8")
        assert decode_file(compressed_string) == html_string

    # errors
    for bad_file in ("äöüß", b"\x1f\x8b\x08abc", b"\x28\xb5\x2f\xfdabc"):
        assert handle_compressed_file(bad_file) == bad_file


def test_queue():
    'Test creation, modification and download of URL queues.'
    # test conversion and storage
    url_store = add_to_compressed_dict(['ftps://www.example.org/', 'http://'])
    assert isinstance(url_store, UrlStore)

    # download buffer
    inputurls = [f'https://test{i}.org/{j}' for i in range(1, 7) for j in range(1, 4)]
    url_store = add_to_compressed_dict(inputurls)
    bufferlist, _ = load_download_buffer(url_store, sleep_time=5)
    assert len(bufferlist) == 6
    sleep(0.25)
    bufferlist, _ = load_download_buffer(url_store, sleep_time=0.1)
    assert len(bufferlist) == 6

    # CLI args
    url_store = add_to_compressed_dict(['https://www.example.org/'])
    testargs = ['', '--list']
    with patch.object(sys, 'argv', testargs):
        args = parse_args(testargs)
    assert url_processing_pipeline(args, url_store) is False

    # single/multiprocessing
    testargs = ['', '-v']
    with patch.object(sys, 'argv', testargs):
        args = parse_args(testargs)
    inputurls = [f'https://httpbun.com/status/{i}' for i in (301, 304, 200, 300, 400, 505)]
    url_store = add_to_compressed_dict(inputurls)
    args.archived = True
    args.config_file = os.path.join(RESOURCES_DIR, 'newsettings.cfg')
    options = args_to_extractor(args)
    options.config['DEFAULT']['SLEEP_TIME'] = '0.2'
    results = download_queue_processing(url_store, args, -1, options)
    assert len(results[0]) == 5 and results[1] is -1

def test_fetch_response_render_force_uses_browser():
    """Test that fetch_response uses browser path when render='force'."""
    from unittest.mock import Mock
    
    # Patch both send functions to isolate browser path
    with patch('trafilatura.downloads._send_urllib_request') as mock_urllib, \
         patch('trafilatura.downloads._send_pycurl_request') as mock_pycurl, \
         patch('trafilatura.downloads._send_browser_request') as mock_browser:
        
        # Browser should return Response-like object
        mock_browser.return_value = Response(b"<html>rendered</html>", 200, "https://example.org")
        
        # Call with render='force'
        response = trafilatura.downloads.fetch_response(
            "https://example.org",
            render="force",
            config=DEFAULT_CONFIG
        )
        
        # Assert browser path was used
        mock_browser.assert_called_once()
        mock_urllib.assert_not_called()
        mock_pycurl.assert_not_called()
        
        # Assert response is correct
        assert response is not None
        assert response.data == b"<html>rendered</html>"
        assert response.status == 200




def test_fetch_response_render_off_keeps_http_path():
    """Test that fetch_response uses HTTP path when render='off' or None."""
    from unittest.mock import Mock
    
    # Patch both send functions and browser
    with patch('trafilatura.downloads._send_urllib_request') as mock_urllib, \
         patch('trafilatura.downloads._send_pycurl_request') as mock_pycurl, \
         patch('trafilatura.downloads._send_browser_request') as mock_browser:
        
        # HTTP path should return Response
        mock_urllib.return_value = Response(b"<html>http content</html>", 200, "https://example.org")
        mock_pycurl.return_value = Response(b"<html>http content</html>", 200, "https://example.org")
        
        # Test render='off'
        response = trafilatura.downloads.fetch_response(
            "https://example.org",
            render="off",
            config=DEFAULT_CONFIG
        )
        
        # Browser should not be used
        mock_browser.assert_not_called()
        # One of the HTTP methods should be called (depends on HAS_PYCURL)
        assert mock_urllib.called or mock_pycurl.called
        assert response is not None
        assert response.data == b"<html>http content</html>"
        
        # Reset mocks
        mock_urllib.reset_mock()
        mock_pycurl.reset_mock()
        mock_browser.reset_mock()
        
        # Test render=None (default)
        response = trafilatura.downloads.fetch_response(
            "https://example.org",
            config=DEFAULT_CONFIG
        )
        
        # Browser should not be used
        mock_browser.assert_not_called()
        # One of the HTTP methods should be called
        assert mock_urllib.called or mock_pycurl.called


def test_buffered_response_downloads_passes_render_options():
    """Test that buffered_response_downloads passes render mode from options to fetch_response."""
    from trafilatura.downloads import buffered_response_downloads
    from trafilatura.settings import Extractor
    
    seen = {"render": None}

    def fake_fetch_response(url, **kwargs):
        seen["render"] = kwargs.get("render")
        return Response(b"<html></html>", 200, url)

    with patch('trafilatura.downloads.fetch_response', fake_fetch_response):
        options = Extractor(config=DEFAULT_CONFIG, render="force")
        
        list(buffered_response_downloads(["https://example.org"], 1, options=options))
        assert seen["render"] == "force"


def test_fetch_response_on_failure_falls_back_to_browser():
    """Test that on-failure mode falls back to browser for 503, None, or 408 HTTP responses."""
    from unittest.mock import Mock
    
    # Patch both send functions to simulate server error
    with patch('trafilatura.downloads._send_urllib_request') as mock_urllib, \
         patch('trafilatura.downloads._send_pycurl_request') as mock_pycurl, \
         patch('trafilatura.downloads._send_browser_request') as mock_browser:
        
        # HTTP returns 503 error
        mock_urllib.return_value = Response(b"", 503, "https://example.org")
        mock_pycurl.return_value = Response(b"", 503, "https://example.org")
        
        # Browser returns success
        mock_browser.return_value = Response(b"<html>ok</html>", 200, "https://example.org")
        
        # Call with render='on-failure'
        response = trafilatura.downloads.fetch_response(
            "https://example.org",
            render="on-failure",
            config=DEFAULT_CONFIG
        )
        
        # Assert HTTP was tried first, then browser fallback
        assert mock_urllib.called or mock_pycurl.called
        mock_browser.assert_called_once()
        
        # Assert browser response returned
        assert response is not None
        assert response.status == 200
        assert response.data == b"<html>ok</html>"


def test_fetch_response_on_failure_does_not_fallback_for_429():
    """Test that on-failure mode does NOT fall back to browser for 429 or other non-qualifying statuses."""
    from unittest.mock import Mock
    
    # Patch both send functions to simulate rate limit
    with patch('trafilatura.downloads._send_urllib_request') as mock_urllib, \
         patch('trafilatura.downloads._send_pycurl_request') as mock_pycurl, \
         patch('trafilatura.downloads._send_browser_request') as mock_browser:
        
        # HTTP returns 429 rate limit
        mock_urllib.return_value = Response(b"", 429, "https://example.org")
        mock_pycurl.return_value = Response(b"", 429, "https://example.org")
        
        # Call with render='on-failure'
        response = trafilatura.downloads.fetch_response(
            "https://example.org",
            render="on-failure",
            config=DEFAULT_CONFIG
        )
        
        # Assert HTTP was tried but browser fallback was NOT used
        assert mock_urllib.called or mock_pycurl.called
        mock_browser.assert_not_called()
        
        # Assert original 429 response returned (no fallback)
        assert response is not None
        assert response.status == 429


def test_auto_mode_detects_js_shell_and_fallbacks():
    """Test that auto mode detects JS shell and falls back to browser."""
    from unittest.mock import Mock
    
    # Patch both send functions to simulate JS shell response
    with patch('trafilatura.downloads._send_urllib_request') as mock_urllib, \
         patch('trafilatura.downloads._send_pycurl_request') as mock_pycurl, \
         patch('trafilatura.downloads._send_browser_request') as mock_browser:
        
        # HTTP returns empty shell with typical JS app markers
        js_shell_html = b"""<!DOCTYPE html>
<html>
<head><title>App</title></head>
<body>
  <div id="root"></div>
  <div id="app"></div>
  <script src="bundle.js"></script>
</body>
</html>"""
        mock_urllib.return_value = Response(js_shell_html, 200, "https://example.org")
        mock_pycurl.return_value = Response(js_shell_html, 200, "https://example.org")
        
        # Browser returns rendered content
        mock_browser.return_value = Response(b"<html><body><p>Rendered content</p></body></html>", 200, "https://example.org")
        
        # Call with render='auto'
        response = trafilatura.downloads.fetch_response(
            "https://example.org",
            render="auto",
            config=DEFAULT_CONFIG
        )
        
        # Assert HTTP was tried first, then browser fallback
        assert mock_urllib.called or mock_pycurl.called
        mock_browser.assert_called_once()
        
        # Assert browser response returned
        assert response is not None
        assert b"Rendered content" in response.data


def test_auto_mode_keeps_http_when_not_js_shell():
    """Test that auto mode keeps HTTP response when it's not a JS shell (has real content)."""
    from unittest.mock import Mock
    
    # Patch both send functions to simulate normal response
    with patch('trafilatura.downloads._send_urllib_request') as mock_urllib, \
         patch('trafilatura.downloads._send_pycurl_request') as mock_pycurl, \
         patch('trafilatura.downloads._send_browser_request') as mock_browser:
        
        # HTTP returns normal page with article-like content
        normal_html = b"""<!DOCTYPE html>
<html>
<head><title>Article Title</title></head>
<body>
  <article>
    <h1>Article Heading</h1>
    <p>This is a paragraph with real content. Articles contain meaningful text.</p>
    <p>Another paragraph with more content to ensure this is a real article.</p>
  </article>
</body>
</html>"""
        mock_urllib.return_value = Response(normal_html, 200, "https://example.org")
        mock_pycurl.return_value = Response(normal_html, 200, "https://example.org")
        
        # Call with render='auto'
        response = trafilatura.downloads.fetch_response(
            "https://example.org",
            render="auto",
            config=DEFAULT_CONFIG
        )
        
        # Assert HTTP was tried, browser NOT called
        assert mock_urllib.called or mock_pycurl.called
        mock_browser.assert_not_called()
        
        # Assert HTTP response returned (not browser)
        assert response is not None
        assert b"Article Heading" in response.data
if __name__ == '__main__':
    test_response_object()
    test_is_live_page()
    test_fetch()
    test_proxied_is_live_page()
    test_proxied_fetch()
    test_config()
    test_decode()
    test_queue()
