"""Unit + integration tests.

The integration tests drive the bundled mock board (tests/mock_padlet.html) with a
real headless Chromium. They verify the state machine (column discovery, upload,
publish, verification, dry run, security stop) -- not Padlet's real DOM.
"""

import json
import re
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

import main  # noqa: E402

HERE = Path(__file__).resolve().parent
MOCK = (HERE / "mock_padlet.html").as_uri()
SECURITY = (HERE / "security.html").as_uri()
SELECTORS = main.load_selectors()


@pytest.fixture
def run_env(images_dir, tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(main, "IMAGES_DIR", images_dir)
    monkeypatch.setattr(main, "LOGS_DIR", logs)
    return images_dir


# --------------------------------------------------------------------------- #
# Pure functions
# --------------------------------------------------------------------------- #


def test_load_images_filters_unsupported_and_empty(tmp_path):
    d = tmp_path / "imgs"
    d.mkdir()
    (d / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
    (d / "b.txt").write_text("not an image")
    (d / "c.jpg").write_bytes(b"")
    (d / "d.WEBP").write_bytes(b"RIFF0000WEBP" + b"0" * 8)
    names = [p.name for p in main.load_images(d)]
    assert names == ["a.png", "d.WEBP"]


def test_load_images_missing_dir_raises(tmp_path):
    with pytest.raises(main.BotError):
        main.load_images(tmp_path / "nope")


def test_load_alias_priority(tmp_path):
    f = tmp_path / "names.txt"
    f.write_text("# comment line\n\n  固定昵称  \nsecond\n", encoding="utf-8")
    assert main.load_alias(f) == "固定昵称"
    assert main.load_alias(f, override="Override") == "Override"
    assert main.load_alias(tmp_path / "missing.txt") == main.DEFAULT_ALIAS


def test_pick_image_avoids_immediate_repeat():
    a, b = Path("a.png"), Path("b.png")
    assert main._pick_image([a], a) == a  # only one option -> allowed
    for _ in range(25):
        assert main._pick_image([a, b], a) == b


def test_pick_column_avoids_immediate_repeat():
    c1 = main.Column(0, "A", None, None)
    c2 = main.Column(1, "B", None, None)
    assert main._pick_column([c1], c1) is c1
    for _ in range(25):
        assert main._pick_column([c1, c2], c1) is c2


# --------------------------------------------------------------------------- #
# Browser integration against the mock board
# --------------------------------------------------------------------------- #


def _page():
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    return pw, browser, page


class _MockNetworkMonitor:
    """Stand-in for the mock board, which has no upload server.

    The mock only renders a local blob preview, so real network evidence can
    never prove success. These tests exercise publish orchestration, so the
    monitor reports the mock asset as server-accepted.
    """

    def __init__(self, page, path):
        self.path = Path(path)
        self.upload_started = True
        self.upload_accepted = True
        self.upload_rejected = None
        self.upload_inflight = 0
        self.process_inflight = 0
        self.process_failed = None
        self.image_load_failed = None
        self.challenge_detected = False
        self.upload_responses = []
        self.process_responses = []
        self.asset_responses = []

    def reset_upload_tracking(self):
        pass

    def fatal_upload_status(self):
        return None

    def close(self):
        pass


@pytest.fixture
def mock_network(monkeypatch):
    monkeypatch.setattr(main, "NetworkMonitor", _MockNetworkMonitor)
    monkeypatch.setattr(main, "_is_server_asset", lambda src: bool(src))


def test_get_columns_discovers_and_titles(run_env):
    pw, browser, page = _page()
    try:
        page.goto(MOCK)
        main.wait_for_board(page, SELECTORS)
        columns = main.get_columns(page, SELECTORS)
        assert {c.title for c in columns} == {"Section 1", "Section 2"}
    finally:
        browser.close()
        pw.stop()


def test_set_guest_alias_verifies(run_env):
    pw, browser, page = _page()
    try:
        page.goto(MOCK)
        main.wait_for_board(page, SELECTORS)
        main.set_guest_alias(page, "测试用户", SELECTORS)
        assert page.locator("#guest").get_attribute("data-alias") == "测试用户"
    finally:
        browser.close()
        pw.stop()


def test_set_guest_alias_skips_when_renaming_is_forbidden(monkeypatch):
    """No 'Change name' entry is not fatal: keep the board's existing name."""

    def no_menu(page, selectors):
        raise main.AliasNotRenamable("menu item missing")

    monkeypatch.setattr(main, "_open_change_name", no_menu)
    main.set_guest_alias(object(), "TestBot", SELECTORS)  # must not raise


def test_publishes_requested_count(run_env, mock_network):
    stats = main.run(MOCK, 3, headless=True, alias="Tester", interval=0.01, profile_dir=None)
    assert stats.requested == 3
    assert stats.published == 3
    assert stats.failed == 0
    assert stats.uncertain == 0
    assert [r["alias"] for r in stats.rows] == ["Tester"] * 3
    assert len({r["column"] for r in stats.rows}) >= 1


def test_dry_run_uploads_but_does_not_publish(run_env, mock_network):
    stats = main.run(
        MOCK, 2, headless=True, dry_run=True, alias="Tester", interval=0.01, profile_dir=None
    )
    assert stats.published == 0
    assert [r["status"] for r in stats.rows] == ["dry_run", "dry_run"]


def test_upload_error_is_retried_then_reported(run_env):
    stats = main.run(
        MOCK + "?upload_error=1",
        1,
        headless=True,
        dry_run=True,
        alias="Tester",
        interval=0.01,
        profile_dir=None,
    )
    assert stats.failed == 1
    assert stats.rows[0]["status"] == "failed"
    assert "upload" in stats.rows[0]["detail"].lower()


def test_security_check_stops_run(run_env):
    """A check the user never clears ends in a stop, not an infinite hang."""
    with pytest.raises(main.StopRun):
        main.run(
            SECURITY, 1, headless=True, alias="Tester", interval=0.01,
            profile_dir=None, security_timeout_ms=500,
        )


def test_security_check_waits_for_the_user(monkeypatch, tmp_path):
    """While the check is visible the run waits; it continues once cleared."""
    calls = {"waits": 0}
    monkeypatch.setattr(
        main, "_first_visible", lambda page, specs: object()
    )

    def fake_wait(page, selectors, timeout_ms=0, label=""):
        calls["waits"] += 1
        monkeypatch.setattr(main, "_first_visible", lambda page, specs: None)

    monkeypatch.setattr(main, "wait_out_security_check", fake_wait)
    monkeypatch.setattr(main, "_add_buttons_present", lambda page: True)
    main.wait_for_board(object(), SELECTORS, timeout_ms=2000)
    assert calls["waits"] == 1


# --------------------------------------------------------------------------- #
# CDP attach: drive the browser the user already opened
# --------------------------------------------------------------------------- #


class _FakeTab:
    def __init__(self, url):
        self.url = url


class _FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self.closed = False
        self.created = 0

    def new_page(self):
        self.created += 1
        tab = _FakeTab("about:blank")
        self.pages.append(tab)
        return tab

    def close(self):
        self.closed = True


class _FakeCdpBrowser:
    def __init__(self, context):
        self.contexts = [context]
        self.closed = False

    def close(self):
        self.closed = True


class _FakeCdpPlaywright:
    def __init__(self, browser):
        self.chromium = self
        self._browser = browser

    def connect_over_cdp(self, url):
        assert url == "http://127.0.0.1:9222"
        return self._browser


def test_same_board_ignores_query_and_fragment():
    board = "https://padlet.com/example-user/test-board"
    assert main._same_board(board + "?share=1", board)
    assert main._same_board(board + "/#x", board)
    assert not main._same_board("https://padlet.com/other/board", board)


def test_pick_cdp_page_prefers_the_open_board_tab():
    board = "https://padlet.com/example-user/test-board"
    ctx = _FakeContext(
        [_FakeTab("https://padlet.com/example-user/other"), _FakeTab(board), _FakeTab("about:blank")]
    )
    assert main._pick_cdp_page(ctx, board).url == board


def test_pick_cdp_page_falls_back_to_any_padlet_tab():
    ctx = _FakeContext([_FakeTab("https://example.com"), _FakeTab("https://padlet.com/home")])
    assert main._pick_cdp_page(ctx, "https://padlet.com/example-user/test-board").url == (
        "https://padlet.com/home"
    )


def test_cdp_session_never_closes_the_users_browser():
    board = "https://padlet.com/example-user/test-board"
    ctx = _FakeContext([_FakeTab(board)])
    browser = _FakeCdpBrowser(ctx)
    session = main.connect_browser(_FakeCdpPlaywright(browser), "http://127.0.0.1:9222", board)
    assert session.owned is False
    assert session.page.url == board
    session.close()
    assert ctx.closed is False
    assert browser.closed is False


def test_uncertain_stops_without_resubmitting(run_env, monkeypatch):
    """If a publish cannot be confirmed, the run stops instead of retrying."""

    def fake_publish(page, column, image_path, alias, selectors, **kwargs):
        return main.STATUS_UNCERTAIN

    monkeypatch.setattr(main, "publish_post", fake_publish)
    stats = main.run(MOCK, 5, headless=True, alias="Tester", interval=0.01, profile_dir=None)
    assert stats.uncertain == 1
    assert len(stats.rows) == 1  # stopped after the first uncertain outcome
    assert stats.published == 0


# --------------------------------------------------------------------------- #
# Upload diagnostics: sanitisation, 4xx vs requestfailed, state machine
# --------------------------------------------------------------------------- #


class _FakePage:
    """Minimal stand-in for Playwright's Page used by NetworkMonitor."""

    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def wait_for_timeout(self, ms):
        time.sleep(min(ms, 10) / 1000)


class _FakeRequest:
    def __init__(self, url, method="POST", resource_type="fetch", failure=None):
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.failure = failure


class _FakeResponse:
    def __init__(self, request, status=200, headers=None, body=""):
        self.request = request
        self.url = request.url
        self.status = status
        self.headers = headers or {}
        self._body = body

    def text(self):
        return self._body


class _FakeScope:
    def __init__(self, src, uploading=False, pct=None):
        self._ui = {
            "src": src,
            "natural": [100, 100] if src else None,
            "uploading": uploading,
            "bars": [],
            "pct": pct,
        }

    def evaluate(self, _js):
        return dict(self._ui)


def test_upload_error_text_matches_picture_loading_failures():
    pattern = re.compile(main.DEFAULT_SELECTORS["upload_error_text"][0]["value"], re.I)
    for text in (
        "Picture loading failed",
        "picture loading fail",
        "Failed to load picture",
        "Failed to load the image",
        "Image processing failed",
        "Sorry, there's an error when uploading the file",
    ):
        assert pattern.search(text), text


def test_sanitize_url_drops_query_and_fragment():
    got = main._sanitize_url(
        "https://uploads-usc1.padletcdn.com/media/abc?token=SECRET&sig=XYZ#frag"
    )
    assert got == "https://uploads-usc1.padletcdn.com/media/abc"
    assert "SECRET" not in got


def test_redact_headers_hides_credentials():
    out = main._redact_headers(
        {
            "Cookie": "a=1",
            "Authorization": "Bearer x",
            "Set-Cookie": "b=2",
            "Content-Type": "application/json",
        }
    )
    assert out["Cookie"] == "[redacted]"
    assert out["Authorization"] == "[redacted]"
    assert out["Set-Cookie"] == "[redacted]"
    assert out["Content-Type"] == "application/json"


def test_network_monitor_records_4xx_as_response_not_requestfailed(tmp_path):
    page = _FakePage()
    monitor = main.NetworkMonitor(page, tmp_path / "net.jsonl")
    request = _FakeRequest("https://uploads-usc1.padletcdn.com/media/abc?token=SECRET")
    page.handlers["request"](request)
    page.handlers["response"](
        _FakeResponse(
            request,
            status=413,
            headers={"Set-Cookie": "s=1", "Content-Type": "application/json"},
            body='{"error":"file too large"}',
        )
    )
    monitor.close()

    text = (tmp_path / "net.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in text.splitlines()]
    assert [r["kind"] for r in records] == ["request", "response"]
    assert records[1]["status"] == 413
    assert monitor.upload_rejected == 413
    assert monitor.upload_accepted is False
    assert "SECRET" not in text
    assert records[1]["headers"]["Set-Cookie"] == "[redacted]"
    assert records[1]["body"] == '{"error":"file too large"}'


def test_network_monitor_records_transport_failure_separately(tmp_path):
    page = _FakePage()
    monitor = main.NetworkMonitor(page, tmp_path / "net.jsonl")
    request = _FakeRequest(
        "https://uploads-usc1.padletcdn.com/media/abc",
        failure="net::ERR_CONNECTION_RESET",
    )
    page.handlers["request"](request)
    page.handlers["requestfailed"](request)
    monitor.close()

    records = [
        json.loads(line)
        for line in (tmp_path / "net.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [r["kind"] for r in records] == ["request", "requestfailed"]
    assert "status" not in records[1]
    assert monitor.upload_rejected == "requestfailed: net::ERR_CONNECTION_RESET"


def test_is_server_asset_rejects_local_previews():
    assert not main._is_server_asset("blob:https://padlet.com/123")
    assert not main._is_server_asset("data:image/png;base64,AAAA")
    assert not main._is_server_asset(None)
    assert main._is_server_asset("https://v1.padlet.pics/1/image.webp")


def test_track_upload_refuses_ready_without_server_acceptance(monkeypatch, tmp_path):
    """A rendered preview alone must not be reported as READY."""
    monkeypatch.setattr(main, "_first_visible", lambda *args, **kwargs: None)
    monitor = main.NetworkMonitor(_FakePage(), tmp_path / "net.jsonl")
    monitor.upload_started = True  # request seen, but no 2xx response yet
    scope = _FakeScope("https://v1.padlet.pics/1/image.webp")
    state, _reason, _history = main.track_upload_state(
        _FakePage(), scope, monitor, main.DEFAULT_SELECTORS, timeout_s=0.1
    )
    monitor.close()
    assert state == "UNKNOWN"


def test_track_upload_reaches_ready_with_server_accepted_asset(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_first_visible", lambda *args, **kwargs: None)
    monitor = main.NetworkMonitor(_FakePage(), tmp_path / "net.jsonl")
    monitor.upload_started = True
    monitor.upload_accepted = True
    monitor.upload_inflight = 0
    scope = _FakeScope("https://v1.padlet.pics/1/image.webp")
    state, _reason, _history = main.track_upload_state(
        _FakePage(), scope, monitor, main.DEFAULT_SELECTORS, timeout_s=1.0
    )
    monitor.close()
    assert state == "READY"


def test_track_upload_fails_on_http_error(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_first_visible", lambda *args, **kwargs: None)
    monitor = main.NetworkMonitor(_FakePage(), tmp_path / "net.jsonl")
    monitor.upload_started = True
    monitor.upload_rejected = 500
    scope = _FakeScope(None)
    state, reason, _history = main.track_upload_state(
        _FakePage(), scope, monitor, main.DEFAULT_SELECTORS, timeout_s=1.0
    )
    monitor.close()
    assert state == "FAILED"
    assert "500" in reason


def test_http_403_stops_the_run_instead_of_retrying():
    with pytest.raises(main.StopRun):
        main._raise_for_upload_status("image post-processing", 403)
    with pytest.raises(main.StopRun):
        main._raise_for_upload_status("file transfer", 429)


def test_retryable_and_non_retryable_statuses():
    with pytest.raises(main.UploadError) as server_error:
        main._raise_for_upload_status("file transfer", 500)
    assert server_error.value.retryable is True

    with pytest.raises(main.UploadError) as client_error:
        main._raise_for_upload_status("file transfer", 413)
    assert client_error.value.retryable is False

    with pytest.raises(main.UploadError) as transport_error:
        main._raise_for_upload_status("file transfer", "requestfailed: net::ERR_RESET")
    assert transport_error.value.retryable is True


def test_diagnose_upload_reports_unknown_without_network_evidence(run_env, images_dir, capsys):
    """On the mock board there is no server, so the honest result is UNKNOWN."""
    image = sorted(images_dir.glob("*.png"))[0]
    code = main.diagnose_upload(
        MOCK, str(image), headless=True, timeout_s=1.0, profile_dir=None
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "Upload selected:" in out
    assert "Final status: UNKNOWN" in out
    assert "Reason:" in out
    assert list(main.LOGS_DIR.glob("upload_network_*.jsonl"))
    assert list(main.LOGS_DIR.glob("upload_debug_*.txt"))
