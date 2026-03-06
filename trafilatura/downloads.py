# pylint:disable-msg=E0611,I1101
"""
All functions needed to steer and execute downloads of web documents.
"""

import logging
import os
import random

from concurrent.futures import ThreadPoolExecutor, as_completed
from configparser import ConfigParser
from functools import partial
from importlib.metadata import version
from io import BytesIO
from time import sleep
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

import certifi
import urllib3

from courlan import UrlStore
from courlan.network import redirection_test

from .settings import DEFAULT_CONFIG, Extractor
from .utils import URL_BLACKLIST_REGEX, decode_file, is_acceptable_length, make_chunks

try:
    from urllib3.contrib.socks import SOCKSProxyManager

    PROXY_URL = os.environ.get("http_proxy")
except ImportError:
    PROXY_URL = None

try:
    import pycurl  # type: ignore

    CURL_SHARE = pycurl.CurlShare()
    # available options:
    # https://curl.se/libcurl/c/curl_share_setopt.html
    CURL_SHARE.setopt(pycurl.SH_SHARE, pycurl.LOCK_DATA_DNS)
    CURL_SHARE.setopt(pycurl.SH_SHARE, pycurl.LOCK_DATA_SSL_SESSION)
    # not thread-safe
    # CURL_SHARE.setopt(pycurl.SH_SHARE, pycurl.LOCK_DATA_CONNECT)
    HAS_PYCURL = True
except ImportError:
    HAS_PYCURL = False


LOGGER = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
HTTP_POOL = None
NO_CERT_POOL = None
RETRY_STRATEGY = None


def create_pool(**args: Any) -> Union[urllib3.PoolManager, Any]:
    "Configure urllib3 download pool according to user-defined settings."
    manager_class = SOCKSProxyManager if PROXY_URL else urllib3.PoolManager
    manager_args = {"proxy_url": PROXY_URL} if PROXY_URL else {}
    manager_args["num_pools"] = 50  # type: ignore[assignment]
    return manager_class(**manager_args, **args)  # type: ignore[arg-type]


DEFAULT_HEADERS = urllib3.util.make_headers(accept_encoding=True)  # type: ignore[no-untyped-call]
USER_AGENT = (
    "trafilatura/" + version("trafilatura") + " (+https://github.com/adbar/trafilatura)"
)
DEFAULT_HEADERS["User-Agent"] = USER_AGENT

FORCE_STATUS = [
    429,
    499,
    500,
    502,
    503,
    504,
    509,
    520,
    521,
    522,
    523,
    524,
    525,
    526,
    527,
    530,
    598,
]

CURL_SSL_ERRORS = {35, 54, 58, 59, 60, 64, 66, 77, 82, 83, 91}


class Response:
    "Store information gathered in a HTTP response object."
    __slots__ = ["data", "headers", "html", "status", "url"]

    def __init__(self, data: bytes, status: int, url: str) -> None:
        self.data = data
        self.headers: Optional[Dict[str, str]] = None
        self.html: Optional[str] = None
        self.status = status
        self.url = url

    def __bool__(self) -> bool:
        return self.data is not None

    def __repr__(self) -> str:
        return self.html or decode_file(self.data)

    def store_headers(self, headerdict: Dict[str, str]) -> None:
        "Store response headers if required."
        # further control steps here
        self.headers = {k.lower(): v for k, v in headerdict.items()}

    def decode_data(self, decode: bool) -> None:
        "Decode the bytestring in data and store a string in html."
        if decode and self.data:
            self.html = decode_file(self.data)

    def as_dict(self) -> Dict[str, str]:
        "Convert the response object to a dictionary."
        return {attr: getattr(self, attr) for attr in self.__slots__}


# caching throws an error
# @lru_cache(maxsize=2)
def _parse_config(config: ConfigParser) -> Tuple[Optional[List[str]], Optional[str]]:
    "Read and extract HTTP header strings from the configuration file."
    # load a series of user-agents
    myagents = config.get("DEFAULT", "USER_AGENTS", fallback="").strip()
    agent_list = myagents.splitlines() if myagents else None
    # https://developer.mozilla.org/en-US/docs/Web/HTTP/Cookies
    # todo: support for several cookies?
    mycookie = config.get("DEFAULT", "COOKIE") or None
    return agent_list, mycookie


def _determine_headers(
    config: ConfigParser, headers: Optional[Dict[str, str]] = None
) -> Dict[str, str]:
    "Internal function to decide on user-agent string."
    if config != DEFAULT_CONFIG:
        myagents, mycookie = _parse_config(config)
        headers = {}
        if myagents:
            headers["User-Agent"] = random.choice(myagents)
        if mycookie:
            headers["Cookie"] = mycookie
    return headers or DEFAULT_HEADERS


def _get_retry_strategy(config: ConfigParser) -> urllib3.util.Retry:
    "Define a retry strategy according to the config file."
    global RETRY_STRATEGY
    if not RETRY_STRATEGY:
        # or RETRY_STRATEGY.redirect != config.getint("DEFAULT", "MAX_REDIRECTS")
        RETRY_STRATEGY = urllib3.util.Retry(
            total=config.getint("DEFAULT", "MAX_REDIRECTS"),
            redirect=config.getint(
                "DEFAULT", "MAX_REDIRECTS"
            ),  # raise_on_redirect=False,
            connect=0,
            backoff_factor=config.getint("DEFAULT", "DOWNLOAD_TIMEOUT") / 2,
            status_forcelist=FORCE_STATUS,
            # unofficial: https://en.wikipedia.org/wiki/List_of_HTTP_status_codes#Unofficial_codes
        )
    return RETRY_STRATEGY


def _initiate_pool(
    config: ConfigParser, no_ssl: bool = False
) -> Union[urllib3.PoolManager, Any]:
    "Create a urllib3 pool manager according to options in the config file and HTTPS setting."
    global HTTP_POOL, NO_CERT_POOL
    pool = NO_CERT_POOL if no_ssl else HTTP_POOL

    if not pool:
        # define settings
        pool = create_pool(
            timeout=config.getint("DEFAULT", "DOWNLOAD_TIMEOUT"),
            ca_certs=None if no_ssl else certifi.where(),
            cert_reqs="CERT_NONE" if no_ssl else "CERT_REQUIRED",
        )
        # update variables
        if no_ssl:
            NO_CERT_POOL = pool
        else:
            HTTP_POOL = pool

    return pool


def _send_urllib_request(
    url: str, no_ssl: bool, with_headers: bool, config: ConfigParser
) -> Optional[Response]:
    "Internal function to robustly send a request (SSL or not) and return its result."
    try:
        pool_manager = _initiate_pool(config, no_ssl=no_ssl)

        # execute request, stop downloading as soon as MAX_FILE_SIZE is reached
        response = pool_manager.request(
            "GET",
            url,
            headers=_determine_headers(config),
            retries=_get_retry_strategy(config),
            preload_content=False,
        )
        data = bytearray()
        for chunk in response.stream(2**17):
            data.extend(chunk)
            if len(data) > config.getint("DEFAULT", "MAX_FILE_SIZE"):
                raise ValueError("MAX_FILE_SIZE exceeded")
        response.release_conn()

        # necessary for standardization
        resp = Response(bytes(data), response.status, response.geturl())
        if with_headers:
            resp.store_headers(response.headers)
        return resp

    except urllib3.exceptions.SSLError:
        LOGGER.warning("retrying after SSLError: %s", url)
        return _send_urllib_request(url, True, with_headers, config)
    except Exception as err:
        LOGGER.error("download error: %s %s", url, err)  # sys.exc_info()[0]

    return None


def _is_suitable_response(url: str, response: Response, options: Extractor) -> bool:
    "Check if the response conforms to formal criteria."
    lentest = len(response.html or response.data or "")
    if response.status != 200:
        LOGGER.error("not a 200 response: %s for URL %s", response.status, url)
        return False
    # raise error instead?
    if not is_acceptable_length(lentest, options):
        return False
    return True


def _handle_response(
    url: str, response: Response, decode: bool, options: Extractor
) -> Optional[Union[Response, str]]:  # todo: only return str
    "Internal function to run safety checks on response result."
    if _is_suitable_response(url, response, options):
        return response.html if decode else response
    # catchall
    return None




def _looks_like_js_shell(response: Response) -> bool:
    """Heuristic to detect if HTTP response is a JS shell requiring browser rendering.
    
    Conservative check: returns True only when shell markers present AND article-like content absent.
    This avoids over-rendering real pages that happen to use <div id='root'>.
    
    Args:
        response: Response object with HTML content.
    
    Returns:
        True if response looks like an empty JS shell, False otherwise.
    """
    # Decode HTML to text for pattern matching
    try:
        html_text = response.data.decode('utf-8', errors='ignore').lower()
    except Exception:
        return False  # Can't decode, assume not JS shell
    
    # Check for typical JS shell div IDs (root, app, etc.)
    shell_markers = [
        'id="root"', "id='root'",
        'id="app"', "id='app'",
        'id="__next"', "id='__next'"  # Next.js
    ]
    has_shell_marker = any(marker in html_text for marker in shell_markers)
    
    if not has_shell_marker:
        return False  # No shell markers, not a JS shell
    
    # Check for article-like content (headings, paragraphs with substantial text)
    # If we find real content, it's NOT a JS shell even if it has shell divs
    article_indicators = [
        '<article', '<p>', '<h1>', '<h2>', '<h3>',
    ]
    has_content = any(indicator in html_text for indicator in article_indicators)
    
    # Additional check: count text content outside script/style tags
    # JS shells have very little visible text
    import re
    # Remove script and style content
    text_only = re.sub(r'<script[^>]*>.*?</script>', '', html_text, flags=re.DOTALL)
    text_only = re.sub(r'<style[^>]*>.*?</style>', '', text_only, flags=re.DOTALL)
    # Remove HTML tags
    text_only = re.sub(r'<[^>]+>', '', text_only)
    # Count non-whitespace characters
    text_length = len(text_only.strip())
    
    # If we have shell markers, but also real content (>100 chars visible text or article tags), keep HTTP
    if has_content or text_length > 100:
        return False
    
    # Has shell markers, minimal content → likely JS shell
    return True

def _send_browser_request(
    url: str,
    no_ssl: bool,
    with_headers: bool,
    config: ConfigParser,
    render_timeout: Optional[int] = None,
    render_wait_until: Optional[str] = None,
    render_parallel: Optional[int] = None,
) -> Optional[Response]:
    """Use browser rendering backend to fetch page content.
    
    Bridges to trafilatura.browser.render_page and returns Response object.
    
    Args:
        url: URL of the page to fetch.
        no_ssl: Don't verify SSL certificate.
        with_headers: Keep track of response headers (ignored for browser path).
        config: Configuration for user-agent extraction.
        render_timeout: Browser timeout in milliseconds (overrides config).
        render_wait_until: Wait condition (overrides config).
        render_parallel: Browser concurrency limit (overrides config).
    """
    from . import browser
    
    # Use explicit parameters or fall back to config
    timeout = render_timeout if render_timeout is not None else config.getint("DEFAULT", "RENDER_TIMEOUT", fallback=30000)
    wait_until = render_wait_until if render_wait_until is not None else config.get("DEFAULT", "RENDER_WAIT_UNTIL", fallback="domcontentloaded")
    
    # Get render_parallel for semaphore
    parallel_limit = render_parallel if render_parallel is not None else config.getint("DEFAULT", "RENDER_PARALLEL", fallback=2)
    semaphore = browser.get_semaphore(parallel_limit)
    
    # Get MAX_FILE_SIZE for guard
    max_file_size = config.getint("DEFAULT", "MAX_FILE_SIZE", fallback=20000000)
    
    try:
        # Get user agent from config headers
        headers = _determine_headers(config)
        user_agent = headers.get("User-Agent")
        
        # Acquire semaphore to limit parallel browser operations
        with semaphore:
            # Call browser backend
            html_bytes, status_code, final_url = browser.render_page(
                url=url,
                timeout=timeout,
                wait_until=wait_until,
                user_agent=user_agent,
                no_ssl=no_ssl
            )
        
        # Enforce MAX_FILE_SIZE guard
        if len(html_bytes) > max_file_size:
            LOGGER.warning("Rendered HTML exceeds MAX_FILE_SIZE (%d > %d): %s", 
                          len(html_bytes), max_file_size, url)
            return None
        
        # Create Response object matching existing pattern
        resp = Response(html_bytes, status_code, final_url)
        
        # Note: browser rendering doesn't provide response headers easily,
        # so with_headers parameter is ignored for browser path
        
        return resp
        
    except Exception as err:
        LOGGER.error("browser request error: %s %s", url, err)
        return None


def fetch_url(
    url: str,
    no_ssl: bool = False,
    config: ConfigParser = DEFAULT_CONFIG,
    options: Optional[Extractor] = None,
    render: Optional[str] = None,
) -> Optional[str]:
    """Downloads a web page and seamlessly decodes the response.

    Args:
        url: URL of the page to fetch.
        no_ssl: Do not try to establish a secure connection (to prevent SSLError).
        config: Pass configuration values for output control.
        options: Extraction options (supersedes config).
        render: Render mode ('off', 'force', 'on-failure', 'auto'). None defaults to 'off'.

    Returns:
        Unicode string or None in case of failed downloads and invalid results.

    """
    config = options.config if options else config
    response = fetch_response(url, decode=True, no_ssl=no_ssl, config=config, options=options, render=render)
    if response and response.data:
        if not options:
            options = Extractor(config=config)
        if _is_suitable_response(url, response, options):
            return response.html
    return None


def fetch_response(
    url: str,
    *,
    decode: bool = False,
    no_ssl: bool = False,
    with_headers: bool = False,
    config: ConfigParser = DEFAULT_CONFIG,
    options: Optional[Extractor] = None,
    render: Optional[str] = None,
) -> Optional[Response]:
    """Downloads a web page and returns a full response object.

    Args:
        url: URL of the page to fetch.
        decode: Use html attribute to decode the data (boolean).
        no_ssl: Don't try to establish a secure connection (to prevent SSLError).
        with_headers: Keep track of the response headers.
        config: Pass configuration values for output control.
        options: Extraction options (supersedes config for render settings).
        render: Render mode ('off', 'force', 'on-failure', 'auto'). None uses options/config.

    Returns:
        Response object or None in case of failed downloads and invalid results.

    """
    # Resolve config from options if provided
    if options:
        config = options.config
    
    # Resolve render mode: explicit arg > options.render > config RENDER_MODE > 'off'
    if render is None:
        if options and hasattr(options, 'render') and options.render:
            render = options.render
        else:
            render = config.get("DEFAULT", "RENDER_MODE", fallback="off")
    
    # Resolve render timeout, wait_until, and parallel from options or config
    render_timeout = None
    render_wait_until = None
    render_parallel = None
    # Extract render settings for force/on-failure/auto modes
    if render in ("force", "on-failure", "auto"):
        if options and hasattr(options, 'render_timeout'):
            render_timeout = options.render_timeout
        if options and hasattr(options, 'render_wait_until'):
            render_wait_until = options.render_wait_until
        if options and hasattr(options, 'render_parallel'):
            render_parallel = options.render_parallel
    # Route based on render mode
    if render == "force":
        # Force browser rendering
        dl_function = lambda u, ns, wh, c: _send_browser_request(u, ns, wh, c, render_timeout, render_wait_until, render_parallel)
        LOGGER.debug("sending request: %s", url)
        response = dl_function(url, no_ssl, with_headers, config)
        if not response:
            LOGGER.debug("request failed: %s", url)
            return None
        response.decode_data(decode)
        return response
    
    elif render == "on-failure":
        # Try HTTP first, fallback to browser on qualifying failures
        dl_function = _send_urllib_request if not HAS_PYCURL else _send_pycurl_request
        LOGGER.debug("sending request: %s", url)
        http_response = dl_function(url, no_ssl, with_headers, config)
        
        # Check if fallback needed: None, 408, or status >= 500
        should_fallback = (
            http_response is None or 
            http_response.status == 408 or 
            http_response.status >= 500
        )
        
        if should_fallback:
            LOGGER.debug("HTTP failed with status %s, falling back to browser: %s", 
                        http_response.status if http_response else None, url)
            response = _send_browser_request(url, no_ssl, with_headers, config, render_timeout, render_wait_until, render_parallel)
        else:
            response = http_response
        
        if not response:
            LOGGER.debug("request failed: %s", url)
            return None
        response.decode_data(decode)
        return response
    
    elif render == "auto":
        # Try HTTP first, check heuristic, fallback to browser if looks like JS shell
        dl_function = _send_urllib_request if not HAS_PYCURL else _send_pycurl_request
        LOGGER.debug("sending request: %s", url)
        http_response = dl_function(url, no_ssl, with_headers, config)
        
        # Check if HTTP response looks like JS shell
        should_fallback = http_response and _looks_like_js_shell(http_response)
        
        if should_fallback:
            LOGGER.debug("HTTP response looks like JS shell, falling back to browser: %s", url)
            response = _send_browser_request(url, no_ssl, with_headers, config, render_timeout, render_wait_until, render_parallel)
        else:
            response = http_response
        
        if not response:
            LOGGER.debug("request failed: %s", url)
            return None
        response.decode_data(decode)
        return response
    
    else:
        # Default HTTP path (render='off', None, or other future modes)
        dl_function = _send_urllib_request if not HAS_PYCURL else _send_pycurl_request
        LOGGER.debug("sending request: %s", url)
        response = dl_function(url, no_ssl, with_headers, config)
        if not response:
            LOGGER.debug("request failed: %s", url)
            return None
        response.decode_data(decode)
        return response


def _pycurl_is_live_page(url: str) -> bool:
    "Send a basic HTTP HEAD request with pycurl."
    page_exists = False
    # Initialize pycurl object
    curl = pycurl.Curl()
    # Set the URL and HTTP method (HEAD)
    curl.setopt(pycurl.URL, url.encode("utf-8"))
    curl.setopt(pycurl.CONNECTTIMEOUT, 10)
    # no SSL verification
    curl.setopt(pycurl.SSL_VERIFYPEER, 0)
    curl.setopt(pycurl.SSL_VERIFYHOST, 0)
    # Set option to avoid getting the response body
    curl.setopt(curl.NOBODY, True)
    if PROXY_URL:
        curl.setopt(pycurl.PRE_PROXY, PROXY_URL)
    # Perform the request
    try:
        curl.perform()
        # Get the response code
        page_exists = curl.getinfo(curl.RESPONSE_CODE) < 400
    except pycurl.error as err:
        LOGGER.debug("pycurl HEAD error: %s %s", url, err)
        page_exists = False
    # Clean up
    curl.close()
    return page_exists


def _urllib3_is_live_page(url: str) -> bool:
    "Use courlan redirection test (based on urllib3) to send a HEAD request."
    try:
        _ = redirection_test(url)
    except Exception as err:
        LOGGER.debug("urllib3 HEAD error: %s %s", url, err)
        return False
    return True


def is_live_page(url: str) -> bool:
    "Send a HTTP HEAD request without taking anything else into account."
    result = _pycurl_is_live_page(url) if HAS_PYCURL else False
    # use urllib3 as backup
    return result or _urllib3_is_live_page(url)


def add_to_compressed_dict(
    inputlist: List[str],
    blacklist: Optional[Set[str]] = None,
    url_filter: Optional[str] = None,
    url_store: Optional[UrlStore] = None,
    compression: bool = False,
    verbose: bool = False,
) -> UrlStore:
    """Filter, convert input URLs and add them to domain-aware processing dictionary"""
    if url_store is None:
        url_store = UrlStore(compressed=compression, strict=False, verbose=verbose)

    inputlist = list(dict.fromkeys(inputlist))

    if blacklist:
        inputlist = [
            u for u in inputlist if URL_BLACKLIST_REGEX.sub("", u) not in blacklist
        ]

    if url_filter:
        inputlist = [u for u in inputlist if any(f in u for f in url_filter)]

    url_store.add_urls(inputlist)
    return url_store


def load_download_buffer(
    url_store: UrlStore, sleep_time: float = 5.0
) -> Tuple[List[str], UrlStore]:
    """Determine threading strategy and draw URLs respecting domain-based back-off rules."""
    while True:
        bufferlist = url_store.get_download_urls(time_limit=sleep_time, max_urls=10**5)
        if bufferlist or url_store.done:
            break
        sleep(sleep_time)
    return bufferlist, url_store


def _buffered_downloads(
    bufferlist: List[str],
    download_threads: int,
    worker: Callable[[str], Any],
    chunksize: int = 10000,
) -> Generator[Tuple[str, Any], None, None]:
    "Use a thread pool to perform a series of downloads."
    with ThreadPoolExecutor(max_workers=download_threads) as executor:
        for chunk in make_chunks(bufferlist, chunksize):
            future_to_url = {executor.submit(worker, url): url for url in chunk}
            for future in as_completed(future_to_url):
                yield future_to_url[future], future.result()


def buffered_downloads(
    bufferlist: List[str],
    download_threads: int,
    options: Optional[Extractor] = None,
) -> Generator[Tuple[str, str], None, None]:
    "Download queue consumer, single- or multi-threaded."
    worker = partial(fetch_url, options=options)

    return _buffered_downloads(bufferlist, download_threads, worker)


def buffered_response_downloads(
    bufferlist: List[str],
    download_threads: int,
    options: Optional[Extractor] = None,
) -> Generator[Tuple[str, Response], None, None]:
    "Download queue consumer, returns full Response objects."
    config = options.config if options else DEFAULT_CONFIG
    render_mode = options.render if options else None
    worker = partial(fetch_response, config=config, options=options, render=render_mode)

    return _buffered_downloads(bufferlist, download_threads, worker)


def _send_pycurl_request(
    url: str, no_ssl: bool, with_headers: bool, config: ConfigParser
) -> Optional[Response]:
    """Experimental function using libcurl and pycurl to speed up downloads"""
    # https://github.com/pycurl/pycurl/blob/master/examples/retriever-multi.py

    # init
    headerlist = [
        f"{header}: {content}" for header, content in _determine_headers(config).items()
    ]

    # prepare curl request
    # https://curl.haxx.se/libcurl/c/curl_easy_setopt.html
    curl = pycurl.Curl()
    curl.setopt(pycurl.URL, url.encode("utf-8"))
    # share data
    curl.setopt(pycurl.SHARE, CURL_SHARE)
    curl.setopt(pycurl.HTTPHEADER, headerlist)
    # curl.setopt(pycurl.USERAGENT, '')
    curl.setopt(pycurl.FOLLOWLOCATION, 1)
    curl.setopt(pycurl.MAXREDIRS, config.getint("DEFAULT", "MAX_REDIRECTS"))
    curl.setopt(pycurl.CONNECTTIMEOUT, config.getint("DEFAULT", "DOWNLOAD_TIMEOUT"))
    curl.setopt(pycurl.TIMEOUT, config.getint("DEFAULT", "DOWNLOAD_TIMEOUT"))
    curl.setopt(pycurl.MAXFILESIZE, config.getint("DEFAULT", "MAX_FILE_SIZE"))
    curl.setopt(pycurl.NOSIGNAL, 1)

    if no_ssl is True:
        curl.setopt(pycurl.SSL_VERIFYPEER, 0)
        curl.setopt(pycurl.SSL_VERIFYHOST, 0)
    else:
        curl.setopt(pycurl.CAINFO, certifi.where())

    if with_headers:
        headerbytes = BytesIO()
        curl.setopt(pycurl.HEADERFUNCTION, headerbytes.write)

    if PROXY_URL:
        curl.setopt(pycurl.PRE_PROXY, PROXY_URL)

    # TCP_FASTOPEN
    # curl.setopt(pycurl.FAILONERROR, 1)
    # curl.setopt(pycurl.ACCEPT_ENCODING, '')

    # send request
    try:
        bufferbytes = curl.perform_rb()
    except pycurl.error as err:
        LOGGER.error("pycurl error: %s %s", url, err)
        # retry in case of SSL-related error
        # see https://curl.se/libcurl/c/libcurl-errors.html
        # errmsg = curl.errstr_raw()
        # additional error codes: 80, 90, 96, 98
        if no_ssl is False and err.args[0] in CURL_SSL_ERRORS:
            LOGGER.debug("retrying after SSL error: %s %s", url, err)
            return _send_pycurl_request(url, True, with_headers, config)
        # traceback.print_exc(file=sys.stderr)
        # sys.stderr.flush()
        return None

    # additional info
    # ip_info = curl.getinfo(curl.PRIMARY_IP)

    resp = Response(
        bufferbytes, curl.getinfo(curl.RESPONSE_CODE), curl.getinfo(curl.EFFECTIVE_URL)
    )
    curl.close()

    if with_headers:
        respheaders = {}
        # https://github.com/pycurl/pycurl/blob/master/examples/quickstart/response_headers.py
        for line in (
            headerbytes.getvalue().decode("iso-8859-1", errors="replace").splitlines()
        ):
            # re.split(r'\r?\n') ?
            # This will botch headers that are split on multiple lines...
            if ":" not in line:
                continue
            # Break the header line into header name and value.
            name, value = line.split(":", 1)
            # Now we can actually record the header name and value.
            respheaders[name.strip()] = value.strip()  # name.strip().lower() ?
        resp.store_headers(respheaders)

    return resp
