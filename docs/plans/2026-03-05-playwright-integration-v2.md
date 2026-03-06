# Playwright Fallback Integration v2 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an optional Playwright rendering fallback to trafilatura so JS-rendered pages can be extracted successfully while preserving current HTTP behavior by default.

**Architecture:** Keep `urllib3/pycurl` as the default fast path and add a browser path that still returns `trafilatura.downloads.Response`. Store render policy in `Extractor` (slot-safe), propagate it from CLI/config/Python API, and dispatch centrally in `fetch_response()`. Apply conservative fallback policies (`force`, `on-failure`, `auto`), lazy Playwright imports, bounded browser concurrency, and strict timeouts.

**Tech Stack:** Python 3.8+, trafilatura core modules, Playwright (optional dependency), pytest, ThreadPoolExecutor, stdlib `http.server` fixtures.

---

### Task 1: Add render configuration contract (CLI + Extractor + config fallbacks)

**Files:**
- Modify: `trafilatura/cli.py`
- Modify: `trafilatura/settings.py`
- Modify: `trafilatura/settings.cfg`
- Test: `tests/cli_tests.py`

**Step 1: Write the failing tests**

```python
def test_parse_args_render_defaults():
    testargs = ["", "-u", "https://example.org"]
    with patch.object(sys, "argv", testargs):
        args = cli.parse_args(testargs)

    assert args.render == "off"
    assert args.render_timeout is None
    assert args.render_parallel is None
    assert args.render_wait_until == "domcontentloaded"


def test_args_to_extractor_has_render_fields():
    testargs = ["", "-u", "https://example.org"]
    with patch.object(sys, "argv", testargs):
        args = cli.parse_args(testargs)

    options = settings.args_to_extractor(args)
    assert options.render == "off"
    assert isinstance(options.render_timeout, int)
    assert isinstance(options.render_parallel, int)
    assert options.render_wait_until == "domcontentloaded"


def test_render_flag_help_mentions_modes(capsys):
    with pytest.raises(SystemExit):
        with patch.object(sys, "argv", ["", "--help"]):
            cli.parse_args(["--help"])

    out = capsys.readouterr().out
    assert "--render" in out
    assert "off" in out and "force" in out and "on-failure" in out and "auto" in out
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/cli_tests.py::test_parse_args_render_defaults tests/cli_tests.py::test_args_to_extractor_has_render_fields tests/cli_tests.py::test_render_flag_help_mentions_modes -v`
Expected: FAIL because render CLI args and `Extractor` render fields do not exist.

**Step 3: Write minimal implementation**

```python
# trafilatura/cli.py (add_args)
group3.add_argument(
    "--render",
    choices=["off", "force", "on-failure", "auto"],
    default="off",
    help="render strategy for JavaScript-heavy pages",
)
group3.add_argument("--render-timeout", type=int, help="render timeout in milliseconds")
group3.add_argument("--render-parallel", type=int, help="maximum concurrent browser renders")
group3.add_argument(
    "--render-wait-until",
    choices=["domcontentloaded", "load", "networkidle"],
    default="domcontentloaded",
)


# trafilatura/settings.py (Extractor.__slots__)
"render",
"render_timeout",
"render_parallel",
"render_wait_until",


# trafilatura/settings.py (Extractor.__init__ kwargs)
render: str = "off",
render_timeout: Optional[int] = None,
render_parallel: Optional[int] = None,
render_wait_until: str = "domcontentloaded",

# trafilatura/settings.py (Extractor.__init__ body)
self.render = render
self.render_timeout = render_timeout if render_timeout is not None else config.getint(
    "DEFAULT", "RENDER_TIMEOUT", fallback=config.getint("DEFAULT", "DOWNLOAD_TIMEOUT", fallback=30) * 1000
)
self.render_parallel = render_parallel if render_parallel is not None else config.getint(
    "DEFAULT", "RENDER_PARALLEL", fallback=2
)
self.render_wait_until = (
    render_wait_until
    or config.get("DEFAULT", "RENDER_WAIT_UNTIL", fallback="domcontentloaded")
)


# trafilatura/settings.py (args_to_extractor)
render=args.render,
render_timeout=args.render_timeout,
render_parallel=args.render_parallel,
render_wait_until=args.render_wait_until,
```

Also extend `trafilatura/settings.cfg` defaults (must be read with `fallback=`):

```ini
RENDER_MODE = off
RENDER_TIMEOUT = 30000
RENDER_PARALLEL = 2
RENDER_WAIT_UNTIL = domcontentloaded
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/cli_tests.py::test_parse_args_render_defaults tests/cli_tests.py::test_args_to_extractor_has_render_fields tests/cli_tests.py::test_render_flag_help_mentions_modes -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add trafilatura/cli.py trafilatura/settings.py trafilatura/settings.cfg tests/cli_tests.py
git commit -m "feat: add render config surface in CLI and Extractor"
```

### Task 2: Add browser backend module with lazy import and clear guardrails

**Files:**
- Create: `trafilatura/browser.py`
- Test: `tests/browser_tests.py`

**Step 1: Write the failing tests**

```python
def test_browser_import_error_is_clear(monkeypatch):
    from trafilatura import browser

    monkeypatch.setattr(browser, "PLAYWRIGHT_AVAILABLE", False)
    with pytest.raises(RuntimeError, match="Install playwright"):
        browser.ensure_playwright_available()


def test_render_page_signature_smoke(monkeypatch):
    from trafilatura import browser

    assert hasattr(browser, "render_page")
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/browser_tests.py::test_browser_import_error_is_clear tests/browser_tests.py::test_render_page_signature_smoke -v`
Expected: FAIL because `trafilatura/browser.py` does not exist.

**Step 3: Write minimal implementation**

```python
# trafilatura/browser.py
from typing import Dict, Optional, Tuple

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PlaywrightError = Exception  # type: ignore[assignment]
    PlaywrightTimeoutError = TimeoutError  # type: ignore[assignment]
    PLAYWRIGHT_AVAILABLE = False


def ensure_playwright_available() -> None:
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            "Playwright is not installed. Install playwright and browser binaries: "
            "pip install trafilatura[playwright] && python -m playwright install chromium"
        )


def render_page(
    url: str,
    timeout: int,
    wait_until: str,
    user_agent: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    no_ssl: bool = False,
) -> Tuple[bytes, int, str]:
    ensure_playwright_available()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=user_agent,
            ignore_https_errors=no_ssl,
            extra_http_headers=extra_headers or {},
        )
        page = context.new_page()
        response = page.goto(url, timeout=timeout, wait_until=wait_until)
        html = page.content().encode("utf-8")
        status = response.status if response is not None else 200
        final_url = page.url
        browser.close()
        return html, status, final_url
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/browser_tests.py::test_browser_import_error_is_clear tests/browser_tests.py::test_render_page_signature_smoke -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add trafilatura/browser.py tests/browser_tests.py
git commit -m "feat: add optional Playwright backend module"
```

### Task 3: Add `render=force` support to download API with Response-compatible browser adapter

**Files:**
- Modify: `trafilatura/downloads.py`
- Modify: `trafilatura/__init__.py`
- Test: `tests/downloads_tests.py`

**Step 1: Write the failing tests**

```python
import trafilatura.downloads as downloads


def test_fetch_response_render_force_uses_browser(monkeypatch):
    expected = downloads.Response(
        b"<html><body><article>hello</article></body></html>", 200, "https://example.org"
    )

    monkeypatch.setattr(downloads, "_send_browser_request", lambda *a, **k: expected, raising=False)
    monkeypatch.setattr(
        downloads,
        "_send_urllib_request",
        lambda *a, **k: downloads.Response(b"<html></html>", 200, "https://example.org"),
    )
    monkeypatch.setattr(
        downloads,
        "_send_pycurl_request",
        lambda *a, **k: downloads.Response(b"<html></html>", 200, "https://example.org"),
        raising=False,
    )

    response = downloads.fetch_response("https://example.org", decode=True, render="force")

    assert response is not None
    assert response.status == 200
    assert b"hello" in response.data


def test_fetch_response_render_off_keeps_http_path(monkeypatch):
    called = {"browser": False}

    def fake_browser(*args, **kwargs):
        called["browser"] = True
        return downloads.Response(b"<html></html>", 200, "https://example.org")

    monkeypatch.setattr(downloads, "_send_browser_request", fake_browser, raising=False)
    monkeypatch.setattr(
        downloads,
        "_send_urllib_request",
        lambda *a, **k: downloads.Response(b"<html></html>", 200, "https://example.org"),
    )
    monkeypatch.setattr(
        downloads,
        "_send_pycurl_request",
        lambda *a, **k: downloads.Response(b"<html></html>", 200, "https://example.org"),
        raising=False,
    )

    _ = downloads.fetch_response("https://example.org", render="off")

    assert called["browser"] is False
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/downloads_tests.py::test_fetch_response_render_force_uses_browser tests/downloads_tests.py::test_fetch_response_render_off_keeps_http_path -v`
Expected: FAIL because `fetch_response()` has no render support.

**Step 3: Write minimal implementation**

```python
# trafilatura/downloads.py
def _send_browser_request(
    url: str,
    no_ssl: bool,
    with_headers: bool,
    config: ConfigParser,
    render_timeout: int,
    render_wait_until: str,
) -> Optional[Response]:
    from .browser import render_page

    headers = _determine_headers(config)
    html, status, final_url = render_page(
        url=url,
        timeout=render_timeout,
        wait_until=render_wait_until,
        user_agent=headers.get("User-Agent"),
        extra_headers=headers,
        no_ssl=no_ssl,
    )
    return Response(html, status, final_url)


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
    config = options.config if options else config
    mode = render if render is not None else (options.render if options else config.get("DEFAULT", "RENDER_MODE", fallback="off"))
    render_timeout = options.render_timeout if options else config.getint("DEFAULT", "RENDER_TIMEOUT", fallback=30000)
    render_wait_until = options.render_wait_until if options else config.get("DEFAULT", "RENDER_WAIT_UNTIL", fallback="domcontentloaded")

    if mode == "force":
        response = _send_browser_request(url, no_ssl, with_headers, config, render_timeout, render_wait_until)
    else:
        dl_function = _send_urllib_request if not HAS_PYCURL else _send_pycurl_request
        response = dl_function(url, no_ssl, with_headers, config)

    if not response:
        return None
    response.decode_data(decode)
    return response


def fetch_url(
    url: str,
    no_ssl: bool = False,
    config: ConfigParser = DEFAULT_CONFIG,
    options: Optional[Extractor] = None,
    render: Optional[str] = None,
) -> Optional[str]:
    config = options.config if options else config
    response = fetch_response(
        url,
        decode=True,
        no_ssl=no_ssl,
        config=config,
        options=options,
        render=render,
    )
    if response and response.data:
        if not options:
            options = Extractor(config=config)
        if _is_suitable_response(url, response, options):
            return response.html
    return None
```

Keep `render="off"` as the implicit default everywhere for backward compatibility.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/downloads_tests.py::test_fetch_response_render_force_uses_browser tests/downloads_tests.py::test_fetch_response_render_off_keeps_http_path -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add trafilatura/downloads.py trafilatura/__init__.py tests/downloads_tests.py
git commit -m "feat: add force render mode in download API"
```

### Task 4: Propagate render mode through queue and crawler call chains

**Files:**
- Modify: `trafilatura/downloads.py`
- Modify: `trafilatura/cli_utils.py`
- Modify: `trafilatura/spider.py`
- Test: `tests/downloads_tests.py`
- Test: `tests/spider_tests.py`

**Step 1: Write the failing tests**

```python
import trafilatura.downloads as downloads


def test_buffered_response_downloads_passes_render_options(monkeypatch):
    seen = {"render": None}

    def fake_fetch_response(url, **kwargs):
        seen["render"] = kwargs.get("render")
        return downloads.Response(b"<html></html>", 200, url)

    monkeypatch.setattr(downloads, "fetch_response", fake_fetch_response)
    options = Extractor(config=DEFAULT_CONFIG, render="force")

    list(downloads.buffered_response_downloads(["https://example.org"], 1, options=options))
    assert seen["render"] == "force"


def test_probe_alternative_homepage_passes_render(monkeypatch):
    seen = {"render": None}

    def fake_fetch_response(url, **kwargs):
        seen["render"] = kwargs.get("render")
        return spider.Response(b"<html><body>ok</body></html>", 200, url)

    monkeypatch.setattr("trafilatura.spider.fetch_response", fake_fetch_response)
    monkeypatch.setattr("trafilatura.spider.refresh_detection", lambda html, homepage, render="off": (html, homepage))

    spider.probe_alternative_homepage("https://example.org", render="force")
    assert seen["render"] == "force"
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/downloads_tests.py::test_buffered_response_downloads_passes_render_options tests/spider_tests.py::test_probe_alternative_homepage_passes_render -v`
Expected: FAIL because render is not propagated across these call chains.

**Step 3: Write minimal implementation**

```python
# trafilatura/downloads.py
def buffered_response_downloads(
    bufferlist: List[str],
    download_threads: int,
    options: Optional[Extractor] = None,
) -> Generator[Tuple[str, Response], None, None]:
    config = options.config if options else DEFAULT_CONFIG
    render_mode = options.render if options else None
    worker = partial(fetch_response, config=config, options=options, render=render_mode)
    return _buffered_downloads(bufferlist, download_threads, worker)


# trafilatura/spider.py
def refresh_detection(
    htmlstring: str, homepage: str, render: str = "off"
) -> Tuple[Optional[str], Optional[str]]:
    if '"refresh"' not in htmlstring and '"REFRESH"' not in htmlstring:
        return htmlstring, homepage

    html_tree = load_html(htmlstring)
    if html_tree is None:
        return htmlstring, homepage

    results = html_tree.xpath(
        './/meta[@http-equiv="refresh" or @http-equiv="REFRESH"]/@content'
    )
    result = results[0] if results else ""
    if not result or ";" not in result:
        logging.info("no redirect found: %s", homepage)
        return htmlstring, homepage

    url2 = result.split(";")[1].strip().lower().replace("url=", "")
    if not url2.startswith("http"):
        base_url = get_base_url(url2)
        url2 = fix_relative_urls(base_url, url2)

    newhtmlstring = fetch_url(url2, render=render)
    if newhtmlstring is None:
        logging.warning("failed redirect: %s", url2)
        return None, None

    logging.info("successful redirect: %s", url2)
    return newhtmlstring, url2


def probe_alternative_homepage(
    homepage: str, render: str = "off"
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    response = fetch_response(homepage, decode=False, render=render)
    if not response or not response.data:
        return None, None, None

    if response.url not in (homepage, "/"):
        logging.info("followed homepage redirect: %s", response.url)
        homepage = response.url

    htmlstring = decode_file(response.data)
    new_htmlstring, new_homepage = refresh_detection(htmlstring, homepage, render=render)
    if new_homepage is None:
        return None, None, None

    logging.debug("fetching homepage OK: %s", new_homepage)
    return new_htmlstring, new_homepage, get_base_url(new_homepage)


def init_crawl(
    start: str,
    lang: Optional[str] = None,
    rules: Optional[RobotFileParser] = None,
    todo: Optional[List[str]] = None,
    known: Optional[List[str]] = None,
    prune_xpath: Optional[str] = None,
    render: str = "off",
) -> CrawlParameters:
    params = CrawlParameters(start, lang, rules, prune_xpath)

    URL_STORE.add_urls(urls=known or [], visited=True)
    URL_STORE.add_urls(urls=params.filter_list(todo))
    URL_STORE.store_rules(params.base, params.rules)

    if not todo:
        URL_STORE.add_urls(urls=[params.start], visited=False)
        params = crawl_page(params, initial=True, render=render)
    else:
        params.update_metadata(URL_STORE)

    return params


def crawl_page(
    params: CrawlParameters,
    initial: bool = False,
    render: str = "off",
) -> CrawlParameters:
    url = URL_STORE.get_url(params.base)
    if not url:
        params.is_on = False
        params.known_num = len(URL_STORE.find_known_urls(params.base))
        return params

    params.i += 1

    if initial:
        htmlstring, homepage, new_base_url = probe_alternative_homepage(url, render=render)
        if htmlstring and homepage and new_base_url:
            URL_STORE.add_urls([homepage])
            process_links(htmlstring, params, url=url)
    else:
        response = fetch_response(url, decode=False, render=render)
        process_response(response, params)

    params.update_metadata(URL_STORE)
    return params


# trafilatura/cli_utils.py
param_dict[hostname] = spider.init_crawl(startpage, lang=args.target_language, render=options.render)
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/downloads_tests.py::test_buffered_response_downloads_passes_render_options tests/spider_tests.py::test_probe_alternative_homepage_passes_render -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add trafilatura/downloads.py trafilatura/cli_utils.py trafilatura/spider.py tests/downloads_tests.py tests/spider_tests.py
git commit -m "feat: propagate render mode through queue and crawler paths"
```

### Task 5: Add `on-failure` fallback mode

**Files:**
- Modify: `trafilatura/downloads.py`
- Test: `tests/downloads_tests.py`

**Step 1: Write the failing tests**

```python
import trafilatura.downloads as downloads


def test_fetch_response_on_failure_falls_back_to_browser(monkeypatch):
    monkeypatch.setattr(
        downloads,
        "_send_urllib_request",
        lambda *a, **k: downloads.Response(b"", 503, "https://example.org"),
    )
    monkeypatch.setattr(
        downloads,
        "_send_pycurl_request",
        lambda *a, **k: downloads.Response(b"", 503, "https://example.org"),
        raising=False,
    )
    monkeypatch.setattr(
        downloads,
        "_send_browser_request",
        lambda *a, **k: downloads.Response(b"<html>ok</html>", 200, "https://example.org"),
    )

    response = downloads.fetch_response("https://example.org", render="on-failure")
    assert response is not None
    assert response.status == 200


def test_fetch_response_on_failure_does_not_fallback_for_429(monkeypatch):
    monkeypatch.setattr(
        downloads,
        "_send_urllib_request",
        lambda *a, **k: downloads.Response(b"", 429, "https://example.org"),
    )
    monkeypatch.setattr(
        downloads,
        "_send_pycurl_request",
        lambda *a, **k: downloads.Response(b"", 429, "https://example.org"),
        raising=False,
    )

    response = downloads.fetch_response("https://example.org", render="on-failure")
    assert response is not None
    assert response.status == 429
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/downloads_tests.py::test_fetch_response_on_failure_falls_back_to_browser tests/downloads_tests.py::test_fetch_response_on_failure_does_not_fallback_for_429 -v`
Expected: FAIL because `on-failure` policy is not implemented.

**Step 3: Write minimal implementation**

```python
dl_function = _send_urllib_request if not HAS_PYCURL else _send_pycurl_request
http_response = dl_function(url, no_ssl, with_headers, config)

if mode == "on-failure":
    response = http_response
    if response is None or response.status == 408 or response.status >= 500:
        response = _send_browser_request(
            url,
            no_ssl,
            with_headers,
            config,
            render_timeout,
            render_wait_until,
        )
    if not response:
        return None
    response.decode_data(decode)
    return response
```

Do not fallback automatically for `403`/`429`.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/downloads_tests.py::test_fetch_response_on_failure_falls_back_to_browser tests/downloads_tests.py::test_fetch_response_on_failure_does_not_fallback_for_429 -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add trafilatura/downloads.py tests/downloads_tests.py
git commit -m "feat: add on-failure browser fallback mode"
```

### Task 6: Add conservative `auto` heuristics

**Files:**
- Modify: `trafilatura/downloads.py`
- Test: `tests/downloads_tests.py`

**Step 1: Write the failing tests**

```python
import trafilatura.downloads as downloads


def test_auto_mode_detects_js_shell_and_fallbacks(monkeypatch):
    html = b"<html><body><div id='app'></div><script src='app.js'></script></body></html>"
    monkeypatch.setattr(
        downloads,
        "_send_urllib_request",
        lambda *a, **k: downloads.Response(html, 200, "https://example.org"),
    )
    monkeypatch.setattr(
        downloads,
        "_send_pycurl_request",
        lambda *a, **k: downloads.Response(html, 200, "https://example.org"),
        raising=False,
    )

    called = {"browser": False}

    def fake_browser(*args, **kwargs):
        called["browser"] = True
        return downloads.Response(
            b"<html><body><article>rendered</article></body></html>",
            200,
            "https://example.org",
        )

    monkeypatch.setattr(downloads, "_send_browser_request", fake_browser)
    _ = downloads.fetch_response("https://example.org", render="auto", decode=True)

    assert called["browser"] is True


def test_auto_mode_keeps_http_when_not_js_shell(monkeypatch):
    html = b"<html><body><article>plain</article></body></html>"
    monkeypatch.setattr(
        downloads,
        "_send_urllib_request",
        lambda *a, **k: downloads.Response(html, 200, "https://example.org"),
    )
    monkeypatch.setattr(
        downloads,
        "_send_pycurl_request",
        lambda *a, **k: downloads.Response(html, 200, "https://example.org"),
        raising=False,
    )

    called = {"browser": False}
    monkeypatch.setattr(
        downloads,
        "_send_browser_request",
        lambda *a, **k: called.__setitem__("browser", True),
    )

    _ = downloads.fetch_response("https://example.org", render="auto", decode=True)
    assert called["browser"] is False
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/downloads_tests.py::test_auto_mode_detects_js_shell_and_fallbacks tests/downloads_tests.py::test_auto_mode_keeps_http_when_not_js_shell -v`
Expected: FAIL because `auto` heuristics are missing.

**Step 3: Write minimal implementation**

```python
def _looks_like_js_shell(response: Response) -> bool:
    if response.status != 200 or not response.data:
        return False
    html = decode_file(response.data).lower()
    markers = (
        "id=\"app\"",
        "id='app'",
        "__next_data__",
        "data-reactroot",
        "window.__nuxt__",
        "enable javascript",
    )
    has_shell_marker = any(m in html for m in markers)
    has_article_like_content = "<article" in html or "<main" in html
    return has_shell_marker and not has_article_like_content


if mode == "auto":
    response = http_response
    if response is not None and _looks_like_js_shell(response):
        response = _send_browser_request(
            url,
            no_ssl,
            with_headers,
            config,
            render_timeout,
            render_wait_until,
        )
```

Keep heuristic strict and additive to avoid over-rendering.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/downloads_tests.py::test_auto_mode_detects_js_shell_and_fallbacks tests/downloads_tests.py::test_auto_mode_keeps_http_when_not_js_shell -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add trafilatura/downloads.py tests/downloads_tests.py
git commit -m "feat: add conservative auto render heuristics"
```

### Task 7: Add bounded browser concurrency and render timeout/size guardrails

**Files:**
- Modify: `trafilatura/browser.py`
- Modify: `trafilatura/downloads.py`
- Test: `tests/browser_tests.py`

**Step 1: Write the failing tests**

```python
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import sleep

import trafilatura.browser as browser
import trafilatura.downloads as downloads
from trafilatura.settings import Extractor, use_config


def test_browser_semaphore_limits_parallel_render(monkeypatch):
    cfg = use_config()
    opts = Extractor(
        config=cfg,
        render="force",
        render_parallel=2,
        render_timeout=1000,
        render_wait_until="domcontentloaded",
    )

    lock = Lock()
    state = {"active": 0, "max_active": 0}

    def fake_render_page(*args, **kwargs):
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        sleep(0.05)
        with lock:
            state["active"] -= 1
        return b"<html></html>", 200, kwargs.get("url", "https://example.org")

    monkeypatch.setattr(browser, "render_page", fake_render_page)

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [
            ex.submit(downloads.fetch_response, "https://example.org", options=opts, render="force")
            for _ in range(10)
        ]
        results = [f.result() for f in futures]

    assert all(r is not None for r in results)
    assert state["max_active"] <= 2


def test_browser_response_respects_max_file_size(monkeypatch):
    cfg = use_config()
    cfg.set("DEFAULT", "MAX_FILE_SIZE", "10")
    opts = Extractor(
        config=cfg,
        render="force",
        render_parallel=1,
        render_timeout=1000,
        render_wait_until="domcontentloaded",
    )

    monkeypatch.setattr(
        browser,
        "render_page",
        lambda *a, **k: (b"x" * 20, 200, "https://example.org"),
    )

    response = downloads.fetch_response("https://example.org", options=opts, render="force")
    assert response is None
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/browser_tests.py::test_browser_semaphore_limits_parallel_render tests/browser_tests.py::test_browser_response_respects_max_file_size -v`
Expected: FAIL because concurrency/size guards are missing.

**Step 3: Write minimal implementation**

```python
# trafilatura/browser.py
_SEMAPHORES: Dict[int, BoundedSemaphore] = {}
_SEMAPHORE_LOCK = Lock()


def get_semaphore(limit: int) -> BoundedSemaphore:
    limit = max(1, limit)
    with _SEMAPHORE_LOCK:
        if limit not in _SEMAPHORES:
            _SEMAPHORES[limit] = BoundedSemaphore(limit)
        return _SEMAPHORES[limit]


# trafilatura/downloads.py
parallel = options.render_parallel if options else config.getint("DEFAULT", "RENDER_PARALLEL", fallback=2)
semaphore = browser.get_semaphore(parallel)
with semaphore:
    html, status, final_url = browser.render_page(
        url=url,
        timeout=render_timeout,
        wait_until=render_wait_until,
        user_agent=headers.get("User-Agent"),
        extra_headers=headers,
        no_ssl=no_ssl,
    )

if len(html) > config.getint("DEFAULT", "MAX_FILE_SIZE"):
    LOGGER.error("rendered page too large: %s", url)
    return None
```

Enforce per-request timeout via `render_timeout` in the Playwright page navigation call.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/browser_tests.py::test_browser_semaphore_limits_parallel_render tests/browser_tests.py::test_browser_response_respects_max_file_size -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add trafilatura/browser.py trafilatura/downloads.py tests/browser_tests.py
git commit -m "feat: enforce browser concurrency and timeout/size guardrails"
```

### Task 8: Add packaging metadata and docs for optional Playwright workflow

**Files:**
- Modify: `pyproject.toml`
- Modify: `docs/usage-cli.rst`
- Modify: `docs/downloads.rst`
- Modify: `docs/troubleshooting.rst`

**Step 1: Write failing metadata/doc checks**

```python
from pathlib import Path


def test_playwright_extra_and_docs_are_declared():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    usage_cli = Path("docs/usage-cli.rst").read_text(encoding="utf-8")
    downloads_doc = Path("docs/downloads.rst").read_text(encoding="utf-8")

    assert "playwright =" in pyproject
    assert "playwright:" in pyproject
    assert "pip install trafilatura[playwright]" in usage_cli
    assert "--render" in usage_cli
    assert "fetch_response" in downloads_doc and "render" in downloads_doc
```

**Step 2: Run check to verify it fails**

Run: `python -c "from pathlib import Path; p=Path('pyproject.toml').read_text(); u=Path('docs/usage-cli.rst').read_text(); d=Path('docs/downloads.rst').read_text(); assert 'playwright =' in p and 'playwright:' in p and 'pip install trafilatura[playwright]' in u and '--render' in u and 'fetch_response' in d and 'render' in d"`
Expected: FAIL before packaging/docs updates.

**Step 3: Write minimal implementation**

```toml
[project.optional-dependencies]
playwright = [
  "playwright>=1.40.0,<2",
]

all = [
    "brotli",
    "cchardet >= 2.1.7; python_version < '3.11'",
    "faust-cchardet >= 2.1.19; python_version >= '3.11'",
    "htmldate[speed] >= 1.9.2",
    "py3langid >= 0.3.0",
    "pycurl >= 7.45.3",
    "urllib3[socks]",
    "zstandard >= 0.23.0",
    "playwright>=1.40.0,<2",
]

[tool.pytest.ini_options]
testpaths = "tests/*test*.py"
markers = [
  "playwright: tests requiring Playwright and browser binaries",
]
```

Docs updates:
- Install path: `pip install trafilatura[playwright]`
- Browser binary setup: `python -m playwright install chromium`
- Explain render modes (`off`, `force`, `on-failure`, `auto`) and timeout/parallel controls
- Explicit limitation: rendering helps dynamic content extraction but does not guarantee bypass of site restrictions

**Step 4: Run checks to verify they pass**

Run: `python -c "from pathlib import Path; p=Path('pyproject.toml').read_text(); u=Path('docs/usage-cli.rst').read_text(); d=Path('docs/downloads.rst').read_text(); assert 'playwright =' in p and 'playwright:' in p and 'pip install trafilatura[playwright]' in u and '--render' in u and 'fetch_response' in d and 'render' in d"`
Expected: PASS.

**Step 5: Commit**

```bash
git add pyproject.toml docs/usage-cli.rst docs/downloads.rst docs/troubleshooting.rst
git commit -m "docs: add optional Playwright workflow and packaging metadata"
```

### Task 9: Add integration fixture infrastructure and JS fallback integration tests

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/render_integration_tests.py`

**Step 1: Write the failing test**

```python
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
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/render_integration_tests.py::test_force_render_extracts_js_injected_content -v`
Expected: FAIL before fixture/server and end-to-end render path are complete.

**Step 3: Write minimal implementation**

```python
# tests/conftest.py
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def local_js_server_url():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"""<!doctype html>
<html><head><meta charset='utf-8'></head>
<body>
  <div id='app'></div>
  <script>
    document.getElementById('app').innerHTML =
      '<article>JS injected article body</article>';
  </script>
</body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args, **kwargs):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
```

Integration expectations:
- `render=off` stays on raw shell and should not extract injected target text
- `render=force` should include JS-injected article text

**Step 4: Run tests to verify they pass**

Run: `pytest tests/render_integration_tests.py -v`
Expected: PASS when Playwright + browser binaries exist, otherwise SKIPPED.

**Step 5: Commit**

```bash
git add tests/conftest.py tests/render_integration_tests.py
git commit -m "test: add Playwright integration coverage for JS fallback"
```

### Task 10: Final verification and regression safety

**Files:**
- Modify: none (verification only)

**Step 1: Run render-focused tests**

Run: `pytest tests/browser_tests.py tests/render_integration_tests.py tests/downloads_tests.py tests/cli_tests.py -v`
Expected: PASS (integration tests may be SKIPPED when Playwright is unavailable).

**Step 2: Run crawler/download regression tests**

Run: `pytest tests/spider_tests.py tests/sitemaps_tests.py tests/downloads_tests.py -v`
Expected: PASS, proving default HTTP behavior remains stable.

**Step 3: Run CLI sanity check**

Run: `python -m trafilatura.cli --help`
Expected: command succeeds and includes render options.

**Step 4: Run Python API smoke checks**

Run: `python -c "from trafilatura import fetch_url; print(len(fetch_url('https://example.org') or ''))"`
Expected: numeric output, no exception.

Run: `python -c "from trafilatura import fetch_response; print((fetch_response('https://example.org', render='off') or None) is not None)"`
Expected: `True` (or stable network-failure behavior), no API errors.

**Step 5: Commit (only if files changed during verification)**

```bash
git status
```

If no changes: do not create empty commit.

---

## Notes For Executor

- Keep default mode `render=off` for full backward compatibility.
- Preserve current project stance: rendering is for dynamic content retrieval, not guaranteed anti-bot bypass.
- New config keys must always be read with `fallback=` to keep custom `settings.cfg` files compatible.
- Keep Playwright imports lazy so users not using render mode do not need Playwright installed.
- Keep browser and HTTP paths returning the same `Response` shape to avoid breaking downstream code.
