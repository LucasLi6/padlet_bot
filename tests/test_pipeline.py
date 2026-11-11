"""Unit + integration tests.

The integration tests drive the bundled mock board (tests/mock_padlet.html) with a
real headless Chromium. They verify the state machine (column discovery, upload,
publish, verification, dry run, security stop) -- not Padlet's real DOM.
"""

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


def test_publishes_requested_count(run_env):
    stats = main.run(MOCK, 3, headless=True, alias="Tester", interval=0.01)
    assert stats.requested == 3
    assert stats.published == 3
    assert stats.failed == 0
    assert stats.uncertain == 0
    assert [r["alias"] for r in stats.rows] == ["Tester"] * 3
    assert len({r["column"] for r in stats.rows}) >= 1


def test_dry_run_uploads_but_does_not_publish(run_env):
    stats = main.run(MOCK, 2, headless=True, dry_run=True, alias="Tester", interval=0.01)
    assert stats.published == 0
    assert [r["status"] for r in stats.rows] == ["dry_run", "dry_run"]


def test_upload_error_is_retried_then_reported(run_env):
    stats = main.run(
        MOCK + "?upload_error=1", 1, headless=True, dry_run=True, alias="Tester", interval=0.01
    )
    assert stats.failed == 1
    assert stats.rows[0]["status"] == "failed"
    assert "upload" in stats.rows[0]["detail"].lower()


def test_security_check_stops_run(run_env):
    with pytest.raises(main.StopRun):
        main.run(SECURITY, 1, headless=True, alias="Tester", interval=0.01)


def test_uncertain_stops_without_resubmitting(run_env, monkeypatch):
    """If a publish cannot be confirmed, the run stops instead of retrying."""

    def fake_publish(page, column, image_path, alias, selectors):
        return main.STATUS_UNCERTAIN

    monkeypatch.setattr(main, "publish_post", fake_publish)
    stats = main.run(MOCK, 5, headless=True, alias="Tester", interval=0.01)
    assert stats.uncertain == 1
    assert len(stats.rows) == 1  # stopped after the first uncertain outcome
    assert stats.published == 0
