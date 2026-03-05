# Playwright Fallback Integration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an optional Playwright rendering fallback to trafilatura so JS-rendered pages can be extracted successfully without changing default HTTP behavior.

**Architecture:** Keep `urllib3/pycurl` as the default fast path and introduce a separate browser backend that returns the same `Response` shape. Route requests through an explicit render mode (`off`, `force`, `on-failure`, `auto`) selected from Python API and CLI flags. Use conservative fallback heuristics, bounded browser concurrency, and strict timeouts so large crawls remain predictable.

**Tech Stack:** Python 3.8+, trafilatura core modules, Playwright (optional dependency), pytest, ThreadPoolExecutor.

---

### Task 1: Add render configuration surface (Extractor + config + CLI args)

**Files:**
- Modify: `trafilatura/cli.py`
- Modify: `trafilatura/settings.py`
- Modify: `trafilatura/settings.cfg`
- Test: `tests/cli_tests.py`

**Step 1: Write the failing test**

```python
def test_parse_args_render_defaults():
    args = parse_args(["-u", "https://example.org"])
    assert args.render == "off"
    assert args.render_timeout is None
    assert args.render_parallel is None
    assert args.render_wait_until == "domcontentloaded"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/cli_tests.py::test_parse_args_render_defaults -v`
Expected: FAIL because render CLI options do not exist.

**Step 3: Write minimal implementation**

```python
# in trafilatura/cli.py group3/group4 area
group3.add_argument("--render", choices=["off", "force", "on-failure", "auto"], default="off")
group3.add_argument("--render-timeout", type=int)
group3.add_argument("--render-parallel", type=int)
group3.add_argument("--render-wait-until", choices=["domcontentloaded", "load", "networkidle"], default="domcontentloaded")

# in trafilatura/settings.py args_to_extractor()
# store new render values on options object
```

Also extend `settings.cfg` with optional defaults (read with `fallback=` to preserve custom config compatibility).

**Step 4: Run test to verify it passes**

Run: `pytest tests/cli_tests.py::test_parse_args_render_defaults -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add trafilatura/cli.py trafilatura/settings.py trafilatura/settings.cfg tests/cli_tests.py
git commit -m "feat: add render mode CLI and extractor config surface"
```

### Task 2: Add browser backend module with Playwright guardrails

**Files:**
- Create: `trafilatura/browser.py`
- Test: `tests/browser_tests.py`

**Step 1: Write the failing test**

```python
def test_browser_import_error_is_clear(monkeypatch):
    from trafilatura.browser import ensure_playwright_available
    monkeypatch.setattr("trafilatura.browser.PLAYWRIGHT_AVAILABLE", False)
    with pytest.raises(RuntimeError, match="Install playwright"):
        ensure_playwright_available()
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/browser_tests.py::test_browser_import_error_is_clear -v`
Expected: FAIL because `trafilatura/browser.py` does not exist.

**Step 3: Write minimal implementation**

```python
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

def ensure_playwright_available() -> None:
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("Playwright is not installed. Install playwright and browser binaries.")
```

Add `render_page(url, timeout, wait_until, user_agent, extra_headers)` returning rendered HTML and final URL.

**Step 4: Run test to verify it passes**

Run: `pytest tests/browser_tests.py::test_browser_import_error_is_clear -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add trafilatura/browser.py tests/browser_tests.py
git commit -m "feat: add optional Playwright browser backend module"
```

### Task 3: Add `render=force` support in download API

**Files:**
- Modify: `trafilatura/downloads.py`
- Modify: `trafilatura/__init__.py`
- Test: `tests/downloads_tests.py`

**Step 1: Write the failing test**

```python
def test_fetch_response_render_force_uses_browser(monkeypatch):
    from trafilatura.downloads import fetch_response
    def fake_render(*args, **kwargs):
        return b"<html><body><article>hello</article></body></html>", 200, "https://example.org"
    monkeypatch.setattr("trafilatura.downloads._send_browser_request", fake_render)
    resp = fetch_response("https://example.org", render="force")
    assert resp is not None
    assert b"hello" in resp.data
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/downloads_tests.py::test_fetch_response_render_force_uses_browser -v`
Expected: FAIL with unexpected keyword argument `render`.

**Step 3: Write minimal implementation**

```python
def fetch_response(..., render: str = "off", render_options: Optional[Dict[str, Any]] = None):
    if render == "force":
        return _send_browser_request(url, decode, with_headers, config, render_options)
    # existing HTTP path unchanged
```

Keep existing defaults fully backward compatible when `render="off"`.

**Step 4: Run test to verify it passes**

Run: `pytest tests/downloads_tests.py::test_fetch_response_render_force_uses_browser -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add trafilatura/downloads.py trafilatura/__init__.py tests/downloads_tests.py
git commit -m "feat: support force render mode in download API"
```

### Task 4: Add `on-failure` fallback mode

**Files:**
- Modify: `trafilatura/downloads.py`
- Test: `tests/downloads_tests.py`

**Step 1: Write the failing test**

```python
def test_fetch_response_on_failure_falls_back_to_browser(monkeypatch):
    monkeypatch.setattr("trafilatura.downloads._send_urllib_request", lambda *a, **k: None)
    monkeypatch.setattr("trafilatura.downloads._send_browser_request", lambda *a, **k: Response(b"<html>x</html>", 200, "https://example.org"))
    resp = fetch_response("https://example.org", render="on-failure")
    assert resp is not None
    assert resp.status == 200
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/downloads_tests.py::test_fetch_response_on_failure_falls_back_to_browser -v`
Expected: FAIL because fallback logic is missing.

**Step 3: Write minimal implementation**

```python
if render == "on-failure":
    response = http_response
    if response is None or response.status >= 500 or response.status == 408:
        return _send_browser_request(...)
    return response
```

Do not auto-fallback for 403/429 in default policy.

**Step 4: Run test to verify it passes**

Run: `pytest tests/downloads_tests.py::test_fetch_response_on_failure_falls_back_to_browser -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add trafilatura/downloads.py tests/downloads_tests.py
git commit -m "feat: add on-failure render fallback for download API"
```

### Task 5: Add conservative `auto` fallback heuristics

**Files:**
- Modify: `trafilatura/downloads.py`
- Test: `tests/downloads_tests.py`

**Step 1: Write the failing test**

```python
def test_auto_mode_detects_js_shell_and_fallbacks(monkeypatch):
    html = b"<html><body><div id='app'></div><script src='app.js'></script></body></html>"
    monkeypatch.setattr("trafilatura.downloads._send_urllib_request", lambda *a, **k: Response(html, 200, "https://example.org"))
    called = {"browser": False}
    def fake_browser(*args, **kwargs):
        called["browser"] = True
        return Response(b"<html><body><article>rendered</article></body></html>", 200, "https://example.org")
    monkeypatch.setattr("trafilatura.downloads._send_browser_request", fake_browser)
    _ = fetch_response("https://example.org", render="auto", decode=True)
    assert called["browser"] is True
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/downloads_tests.py::test_auto_mode_detects_js_shell_and_fallbacks -v`
Expected: FAIL because auto heuristic is missing.

**Step 3: Write minimal implementation**

```python
def _looks_like_js_shell(response: Response) -> bool:
    html = decode_file(response.data or b"")
    markers = ("id=\"app\"", "__NEXT_DATA__", "data-reactroot", "enable javascript")
    return response.status == 200 and any(m in html for m in markers)

if render == "auto" and response and _looks_like_js_shell(response):
    return _send_browser_request(...)
```

Keep heuristic strict and additive to avoid over-rendering.

**Step 4: Run test to verify it passes**

Run: `pytest tests/downloads_tests.py::test_auto_mode_detects_js_shell_and_fallbacks -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add trafilatura/downloads.py tests/downloads_tests.py
git commit -m "feat: add conservative auto render heuristics"
```

### Task 6: Wire render options into CLI URL pipeline

**Files:**
- Modify: `trafilatura/cli_utils.py`
- Modify: `trafilatura/settings.py`
- Test: `tests/cli_tests.py`

**Step 1: Write the failing test**

```python
def test_url_pipeline_passes_render_mode(monkeypatch):
    seen = {"render": None}
    def fake_fetch(url, options=None):
        seen["render"] = options.render
        return "<html><body>ok</body></html>"
    monkeypatch.setattr("trafilatura.cli_utils.fetch_url", fake_fetch)
    # call pipeline with args.render='force'
    assert seen["render"] == "force"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/cli_tests.py::test_url_pipeline_passes_render_mode -v`
Expected: FAIL because render options are not propagated.

**Step 3: Write minimal implementation**

```python
# in args_to_extractor, store render fields on options
options.render = args.render
options.render_timeout = args.render_timeout
options.render_parallel = args.render_parallel
options.render_wait_until = args.render_wait_until

# in download path, call fetch_url(..., options=options) and consume options.render internally
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/cli_tests.py::test_url_pipeline_passes_render_mode -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add trafilatura/cli_utils.py trafilatura/settings.py tests/cli_tests.py
git commit -m "feat: propagate render options through CLI pipeline"
```

### Task 7: Add bounded browser concurrency and timeout budget

**Files:**
- Modify: `trafilatura/browser.py`
- Modify: `trafilatura/downloads.py`
- Test: `tests/browser_tests.py`

**Step 1: Write the failing test**

```python
def test_browser_semaphore_limits_parallel_render(monkeypatch):
    # start N threads and ensure active browser jobs never exceed configured limit
    assert max_active_jobs <= 2
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/browser_tests.py::test_browser_semaphore_limits_parallel_render -v`
Expected: FAIL because no concurrency limiter exists.

**Step 3: Write minimal implementation**

```python
RENDER_SEMAPHORE = BoundedSemaphore(value=render_parallel)

with RENDER_SEMAPHORE:
    html = render_page(url=url, timeout=render_timeout, wait_until=wait_until)
```

Enforce per-request timeout and maximum rendered size check (`MAX_FILE_SIZE`).

**Step 4: Run test to verify it passes**

Run: `pytest tests/browser_tests.py::test_browser_semaphore_limits_parallel_render -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add trafilatura/browser.py trafilatura/downloads.py tests/browser_tests.py
git commit -m "feat: enforce bounded browser concurrency and timeout budget"
```

### Task 8: Add packaging and docs for optional Playwright workflow

**Files:**
- Modify: `pyproject.toml`
- Modify: `docs/troubleshooting.rst`
- Modify: `docs/usage-cli.rst`
- Test: `tests/cli_tests.py`

**Step 1: Write the failing test**

```python
def test_render_flag_help_mentions_modes(capsys):
    with pytest.raises(SystemExit):
        parse_args(["--help"])
    out = capsys.readouterr().out
    assert "--render" in out
    assert "off" in out and "force" in out and "on-failure" in out and "auto" in out
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/cli_tests.py::test_render_flag_help_mentions_modes -v`
Expected: FAIL before docs/help text updates are complete.

**Step 3: Write minimal implementation**

```toml
[project.optional-dependencies]
playwright = [
  "playwright>=1.52.0",
]
```

Update docs with install and runtime notes:
- `pip install trafilatura[playwright]`
- `python -m playwright install chromium`
- Explain render modes and that bypassing site restrictions is not guaranteed.

**Step 4: Run test to verify it passes**

Run: `pytest tests/cli_tests.py::test_render_flag_help_mentions_modes -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add pyproject.toml docs/troubleshooting.rst docs/usage-cli.rst tests/cli_tests.py
git commit -m "docs: document optional Playwright render workflow"
```

### Task 9: Add integration test for JS-rendered page fallback

**Files:**
- Create: `tests/render_integration_tests.py`
- Modify: `tests/__init__.py`

**Step 1: Write the failing test**

```python
@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
def test_force_render_extracts_js_injected_content(local_js_server_url):
    html = fetch_url(local_js_server_url, no_ssl=True, options=Extractor(..., render="force"))
    text = extract(html, url=local_js_server_url)
    assert "JS injected article body" in text
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/render_integration_tests.py::test_force_render_extracts_js_injected_content -v`
Expected: FAIL before render path is fully wired end-to-end.

**Step 3: Write minimal implementation**

```python
# local fixture server returns HTML shell + JS that writes article content after load
# test asserts:
# - render=off does not extract target text
# - render=force extracts target text
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/render_integration_tests.py -v`
Expected: PASS (or SKIPPED when Playwright browsers are unavailable).

**Step 5: Commit**

```bash
git add tests/render_integration_tests.py tests/__init__.py
git commit -m "test: add JS-render fallback integration coverage"
```

### Task 10: Final verification and regression safety

**Files:**
- Modify: none (verification only)

**Step 1: Run targeted render-related suite**

Run: `pytest tests/browser_tests.py tests/render_integration_tests.py tests/downloads_tests.py tests/cli_tests.py -v`
Expected: PASS (integration can be SKIPPED if Playwright unavailable).

**Step 2: Run core regression suite**

Run: `pytest tests/downloads_tests.py tests/spider_tests.py tests/sitemaps_tests.py -v`
Expected: PASS, proving default HTTP behavior remains stable.

**Step 3: Run package-level sanity checks**

Run: `python -m trafilatura --help`
Expected: command succeeds and includes render CLI options.

**Step 4: Run one real-world smoke test**

Run: `python -c "from trafilatura import fetch_url; print(len(fetch_url('https://example.org') or ''))"`
Expected: numeric output, no exception.

**Step 5: Commit (if verification-only files changed, otherwise skip)**

```bash
git status
```

If no changes: do not create empty commit.

---

## Notes For Executor

- Keep default mode `render=off` for full backward compatibility.
- Preserve the project stance from docs: rendering is for dynamic content retrieval, not a guaranteed anti-bot bypass.
- Any new config keys must use fallback reads so existing custom `settings.cfg` files keep working.
- Keep browser imports lazy to avoid requiring Playwright when users do not enable render modes.
