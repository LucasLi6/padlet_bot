#!/usr/bin/env python3
"""Padlet bulk image poster (single fixed guest alias).

What this does
--------------
Opens a Padlet board in a real Chromium window, enters a post editor in a chosen
column, attaches one image, and publishes. It repeats this ``--count`` times,
randomising only the image and the target column. The guest alias is set once
at the start and never changed during the run.

What this deliberately does NOT do
----------------------------------
* No identity rotation: the alias is fixed for the entire run.
* No attempt to defeat captchas, security checks, login walls or rate limits.
  On any such signal the run aborts.
* No retry of a publish whose outcome is unknown (avoids duplicate posts).

Verifying the selectors
-----------------------
Padlet's live DOM cannot be verified from an offline checkout. Run::

    python main.py --url <board> --diagnose

which dumps a screenshot, the DOM, and an inventory of interactive elements to
``logs/``. If the built-in heuristics miss, copy the real selectors into
``selectors.json`` (see ``DEFAULT_SELECTORS`` for the shape) and rerun.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from playwright.sync_api import (
    Error as PWError,
    Locator,
    Page,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parent
IMAGES_DIR = ROOT / "images"
NAMES_FILE = ROOT / "names.txt"
LOGS_DIR = ROOT / "logs"
SELECTORS_FILE = ROOT / "selectors.json"
# Dedicated Chrome profile (cookies + Cloudflare clearance). Never reuse the
# user's daily Chrome profile: Playwright warns it can break browser startup.
CHROME_PROFILE_DIR = ROOT / "chrome_profile"

SUPPORTED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_ALIAS = "TestBot"
MIN_INTERVAL_S = 5.0
CONFIRM_THRESHOLD = 20

# How long to wait for a human to clear a security check. 0 = wait forever
# (the run stays paused and the browser window stays open). Tests override it.
SECURITY_WAIT_MS = 0

STATUS_PUBLISHED = "published"
STATUS_PENDING = "pending_moderation"
STATUS_FAILED = "failed"
STATUS_UNCERTAIN = "uncertain"
STATUS_DRY_RUN = "dry_run"


# --------------------------------------------------------------------------- #
# Selector configuration
# --------------------------------------------------------------------------- #
# Each key is an ordered list of candidate specs. A spec is one of:
#   {"kind": "css",   "value": "<css selector>"}
#   {"kind": "text",  "value": "<text or regex>"}
#   {"kind": "role",  "role": "button", "name": "<regex>"}
#   {"kind": "placeholder", "value": "<regex>"}
# The first candidate that resolves to a *visible* element wins.
# Override any key in selectors.json (same shape) when diagnosing the real board.

DEFAULT_SELECTORS: dict[str, list[dict[str, Any]]] = {
    # Verified against the live board on 2026-11-11: the anonymous visitor avatar
    # is the top-right circular image button labelled "Go to sign up page".
    "guest_avatar": [
        {"kind": "css", "value": "button[aria-label='Go to sign up page']"},
        {"kind": "css", "value": "button:has(img.rounded-full)"},
        {"kind": "role", "role": "button", "name": r"guest|your avatar|profile|account|avatar"},
    ],
    "guest_menu_item_change_name": [
        {"kind": "role", "role": "button", "name": r"^\s*change name\s*$"},
        {"kind": "text", "value": r"^\s*change name\s*$"},
        {"kind": "role", "role": "menuitem", "name": r"change name"},
    ],
    "name_input": [
        {"kind": "css", "value": "input[type='text']"},
        {"kind": "css", "value": "input[name='name'], input[aria-label*='name' i]"},
    ],
    "name_save": [
        {"kind": "role", "role": "button", "name": r"^\s*(done|save|ok|apply|update)\s*$"},
        {"kind": "css", "value": "button[type='submit']"},
    ],
    # Composer (verified): subject is a textarea, body is a ProseMirror textbox.
    "editor": [
        {"kind": "css", "value": "textarea[aria-label='Post subject']"},
        {"kind": "css", "value": "[role='dialog']:visible textarea, [role='dialog']:visible [contenteditable='true']"},
        {"kind": "css", "value": "textarea:visible, [contenteditable='true']:visible"},
    ],
    "composer": [
        {"kind": "xpath", "value": "//textarea[@aria-label='Post subject']/ancestor::*[.//button[@aria-label='Submit post']][1]"},
        {"kind": "css", "value": "[role='dialog']:visible"},
    ],
    "image_button": [
        {"kind": "role", "role": "button", "name": r"^\s*upload\s*$"},
        {"kind": "role", "role": "button", "name": r"image|photo|picture|media|attach"},
    ],
    "file_input": [
        {"kind": "css", "value": "input[type='file']:not([accept*='srt'])"},
    ],
    "publish_button": [
        {"kind": "role", "role": "button", "name": r"submit post|^\s*submit\s*$"},
        {"kind": "role", "role": "button", "name": r"^\s*(publish|post|submit|share)\s*$"},
    ],
    "cancel_button": [
        {"kind": "role", "role": "button", "name": r"^\s*close post\s*$"},
        {"kind": "role", "role": "button", "name": r"^\s*(cancel|discard|close)\s*$"},
    ],
    "moderation_text": [
        {"kind": "text", "value": r"(pending|awaiting (approval|review)|under review|needs approval)"},
    ],
    "error_text": [
        {"kind": "text", "value": r"(something went wrong|failed to|could not|not allowed|try again|error)"},
    ],
    "upload_error_text": [
        {
            "kind": "text",
            "value": (
                r"(error (when |while )?uploading|failed to upload|couldn'?t upload"
                r"|could not upload|picture loading fail(ed)?|failed to load (the )?(picture|image)"
                r"|image (processing|upload) (failed|error)|sorry,? there.?s an? error)"
            ),
        },
    ],
    "security_text": [
        {"kind": "text", "value": r"(security check|verify you are human|just a moment|are you a robot|access denied)"},
    ],
    "login_wall": [
        {"kind": "role", "role": "button", "name": r"^\s*(log in|sign in|sign up)\s*$"},
    ],
    "post_card": [
        {"kind": "css", "value": "[data-post-id], article, [class*='postCard' i], [class*='post-card' i]"},
    ],
}


def load_selectors() -> dict[str, list[dict[str, Any]]]:
    """Merge selectors.json over the defaults (per key replacement)."""
    merged = {k: list(v) for k, v in DEFAULT_SELECTORS.items()}
    if SELECTORS_FILE.exists():
        try:
            overlay = json.loads(SELECTORS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BotError(f"selectors.json is not valid JSON: {exc}") from exc
        for key, specs in overlay.items():
            merged[key] = specs
    return merged


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class BotError(Exception):
    """Recoverable, user-facing automation error."""


class SelectorError(BotError):
    """A required element could not be located; carries a diagnostic hint."""


class UploadError(BotError):
    """Upload failed. ``retryable`` says whether another attempt can help."""

    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class StopRun(Exception):
    """A condition that must abort the whole run (captcha, rate limit, ...)."""


class AliasNotRenamable(BotError):
    """The visitor menu has no 'Change name' entry; keep the existing name."""


@dataclass
class BrowserSession:
    """A launched browser plus the page to drive, and how to close both.

    ``owned=False`` means the browser was started by the user (CDP attach):
    closing it would kill their window, so only the connection is dropped.
    """

    context: Any
    page: Page
    browser: Any = None
    owned: bool = True

    def close(self) -> None:
        if not self.owned:
            return
        try:
            self.context.close()
        except PWError:
            pass
        if self.browser is not None:
            try:
                self.browser.close()
            except PWError:
                pass


def _same_board(a: Optional[str], b: str) -> bool:
    """True when two URLs point at the same board (query/fragment ignored)."""
    if not a:
        return False
    pa, pb = urlparse(a), urlparse(b)
    return pa.netloc.lower() == pb.netloc.lower() and pa.path.rstrip("/") == pb.path.rstrip("/")


def _pick_cdp_page(context: Any, url: str) -> Page:
    """Pick the user's already-open board tab, or the first Padlet tab."""
    pages = list(context.pages)
    for page in pages:
        try:
            if _same_board(page.url, url):
                return page
        except PWError:
            continue
    for page in pages:
        try:
            if "padlet.com" in (page.url or ""):
                return page
        except PWError:
            continue
    if pages:
        return pages[0]
    return context.new_page()


def connect_browser(pw: Any, cdp_url: str, url: str) -> BrowserSession:
    """Attach to a Chrome the user started with --remote-debugging-port.

    This is the recommended mode when a human-verification challenge appears:
    the user opens Padlet and clears the check in that window, and the script
    drives the same tab. The browser is never closed by the script.
    """
    try:
        browser = pw.chromium.connect_over_cdp(cdp_url)
    except PWError as exc:
        raise BotError(
            f"Could not connect to Chrome at {cdp_url}: {exc}. Start it first with: "
            '"<path to Google Chrome>" --remote-debugging-port=9222 '
            '--user-data-dir="$HOME/chrome-padlet-debug"'
        ) from exc
    if not browser.contexts:
        raise BotError(
            f"Connected to {cdp_url}, but it has no browser context/page. "
            "Open a Chrome window (not just the debug port) and try again."
        )
    context = browser.contexts[0]
    page = _pick_cdp_page(context, url)
    logging.info("Attached via CDP to %s; using tab: %s", cdp_url, page.url)
    return BrowserSession(context=context, page=page, browser=browser, owned=False)


def launch_browser(
    pw: Any,
    headless: bool,
    profile_dir: Optional[Path] = CHROME_PROFILE_DIR,
    cdp_url: Optional[str] = None,
    url: str = "",
) -> BrowserSession:
    """Launch Google Chrome with a persistent profile, or attach over CDP.

    ``channel="chrome"`` uses the Google Chrome installed on this machine, not
    Playwright's bundled Chromium. ``user_data_dir`` persists cookies and any
    completed Cloudflare/security check between runs. Pass ``profile_dir=None``
    to use bundled Chromium (the test suite does this to stay isolated).

    When ``cdp_url`` is given, the user's already-running Chrome is used and is
    left open when the run ends.
    """
    if cdp_url:
        return connect_browser(pw, cdp_url, url)

    if profile_dir is not None:
        profile = Path(profile_dir).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                channel="chrome",
                headless=headless,
            )
        except PWError as exc:
            raise BotError(
                f"Could not launch Google Chrome with profile {profile}: {exc}. "
                "Close other windows using that profile, or run without --chrome-profile."
            ) from exc
        page = context.pages[0] if context.pages else context.new_page()
        return BrowserSession(context=context, page=page)

    browser = pw.chromium.launch(headless=headless)
    context = browser.new_context()
    return BrowserSession(context=context, page=context.new_page(), browser=browser)


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #


def load_images(images_dir: Optional[Path] = None) -> list[Path]:
    """Return readable, supported image files. Unreadable files are skipped."""
    images_dir = images_dir or IMAGES_DIR
    if not images_dir.is_dir():
        raise BotError(f"Image directory not found: {images_dir}")
    found: list[Path] = []
    for path in sorted(images_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_IMAGE_EXT:
            continue
        try:
            with path.open("rb") as fh:
                head = fh.read(16)
        except OSError as exc:
            logging.warning("Skipping unreadable image %s: %s", path.name, exc)
            continue
        if len(head) < 8:
            logging.warning("Skipping empty/truncated image %s", path.name)
            continue
        found.append(path)
    if not found:
        raise BotError(
            f"No usable images in {images_dir} (supported: {', '.join(sorted(SUPPORTED_IMAGE_EXT))})."
        )
    return found


def load_alias(names_file: Optional[Path] = None, override: Optional[str] = None) -> str:
    """Resolve the *single fixed* guest alias for the run.

    Priority: --alias flag > first non-empty line of names.txt > DEFAULT_ALIAS.
    Duplicate handling in the old design is gone: one alias, one identity.
    """
    if override:
        return override.strip()
    names_file = names_file or NAMES_FILE
    if names_file.exists():
        for line in names_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return stripped
    return DEFAULT_ALIAS


# --------------------------------------------------------------------------- #
# Locator resolution
# --------------------------------------------------------------------------- #


def _spec_to_locator(root: Any, spec: dict[str, Any]) -> Locator:
    kind = spec["kind"]
    if kind == "css":
        return root.locator(spec["value"])
    if kind == "xpath":
        return root.locator(f"xpath={spec['value']}")
    if kind == "text":
        return root.get_by_text(re.compile(spec["value"], re.I))
    if kind == "role":
        return root.get_by_role(spec["role"], name=re.compile(spec["name"], re.I))
    if kind == "placeholder":
        return root.get_by_placeholder(re.compile(spec["value"], re.I))
    raise ValueError(f"Unknown selector kind: {kind}")


def _first_visible(root: Any, specs: list[dict[str, Any]]) -> Optional[Locator]:
    for spec in specs:
        try:
            loc = _spec_to_locator(root, spec)
            count = loc.count()
        except PWError:
            continue
        for i in range(min(count, 12)):
            candidate = loc.nth(i)
            try:
                if candidate.is_visible():
                    return candidate
            except PWError:
                continue
    return None


def resolve(
    page: Page,
    key: str,
    selectors: dict[str, list[dict[str, Any]]],
    root: Any = None,
    timeout_ms: int = 8000,
) -> Locator:
    """Poll until a visible element matching ``key`` appears, else raise."""
    root = root or page
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        loc = _first_visible(root, selectors[key])
        if loc is not None:
            return loc
        page.wait_for_timeout(150)
    raise SelectorError(f"Could not locate a visible element for '{key}'.")


def exists(page: Page, key: str, selectors: dict[str, list[dict[str, Any]]], root: Any = None) -> bool:
    return _first_visible(root or page, selectors[key]) is not None


# --------------------------------------------------------------------------- #
# Page readiness / safety gates
# --------------------------------------------------------------------------- #


def wait_out_security_check(
    page: Page,
    selectors: dict[str, list[dict[str, Any]]],
    timeout_ms: int = SECURITY_WAIT_MS,
    label: str = "board",
) -> None:
    """Pause automation until a visible security check has been cleared.

    The browser window stays open the whole time so the user can complete the
    check by hand. ``timeout_ms=0`` (the default) waits indefinitely.
    """
    if _first_visible(page, selectors["security_text"]) is None:
        return

    logging.warning("Security check detected (%s); waiting for manual completion.", label)
    print()
    print("=" * 70)
    print("检测到安全验证（Cloudflare / captcha）。")
    print("请在浏览器窗口中手动完成验证；脚本会暂停等待，完成后自动继续。")
    print("请不要关闭窗口。")
    print("=" * 70, flush=True)

    started = time.monotonic()
    while True:
        try:
            page.wait_for_timeout(1000)
        except PWError as exc:
            raise BotError(
                f"Browser was closed while waiting for the security check: {exc}"
            ) from exc
        if _first_visible(page, selectors["security_text"]) is None:
            logging.info("Security check cleared; continuing.")
            print("安全验证已通过，继续执行。", flush=True)
            return
        if timeout_ms and (time.monotonic() - started) * 1000 >= timeout_ms:
            raise StopRun(
                f"Security check was not completed within {timeout_ms // 1000}s; stopping."
            )


def wait_for_board(
    page: Page,
    selectors: dict[str, list[dict[str, Any]]],
    timeout_ms: int = 30_000,
    security_timeout_ms: int = SECURITY_WAIT_MS,
) -> None:
    """Wait until the board UI is interactive; pause while a human clears a check."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if exists(page, "security_text", selectors):
            wait_out_security_check(page, selectors, security_timeout_ms, label="board load")
            # Give the board its full startup window after the check clears.
            deadline = time.monotonic() + timeout_ms / 1000
        if exists(page, "guest_avatar", selectors) or _add_buttons_present(page):
            return
        page.wait_for_timeout(300)
    raise SelectorError(
        "Board did not become interactive. Re-run with --diagnose to capture the DOM."
    )


def check_not_logged_in(page: Page, selectors: dict[str, list[dict[str, Any]]]) -> None:
    """Guest posting needs an anonymous session. Fail loudly if we look logged in."""
    if exists(page, "guest_avatar", selectors):
        return
    if exists(page, "login_wall", selectors):
        raise BotError(
            "No guest avatar found, only a Log in/Sign up control. The board may "
            "require an account; guest posting is unavailable."
        )
    raise BotError("Could not find the guest avatar. Guest alias cannot be set.")


def _scan_add_buttons(page: Page) -> list[dict[str, Any]]:
    return page.evaluate(_SCAN_BUTTONS_JS)


def _add_buttons_present(page: Page) -> bool:
    return bool(_scan_add_buttons(page))


# --------------------------------------------------------------------------- #
# Guest alias
# --------------------------------------------------------------------------- #


def _open_change_name(page: Page, selectors: dict[str, list[dict[str, Any]]]) -> tuple[Locator, Locator]:
    """Click the visitor avatar, choose 'Change name', return (avatar, input)."""
    avatar = resolve(page, "guest_avatar", selectors)
    avatar.click()
    try:
        change = resolve(page, "guest_menu_item_change_name", selectors, timeout_ms=5000)
    except SelectorError as exc:
        page.keyboard.press("Escape")
        raise AliasNotRenamable(
            "'Change name' was not available in the visitor menu. The board may "
            "forbid anonymous renaming, or this is not a guest session."
        ) from exc
    change.click()
    field = resolve(page, "name_input", selectors, timeout_ms=5000)
    return avatar, field


def set_guest_alias(page: Page, name: str, selectors: dict[str, list[dict[str, Any]]]) -> None:
    """Set the fixed guest alias once and verify it persisted.

    If the board does not offer 'Change name', the run continues with whatever
    guest name the board already assigned instead of aborting: the board just
    needs one consistent name, not necessarily ours.

    Verification reopens the 'Change name' dialog and reads the stored value --
    stronger than checking the avatar, since Padlet renders the avatar as an
    image with no text.
    """
    if not name.strip():
        raise BotError("Alias must not be empty.")

    try:
        avatar, field = _open_change_name(page, selectors)
    except AliasNotRenamable as exc:
        logging.warning("Skipping alias change: %s", exc)
        print(
            "访客菜单里没有 'Change name'，跳过改名，沿用当前昵称继续。",
            flush=True,
        )
        return
    field.fill(name)
    resolve(page, "name_save", selectors, timeout_ms=5000).click()

    # Wait for the dialog to close (name input no longer visible).
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and field.is_visible():
        page.wait_for_timeout(150)

    verify_field: Optional[Locator] = None
    try:
        _, verify_field = _open_change_name(page, selectors)
        stored = verify_field.input_value().strip()
    except (BotError, SelectorError) as exc:
        raise BotError(f"Alias was submitted but could not be re-opened to verify: {exc}") from exc
    finally:
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        if verify_field is not None:
            try:
                if verify_field.is_visible():
                    resolve(page, "name_save", selectors, timeout_ms=2000).click()
            except (BotError, PWError):
                logging.debug("Could not close the alias dialog; continuing.")

    if stored != name:
        raise BotError(f"Alias did not persist: expected '{name}', dialog shows '{stored}'.")
    logging.info("Guest alias set and verified: %s", name)


# --------------------------------------------------------------------------- #
# Columns
# --------------------------------------------------------------------------- #

_SCAN_BUTTONS_JS = r"""
() => {
  // Verified on the live board: each column exposes "Add post to this section".
  const out = [];
  const buttons = Array.from(document.querySelectorAll(
    'button[aria-label="Add post to this section"], button[aria-label*="Add post" i]'
  )).filter(b => b.offsetWidth > 0 && b.offsetHeight > 0 && !b.disabled);

  for (const b of buttons) {
    const container = b.closest(
      '[data-section-id], section, [class*="section" i], [role="region"]');
    let title = '';
    if (container) {
      const h = container.querySelector('h1,h2,h3,h4,h5,h6,[class*="title" i]');
      if (h) title = (h.innerText || '').trim().split('\n')[0];
    }
    const idx = out.length;
    b.setAttribute('data-pb-add', String(idx));
    if (container) container.setAttribute('data-pb-col', String(idx));
    out.push({index: idx, title: title || `(column ${idx + 1})`});
  }

  // Fallback: a board with no visible sections still has a global post button.
  if (out.length === 0) {
    const g = document.querySelector('button[aria-label="Start writing a new post"]');
    if (g && g.offsetWidth > 0) {
      g.setAttribute('data-pb-add', '0');
      out.push({index: 0, title: '(default column)'});
    }
  }
  return out;
}
"""


@dataclass
class Column:
    index: int
    title: str
    add_button: Locator
    container: Optional[Locator]
    baseline_posts: Optional[int] = None


def get_columns(page: Page, selectors: dict[str, list[dict[str, Any]]]) -> list[Column]:
    """Tag and return every column that has a usable add-post button.

    Sections render asynchronously after the board shell, so poll briefly
    instead of failing on the first scan -- otherwise a run can start before
    the first column exists.
    """
    deadline = time.monotonic() + 10
    while True:
        infos = _scan_add_buttons(page)
        columns: list[Column] = []
        for info in infos:
            idx = info["index"]
            add_button = page.locator(f'[data-pb-add="{idx}"]')
            if add_button.count() == 0 or not add_button.first.is_visible():
                continue
            container = page.locator(f'[data-pb-col="{idx}"]')
            container_loc = container.first if container.count() else None
            col = Column(index=idx, title=info["title"], add_button=add_button.first, container=container_loc)
            if container_loc is not None:
                col.baseline_posts = _count_posts(container_loc, selectors)
            columns.append(col)

        if columns or time.monotonic() >= deadline:
            break
        page.wait_for_timeout(300)

    if not columns:
        raise SelectorError(
            "No columns with an add-post button were found. Open the board with "
            "--diagnose to inspect available controls."
        )
    logging.info("Detected %d postable column(s): %s", len(columns), [c.title for c in columns])
    return columns


def _count_posts(container: Locator, selectors: dict[str, list[dict[str, Any]]]) -> Optional[int]:
    for spec in selectors["post_card"]:
        try:
            loc = _spec_to_locator(container, spec)
            return loc.count()
        except PWError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Editor / upload / publish
# --------------------------------------------------------------------------- #


def open_editor(page: Page, column: Column, selectors: dict[str, list[dict[str, Any]]]) -> Locator:
    """Open the composer for ``column`` and return the composer container."""
    column.add_button.scroll_into_view_if_needed()
    column.add_button.click()
    resolve(page, "editor", selectors, timeout_ms=12_000)
    return resolve(page, "composer", selectors, timeout_ms=6_000)


def _first_present(root: Any, specs: list[dict[str, Any]]) -> Optional[Locator]:
    for spec in specs:
        try:
            loc = _spec_to_locator(root, spec)
            if loc.count():
                return loc.first
        except PWError:
            continue
    return None


def _attach_file(page: Page, composer: Locator, image_path: Path, selectors: dict[str, list[dict[str, Any]]]) -> None:
    """Hand the file to Padlet via the Upload button, or a hidden file input."""
    try:
        button = resolve(page, "image_button", selectors, root=composer, timeout_ms=6_000)
        with page.expect_file_chooser(timeout=10_000) as chooser_info:
            button.click()
        chooser_info.value.set_files(str(image_path))
    except (SelectorError, PWTimeoutError):
        direct = _first_present(composer, selectors["file_input"]) or _first_present(page, selectors["file_input"])
        if direct is None:
            raise BotError("No upload control (Upload button or file input) found in the composer.")
        direct.set_input_files(str(image_path))


def upload_image(
    page: Page,
    image_path: Path,
    selectors: dict[str, list[dict[str, Any]]],
    monitor: Optional["NetworkMonitor"] = None,
    security_timeout_ms: int = SECURITY_WAIT_MS,
) -> Optional[str]:
    """Attach ``image_path``; return the preview src.

    With a network monitor, success requires the bytes transfer *and* Padlet's
    post-processing request to succeed, and the result is read from the network,
    not just from the presence of a preview. Retryable failures are retried (up
    to 3 attempts); HTTP 401/403/429 stops the run instead of retrying.
    """
    last_error: Optional[Exception] = None
    attempts = 0
    for attempt in range(1, 4):
        if attempt > 1:
            # A failed attempt leaves its toast on screen. Clear it first, otherwise
            # the next wait_upload_complete would read the stale message and abort
            # before the new upload has a chance.
            _wait_upload_error_gone(page, selectors)
            page.wait_for_timeout(2000 * (attempt - 1))
        composer = resolve(page, "composer", selectors, timeout_ms=8_000)
        if monitor is not None:
            monitor.reset_upload_tracking()
        attempts = attempt
        try:
            _attach_file(page, composer, image_path, selectors)
            return wait_upload_complete(
                page, composer, image_path, selectors, monitor=monitor,
                security_timeout_ms=security_timeout_ms,
            )
        except UploadError as exc:
            last_error = exc
            logging.warning("Upload attempt %d/3 failed: %s", attempt, exc)
            if not exc.retryable:
                break
    raise BotError(f"Upload failed after {attempts} attempt(s): {last_error}")


def _raise_for_upload_status(stage: str, status: Any) -> None:
    """Turn a network outcome into the right exception class."""
    if isinstance(status, int) and status in (401, 403, 429):
        raise StopRun(
            f"Padlet refused the {stage} request with HTTP {status}. "
            "Stopping; not retrying or bypassing the restriction."
        )
    retryable = not isinstance(status, int) or status >= 500 or status in (408, 409, 425)
    raise UploadError(f"{stage} failed: {status}", retryable=retryable)


def _wait_upload_error_gone(page: Page, selectors: dict[str, list[dict[str, Any]]], timeout_ms: int = 10_000) -> None:
    """Wait for Padlet's upload-error toast to disappear (it auto-hides)."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if _first_visible(page, selectors["upload_error_text"]) is None:
            return
        page.wait_for_timeout(300)
    logging.debug("Upload-error toast did not clear within %dms; continuing anyway.", timeout_ms)


_PREVIEW_SRC_JS = r"""
(root) => {
  let best = null;
  for (const im of root.querySelectorAll('img')) {
    if (im.naturalWidth <= 0 || im.offsetWidth < 40 || im.offsetHeight < 40) continue;
    const area = im.offsetWidth * im.offsetHeight;
    if (!best || area > best.area) best = {area, src: im.src};
  }
  return best ? best.src : null;
}
"""

_UPLOADING_JS = r"""
(root) => /(uploading|processing|please wait)/i.test(root.innerText || '')
"""


def _announce_challenge(stage: str) -> None:
    """Tell the user a Cloudflare challenge is blocking us and we are waiting."""
    logging.warning("Cloudflare challenge on the %s request; pausing for manual completion.", stage)
    print()
    print("!" * 70)
    print("Cloudflare 拦截了上传链路（403 challenge），这不是文件问题。")
    print("请在浏览器窗口中完成验证（若有验证框请手动点一下）。")
    print("脚本会暂停等待并自动重试，不会关闭窗口。")
    print("!" * 70, flush=True)


def _wait_upload_complete_networked(
    page: Page,
    scope: Any,
    image_path: Path,
    selectors: dict[str, list[dict[str, Any]]],
    timeout_ms: int,
    monitor: "NetworkMonitor",
    security_timeout_ms: int = SECURITY_WAIT_MS,
) -> str:
    """Evidence-based upload completion check.

    Stages (network evidence): transfer 2xx -> post-processing 2xx -> processed
    asset 2xx. A UI toast alone is not sufficient to declare failure while one
    of those requests is still in flight; a toast plus a rendered server preview
    is success (Padlet occasionally shows the toast after storing the file).

    A security check or a Cloudflare challenge pauses the wait so the user can
    clear it in the visible browser; the window is never closed on the user.
    """
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    toast_since: Optional[float] = None
    toast_message = ""
    challenge_notices = 0

    while time.monotonic() < deadline:
        if _first_visible(page, selectors["security_text"]) is not None:
            wait_out_security_check(page, selectors, security_timeout_ms, label="upload")
            # The check can take a while; give the upload a fair chance after it.
            deadline = max(deadline, time.monotonic() + 20)
            toast_since = None

        fatal = monitor.fatal_upload_status()
        if fatal is not None:
            if monitor.challenge_detected:
                # A challenge is human-solvable, unlike a plain 403. Pause,
                # let the user clear it, then give Padlet's client a retry.
                if challenge_notices < 3:
                    challenge_notices += 1
                    _announce_challenge(fatal[0])
                    wait_out_security_check(
                        page, selectors, security_timeout_ms, label="upload (Cloudflare)"
                    )
                    page.wait_for_timeout(1500)
                    monitor.clear_challenge_failure()
                    toast_since = None
                    deadline = max(deadline, time.monotonic() + 20)
                    continue
                raise UploadError(
                    f"Cloudflare challenge kept blocking the {fatal[0]} request "
                    f"({fatal[1]}); not bypassing it.",
                    retryable=True,
                )
            _raise_for_upload_status(*fatal)

        try:
            src = scope.evaluate(_PREVIEW_SRC_JS)
            uploading = scope.evaluate(_UPLOADING_JS)
        except PWError:
            src, uploading = None, False

        # Success requires: a rendered preview, no spinner, the server accepted
        # the bytes, and the preview URL is served by Padlet (not a blob/data
        # URL from the local file).
        if src and not uploading and monitor.upload_accepted and _is_server_asset(src):
            if toast_since is not None:
                logging.warning(
                    "Padlet showed an upload-error toast, but the transfer and "
                    "post-processing both succeeded; using the stored image."
                )
            logging.info("Image attached and preview rendered: %s", image_path.name)
            return src

        error = _first_visible(page, selectors["upload_error_text"])
        if error is not None:
            if toast_since is None:
                toast_since = time.monotonic()
                try:
                    toast_message = error.inner_text().strip()[:160]
                except PWError:
                    toast_message = "upload error toast"
            settled = not monitor.upload_inflight and not monitor.process_inflight
            if settled and time.monotonic() - toast_since >= 5:
                raise UploadError(f"Padlet reported an upload error: {toast_message}")
        else:
            toast_since = None

        # A file handed to a not-yet-initialised uploader produces no request at
        # all. That is a handoff failure, not a server failure, and is safe to
        # retry because nothing was transferred.
        if not monitor.upload_started and time.monotonic() - started >= 8:
            raise UploadError(
                "No upload request was observed after handing the file to Padlet "
                "(the uploader may not have been ready)."
            )

        page.wait_for_timeout(250)

    raise BotError(
        f"Image upload did not finish within {timeout_ms // 1000}s for {image_path.name}. "
        f"Last network state: upload={monitor.upload_responses[-2:]} "
        f"process={monitor.process_responses[-2:]} assets={monitor.asset_responses[-2:]}"
    )


def wait_upload_complete(
    page: Page,
    scope: Any,
    image_path: Path,
    selectors: dict[str, list[dict[str, Any]]],
    timeout_ms: int = 60_000,
    monitor: Optional["NetworkMonitor"] = None,
    security_timeout_ms: int = SECURITY_WAIT_MS,
) -> str:
    """Wait for a rendered preview with no upload spinner; return its src.

    With a monitor this is an evidence-based state machine: only a server-hosted
    preview combined with a 2xx transfer is success, and an HTTP/transport
    failure on the transfer, post-processing or asset fetch is the failure.
    Without one it falls back to the legacy UI-only check.
    """
    if monitor is not None:
        return _wait_upload_complete_networked(
            page, scope, image_path, selectors, timeout_ms, monitor, security_timeout_ms
        )
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if _first_visible(page, selectors["security_text"]) is not None:
            wait_out_security_check(page, selectors, security_timeout_ms, label="upload")
            deadline = max(deadline, time.monotonic() + 20)
        try:
            src = scope.evaluate(_PREVIEW_SRC_JS)
            uploading = scope.evaluate(_UPLOADING_JS)
        except PWError:
            src, uploading = None, False

        if src and not uploading:
            logging.info("Image attached and preview rendered: %s", image_path.name)
            return src

        error = _first_visible(page, selectors["upload_error_text"])
        if error is not None:
            # The toast can be spurious: the asset may already be stored and the
            # preview can still render a moment later. Give it a short grace.
            grace = time.monotonic() + 2.5
            while time.monotonic() < grace:
                try:
                    src = scope.evaluate(_PREVIEW_SRC_JS)
                    uploading = scope.evaluate(_UPLOADING_JS)
                except PWError:
                    src, uploading = None, False
                if src and not uploading:
                    logging.info("Image attached despite error toast: %s", image_path.name)
                    return src
                page.wait_for_timeout(250)
            try:
                message = error.inner_text().strip()[:160]
            except PWError:
                message = "upload error toast"
            raise UploadError(f"Padlet reported an upload error: {message}")

        page.wait_for_timeout(250)
    raise BotError(f"Image upload did not finish within {timeout_ms // 1000}s for {image_path.name}.")


# --------------------------------------------------------------------------- #
# Upload diagnostics: network monitor + explicit state machine
# --------------------------------------------------------------------------- #
#
# Design notes
# ------------
# * Every URL is reduced to ``scheme://host/path`` before it is written: query
#   strings can carry signed tokens, so they are dropped entirely.
# * Credential-bearing headers are redacted by name. Request bodies are never
#   recorded (an upload body is the image itself).
# * HTTP 4xx/5xx responses and transport-level ``requestfailed`` events are
#   tracked separately: a 500 is a response, not a failed request, and a
#   ``requestfailed`` says nothing about a status code.
# * A rendered preview is *not* proof of success. READY additionally requires
#   the server to have accepted the upload and the preview to be a server URL,
#   so a local ``blob:``/``data:`` preview cannot be mistaken for success.

_SENSITIVE_HEADERS = frozenset(
    {"cookie", "set-cookie", "authorization", "proxy-authorization", "x-api-key", "x-csrf-token"}
)

UPLOAD_STATES = (
    "SELECTED",
    "UPLOADING",
    "SERVER_ACCEPTED",
    "PROCESSING",
    "READY",
    "FAILED",
    "UNKNOWN",
)


def _sanitize_url(url: str) -> str:
    """Return ``scheme://host/path`` with query string and fragment removed."""
    try:
        parts = urlparse(url)
    except ValueError:
        return url.split("?", 1)[0].split("#", 1)[0]
    if not parts.scheme:
        return url.split("?", 1)[0].split("#", 1)[0]
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: ("[redacted]" if key.lower() in _SENSITIVE_HEADERS else value)
        for key, value in headers.items()
    }


def _classify_request(url: str) -> str:
    """Coarse bucket used to tell file transfer apart from asset loading."""
    try:
        parts = urlparse(url)
        host = parts.netloc.lower()
        path = parts.path
    except ValueError:
        return "other"
    if "padletcdn.com" in host or host.startswith("uploads-"):
        return "upload"
    # After the bytes are stored, Padlet asks its image service to finalise
    # (resize/convert) the file. This is the step the ~80% marker refers to.
    if "beethoven-v2/file" in path:
        return "process"
    if "padlet.pics" in host:
        return "image_load"
    if host.endswith("padlet.com") or host.endswith("padlet.org"):
        return "api"
    return "other"


def _is_server_asset(src: Optional[str]) -> bool:
    """True only for a preview served by Padlet's backend, not a local blob."""
    if not src:
        return False
    try:
        parts = urlparse(src)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.netloc.lower()
    return (
        "padlet.pics" in host
        or "padletcdn.com" in host
        or "padlet.com" in host
        or "googleusercontent.com" in host
        or "storage.googleapis.com" in host
    )


class NetworkMonitor:
    """Record upload-related network events to a JSONL file and summarise them.

    Only xhr/fetch/image requests are recorded, plus any request to the upload
    CDN regardless of its resource type (the transfer may be tagged ``other``
    by the browser depending on how Padlet initiates it).
    """

    def __init__(self, page: Page, path: Path) -> None:
        self.page = page
        self.path = path
        self._fh = path.open("w", encoding="utf-8")
        self._started: dict[int, tuple[float, Any]] = {}

        # Upload lifecycle
        self.upload_started = False
        self.upload_accepted = False
        self.upload_rejected: Optional[Any] = None
        self.upload_inflight = 0
        self.upload_request: Optional[tuple[str, str]] = None
        self.upload_responses: list[tuple[int, str]] = []

        # Post-processing lifecycle (Padlet asks its image service to finalise)
        self.process_started = False
        self.process_ok = False
        self.process_failed: Optional[Any] = None
        self.process_inflight = 0
        self.process_responses: list[tuple[int, str]] = []

        # Asset lifecycle
        self.image_load_ok = False
        self.image_load_failed: Optional[Any] = None
        self.asset_responses: list[tuple[int, str]] = []

        # Other API traffic (upload_attributes, links, etc.)
        self.api_responses: list[tuple[int, str]] = []

        # Cloudflare challenge (403 + cf-mitigated: challenge). Not a normal
        # permission error: a human can clear it, so callers pause instead of
        # stopping the run.
        self.challenge_detected = False
        self.challenge_stage: Optional[str] = None

        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_requestfailed)
        page.on("pageerror", self._on_pageerror)

    def reset_upload_tracking(self) -> None:
        """Forget board-load traffic so only the upload itself is judged."""
        self._started.clear()
        self.upload_started = False
        self.upload_accepted = False
        self.upload_rejected = None
        self.upload_inflight = 0
        self.upload_request = None
        self.upload_responses.clear()
        self.process_started = False
        self.process_ok = False
        self.process_failed = None
        self.process_inflight = 0
        self.process_responses.clear()
        self.image_load_ok = False
        self.image_load_failed = None
        self.asset_responses.clear()
        self.api_responses.clear()
        self.challenge_detected = False
        self.challenge_stage = None

    # -- recording -------------------------------------------------------- #

    def _note_challenge(self, bucket: str, response: Any) -> None:
        """Remember a Cloudflare challenge so callers can wait for a human.

        A challenge is answered with 403 + ``cf-mitigated: challenge``. It is
        not a permissions decision: clearing it in the visible browser is the
        intended path, so this must not be treated as a permanent refusal.
        """
        try:
            headers = dict(response.headers)
        except PWError:
            return
        mitigated = next(
            (str(v).lower() for k, v in headers.items() if k.lower() == "cf-mitigated"),
            "",
        )
        if "challenge" in mitigated:
            self.challenge_detected = True
            self.challenge_stage = bucket

    def clear_challenge_failure(self) -> None:
        """Forget a challenge-caused failure so a later retry can succeed."""
        if self.challenge_stage == "upload":
            self.upload_rejected = None
        elif self.challenge_stage == "process":
            self.process_failed = None
        elif self.challenge_stage == "image_load":
            self.image_load_failed = None
        self.challenge_detected = False
        self.challenge_stage = None

    def _write(self, record: dict[str, Any]) -> None:
        record["ts"] = datetime.now().isoformat(timespec="milliseconds")
        try:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()
        except (OSError, ValueError):
            pass

    def _wanted(self, resource_type: str, bucket: str) -> bool:
        return resource_type in ("xhr", "fetch", "image") or bucket in ("upload", "process")

    # -- handlers --------------------------------------------------------- #

    def _on_request(self, request: Any) -> None:
        try:
            url = request.url
            bucket = _classify_request(url)
            if not self._wanted(request.resource_type, bucket):
                return
            self._started[id(request)] = (time.monotonic(), request)
            if bucket == "upload":
                self.upload_started = True
                self.upload_inflight += 1
                if self.upload_request is None:
                    self.upload_request = (request.method, _sanitize_url(url))
            elif bucket == "process":
                self.process_started = True
                self.process_inflight += 1
            self._write(
                {
                    "kind": "request",
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "class": bucket,
                    "url": _sanitize_url(url),
                }
            )
        except PWError:
            pass

    def _on_response(self, response: Any) -> None:
        try:
            request = response.request
            url = response.url
            bucket = _classify_request(url)
            if not self._wanted(request.resource_type, bucket):
                return
            started = self._started.pop(id(request), None)
            duration_ms = round((time.monotonic() - started[0]) * 1000, 1) if started else None
            status = response.status
            if status >= 400:
                self._note_challenge(bucket, response)
            record: dict[str, Any] = {
                "kind": "response",
                "method": request.method,
                "resource_type": request.resource_type,
                "class": bucket,
                "url": _sanitize_url(url),
                "status": status,
                "duration_ms": duration_ms,
            }
            if bucket == "upload":
                self.upload_inflight = max(0, self.upload_inflight - 1)
                self.upload_responses.append((status, _sanitize_url(url)))
                if 200 <= status < 300:
                    self.upload_accepted = True
                elif status >= 400:
                    self.upload_rejected = status
                    try:
                        record["headers"] = _redact_headers(dict(response.headers))
                    except PWError:
                        pass
                    try:
                        record["body"] = response.text()[:600]
                    except PWError:
                        pass
            elif bucket == "process":
                self.process_inflight = max(0, self.process_inflight - 1)
                self.process_responses.append((status, _sanitize_url(url)))
                if 200 <= status < 300:
                    self.process_ok = True
                elif status >= 400:
                    self.process_failed = status
                    try:
                        record["headers"] = _redact_headers(dict(response.headers))
                    except PWError:
                        pass
                    try:
                        record["body"] = response.text()[:600]
                    except PWError:
                        pass
            elif bucket == "image_load":
                self.asset_responses.append((status, _sanitize_url(url)))
                if 200 <= status < 300:
                    self.image_load_ok = True
                elif status >= 400:
                    self.image_load_failed = status
            elif bucket == "api":
                self.api_responses.append((status, _sanitize_url(url)))
            self._write(record)
        except PWError:
            pass

    def _on_requestfailed(self, request: Any) -> None:
        try:
            url = request.url
            bucket = _classify_request(url)
            if not self._wanted(request.resource_type, bucket):
                return
            self._started.pop(id(request), None)
            failure = request.failure or "unknown"
            if bucket == "upload":
                self.upload_inflight = max(0, self.upload_inflight - 1)
                if self.upload_rejected is None:
                    self.upload_rejected = f"requestfailed: {failure}"
            elif bucket == "process":
                self.process_inflight = max(0, self.process_inflight - 1)
                if self.process_failed is None:
                    self.process_failed = f"requestfailed: {failure}"
            elif bucket == "image_load":
                if self.image_load_failed is None:
                    self.image_load_failed = f"requestfailed: {failure}"
            self._write(
                {
                    "kind": "requestfailed",
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "class": bucket,
                    "url": _sanitize_url(url),
                    "failure": failure,
                }
            )
        except PWError:
            pass

    def _on_pageerror(self, error: Any) -> None:
        self._write({"kind": "pageerror", "error": str(error)[:300]})

    # -- reporting -------------------------------------------------------- #

    def upload_request_line(self) -> str:
        if not self.upload_request:
            return "not observed"
        return f"{self.upload_request[0]} {self.upload_request[1]}"

    def upload_response_line(self) -> str:
        if not self.upload_responses:
            return "not observed"
        status, url = self.upload_responses[-1]
        return f"{status} {url}"

    def fatal_upload_status(self) -> Optional[tuple[str, Any]]:
        """First conclusive upload-side failure, as ``(stage, status)``."""
        if self.upload_rejected is not None:
            return "file transfer", self.upload_rejected
        if self.process_failed is not None:
            return "image post-processing", self.process_failed
        if self.upload_accepted and self.image_load_failed is not None:
            return "image asset", self.image_load_failed
        return None

    def processing_line(self) -> str:
        if self.process_responses:
            recent = self.process_responses[-3:]
            return "; ".join(f"{status} {url}" for status, url in recent)
        if not self.api_responses:
            return "no image-processing/API request observed"
        recent = self.api_responses[-3:]
        return "; ".join(f"{status} {url}" for status, url in recent)

    def loading_line(self) -> str:
        if not self.asset_responses:
            return "no image resource request observed"
        recent = self.asset_responses[-3:]
        return "; ".join(f"{status} {url}" for status, url in recent)

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


# One evaluate() for preview, progress state and progress value: fewer round
# trips means the sampled UI state is internally consistent.
_UPLOAD_UI_JS = r"""
(root) => {
  const imgs = [...root.querySelectorAll('img')]
    .filter(i => i.naturalWidth > 0 && i.offsetWidth >= 40 && i.offsetHeight >= 40);
  imgs.sort((a, b) => (b.offsetWidth * b.offsetHeight) - (a.offsetWidth * a.offsetHeight));
  const bars = [...document.querySelectorAll('[role="progressbar"],[aria-valuenow]')]
    .filter(e => e.offsetWidth > 0)
    .map(e => e.getAttribute('aria-valuenow'));
  const text = (root.innerText || '').slice(0, 4000);
  const pct = text.match(/(\d{1,3})\s*%/);
  return {
    src: imgs.length ? imgs[0].src : null,
    natural: imgs.length ? [imgs[0].naturalWidth, imgs[0].naturalHeight] : null,
    uploading: /(uploading|processing|please wait)/i.test(text),
    bars: bars,
    pct: pct ? Number(pct[1]) : null,
  };
}
"""


def track_upload_state(
    page: Page,
    scope: Locator,
    monitor: NetworkMonitor,
    selectors: dict[str, list[dict[str, Any]]],
    timeout_s: float = 90.0,
    security_timeout_ms: int = SECURITY_WAIT_MS,
) -> tuple[str, str, list[tuple[str, str]]]:
    """Follow SELECTED -> ... -> READY/FAILED using network + UI evidence.

    Returns ``(final_state, reason, history)``. Evidence is evaluated in this
    order, deliberately: an explicit error wins over anything else; a network
    failure of the transfer or of the asset beats a rendered preview; READY
    needs both a server-accepted upload and a server-hosted rendered image.
    If nothing conclusive is observed before the deadline the result is
    ``UNKNOWN`` -- never an assumed success.
    """

    history: list[tuple[str, str]] = []
    state = "SELECTED"
    reason = "file handed to the upload control"
    history.append((state, reason))
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        if _first_visible(page, selectors["security_text"]) is not None:
            # Diagnostics also pause instead of closing the window on the user.
            wait_out_security_check(page, selectors, security_timeout_ms, label="upload diagnostics")
            deadline = max(deadline, time.monotonic() + 20)

        try:
            ui = scope.evaluate(_UPLOAD_UI_JS)
        except PWError:
            ui = {"src": None, "uploading": False, "bars": [], "pct": None}

        err_text = ""
        error = _first_visible(page, selectors["upload_error_text"])
        if error is not None:
            try:
                err_text = error.inner_text().strip()[:200]
            except PWError:
                err_text = "upload error toast"

        progress_note = ""
        if ui.get("pct") is not None:
            progress_note += f"; UI progress {ui['pct']}%"
        if ui.get("bars"):
            progress_note += f"; progressbar={ui['bars']}"
        challenge_note = ""
        if monitor.challenge_detected:
            challenge_note = (
                f"; CF challenge on the {monitor.challenge_stage or 'upload'} request"
                " (403 cf-mitigated: challenge) — clear it in the browser"
            )

        if err_text:
            new_state, why = "FAILED", f"UI error message: {err_text}{progress_note}{challenge_note}"
        elif monitor.upload_rejected is not None:
            new_state, why = (
                "FAILED",
                f"upload request did not succeed: {monitor.upload_rejected}"
                f"{progress_note}{challenge_note}",
            )
        elif monitor.process_failed is not None:
            new_state, why = (
                "FAILED",
                f"image post-processing request failed: {monitor.process_failed}"
                f"{progress_note}{challenge_note}",
            )
        elif monitor.upload_accepted and monitor.image_load_failed is not None:
            new_state, why = (
                "FAILED",
                f"image resource failed to load: {monitor.image_load_failed}{progress_note}",
            )
        elif ui.get("src") and not ui.get("uploading") and monitor.upload_accepted and _is_server_asset(ui["src"]):
            new_state, why = "READY", "server accepted the upload and the server-hosted preview is rendered"
        elif monitor.upload_accepted and (monitor.upload_inflight or monitor.process_inflight):
            new_state, why = "SERVER_ACCEPTED", "a 2xx upload response arrived; a transfer is still in flight"
        elif monitor.upload_accepted:
            new_state, why = "PROCESSING", "upload accepted by the server; waiting for the processed asset"
        elif monitor.upload_started:
            new_state, why = "UPLOADING", "the upload request is in flight"
        else:
            new_state, why = "SELECTED", "waiting for the upload request to appear"

        if new_state != state:
            state, reason = new_state, why
            history.append((state, reason))
        if state in ("READY", "FAILED"):
            break
        page.wait_for_timeout(300)
    else:
        if state not in ("READY", "FAILED"):
            state = "UNKNOWN"
            reason = "timeout without conclusive network or UI evidence"
            history.append((state, reason))

    return state, reason, history


def diagnose_upload(
    url: str,
    image_path: Optional[str] = None,
    headless: bool = False,
    timeout_s: float = 90.0,
    profile_dir: Optional[Path] = CHROME_PROFILE_DIR,
    security_timeout_ms: int = SECURITY_WAIT_MS,
    cdp_url: Optional[str] = None,
) -> int:
    """Single-attempt upload diagnostic. Never publishes and never retries.

    The editor is left open at the end (the browser closes, so nothing is
    submitted) and the failure scene is preserved as a screenshot plus DOM.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    selectors = load_selectors()
    if image_path:
        image = Path(image_path).expanduser()
    else:
        image = random.choice(load_images())
    if not image.is_file():
        raise BotError(f"Image not found: {image}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    net_path = LOGS_DIR / f"upload_network_{stamp}.jsonl"
    report_path = LOGS_DIR / f"upload_debug_{stamp}.txt"
    shot_path = LOGS_DIR / f"upload_debug_{stamp}.png"
    dom_path = LOGS_DIR / f"upload_debug_{stamp}.html"

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text)
        lines.append(text)

    final_state = "UNKNOWN"
    final_reason = "diagnostic did not reach the upload tracking stage"
    history: list[tuple[str, str]] = []

    with sync_playwright() as pw:
        session = launch_browser(pw, headless, profile_dir, cdp_url, url)
        page = session.page
        monitor = NetworkMonitor(page, net_path)
        try:
            if not (cdp_url and _same_board(page.url, url)):
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            wait_for_board(page, selectors, security_timeout_ms=security_timeout_ms)
            check_not_logged_in(page, selectors)
            column = random.choice(get_columns(page, selectors))
            composer = open_editor(page, column, selectors)
            # The board's own thumbnails/avatars are not part of this upload.
            monitor.reset_upload_tracking()
            emit(f"Column: {column.title}")
            emit(f"Upload selected: {image.name} ({image.stat().st_size} bytes)")
            try:
                _attach_file(page, composer, image, selectors)
            except (SelectorError, PWError) as exc:
                final_state = "FAILED"
                final_reason = f"could not hand the file to the upload control: {exc}"
            else:
                final_state, final_reason, history = track_upload_state(
                    page, composer, monitor, selectors, timeout_s=timeout_s,
                    security_timeout_ms=security_timeout_ms,
                )
        except (BotError, PWError) as exc:
            final_state = "UNKNOWN"
            final_reason = f"diagnostic error before a conclusive upload state: {exc}"
        finally:
            try:
                page.screenshot(path=str(shot_path), full_page=True)
            except PWError:
                pass
            try:
                dom_path.write_text(page.content(), encoding="utf-8")
            except PWError:
                pass
            monitor.close()
            session.close()

            if history:
                emit("State transitions:")
                for name, why in history:
                    emit(f"  {name}: {why}")
            emit(f"Upload request: {monitor.upload_request_line()}")
            emit(f"Upload response: {monitor.upload_response_line()}")
            emit(f"Image processing: {monitor.processing_line()}")
            emit(f"Image loading: {monitor.loading_line()}")
            emit(f"Final status: {final_state}")
            emit(f"Reason: {final_reason}")
            emit(f"Network log: {net_path}")
            emit(f"Screenshot: {shot_path}")
            emit(f"DOM: {dom_path}")
            try:
                report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            except OSError:
                pass

    return 0 if final_state == "READY" else 1


def close_editor(page: Page, selectors: dict[str, list[dict[str, Any]]]) -> None:
    """Best-effort discard of an unsubmitted editor (dry run)."""
    for key in ("cancel_button",):
        loc = _first_visible(page, selectors[key])
        if loc is not None:
            try:
                loc.click()
                return
            except PWError:
                pass
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    page.keyboard.press("Escape")


def _toast_text(page: Page, selectors: dict[str, list[dict[str, Any]]]) -> str:
    for key in ("error_text", "moderation_text", "security_text"):
        loc = _first_visible(page, selectors[key])
        if loc is not None:
            try:
                return f"{key}: {loc.inner_text().strip()[:200]}"
            except PWError:
                return key
    return ""


def publish_post(
    page: Page,
    column: Column,
    image_path: Path,
    alias: str,
    selectors: dict[str, list[dict[str, Any]]],
    monitor: Optional["NetworkMonitor"] = None,
    security_timeout_ms: int = SECURITY_WAIT_MS,
) -> str:
    """Open composer -> upload -> submit -> verify. Returns a status constant."""
    open_editor(page, column, selectors)
    editor = resolve(page, "editor", selectors, timeout_ms=5_000)
    upload_image(
        page, image_path, selectors, monitor=monitor,
        security_timeout_ms=security_timeout_ms,
    )
    before_posts = _count_post_cards(page, selectors)

    resolve(page, "publish_button", selectors, timeout_ms=8_000).click()
    return verify_post(
        page, column, editor, before_posts, alias, selectors,
        security_timeout_ms=security_timeout_ms,
    )


_POST_IN_COLUMN_JS = r"""
(args) => {
  const [title, alias] = args;
  for (const s of document.querySelectorAll('section')) {
    const h = s.querySelector('h1,h2,h3,h4,h5,h6');
    if (!h || h.innerText.trim() !== title) continue;
    const text = s.innerText || '';
    if ((!alias || text.includes(alias)) && s.querySelector('img')) return true;
  }
  return false;
}
"""


def _count_post_cards(page: Page, selectors: dict[str, list[dict[str, Any]]]) -> int:
    for spec in selectors["post_card"]:
        try:
            return _spec_to_locator(page, spec).count()
        except PWError:
            continue
    return 0


def verify_post(
    page: Page,
    column: Column,
    editor: Locator,
    before_posts: int,
    alias: str,
    selectors: dict[str, list[dict[str, Any]]],
    security_timeout_ms: int = SECURITY_WAIT_MS,
) -> str:
    """Return published / pending_moderation / failed / uncertain.

    Success requires the composer to close *and* the uploaded image to be visible
    in the target column. Anything less is ``uncertain`` -- never assumed success.
    A security check pauses the verification so the user can clear it.
    """
    # 1) Editor should close.
    editor_closed = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if _first_visible(page, selectors["security_text"]) is not None:
            wait_out_security_check(page, selectors, security_timeout_ms, label="publish")
            deadline = max(deadline, time.monotonic() + 20)
        try:
            if not editor.is_visible():
                editor_closed = True
                break
        except PWError:
            editor_closed = True
            break
        page.wait_for_timeout(200)

    # 2) Explicit signals win over inference.
    toast = _toast_text(page, selectors)
    if toast.startswith("moderation_text"):
        return STATUS_PENDING
    if toast.startswith("error_text"):
        logging.error("Publish reported an error: %s", toast)
        return STATUS_FAILED

    if not editor_closed:
        logging.error("Editor remained open after clicking Publish.")
        return STATUS_FAILED

    # 3) Confirm the post. React re-renders after submit and drops our temporary
    #    markers, and the published image URL differs from the preview URL, so we
    #    look for the alias + an image inside the target section, or a rise in the
    #    page-wide post-card count.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if _first_visible(page, selectors["security_text"]) is not None:
            wait_out_security_check(page, selectors, security_timeout_ms, label="publish confirmation")
            deadline = max(deadline, time.monotonic() + 20)
        try:
            if page.evaluate(_POST_IN_COLUMN_JS, [column.title, alias]):
                return STATUS_PUBLISHED
        except PWError:
            pass
        try:
            if _count_post_cards(page, selectors) > before_posts:
                return STATUS_PUBLISHED
        except PWError:
            pass
        page.wait_for_timeout(300)

    # 4) Composer closed but we cannot prove a post was created.
    logging.warning("Composer closed but no new post could be confirmed in column %r.", column.title)
    return STATUS_UNCERTAIN


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


def diagnose(page: Page, selectors: dict[str, list[dict[str, Any]]], out_dir: Path) -> Path:
    """Dump screenshot, DOM and an interactive-element inventory; no posting."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    png = out_dir / f"diagnose_{stamp}.png"
    html = out_dir / f"diagnose_{stamp}.html"
    report = out_dir / f"diagnose_{stamp}.txt"

    page.screenshot(path=str(png), full_page=True)
    html.write_text(page.content(), encoding="utf-8")

    inventory = page.evaluate(
        r"""
        () => {
          const rows = [];
          for (const el of document.querySelectorAll(
              'button, [role="button"], input, textarea, [contenteditable="true"], a')) {
            const st = getComputedStyle(el);
            if (el.offsetWidth === 0 || el.offsetHeight === 0) continue;
            if (st.visibility === 'hidden' || st.display === 'none') continue;
            rows.push({
              tag: el.tagName.toLowerCase(),
              role: el.getAttribute('role') || '',
              type: el.getAttribute('type') || '',
              aria: el.getAttribute('aria-label') || '',
              text: (el.innerText || '').trim().slice(0, 60),
              cls: (el.className && String(el.className)).slice(0, 70),
            });
          }
          return rows;
        }
        """
    )

    lines = [f"URL: {page.url}", f"Title: {page.title()}", ""]
    for key in DEFAULT_SELECTORS:
        hit = _first_visible(page, selectors[key])
        lines.append(f"{key:28} -> {'FOUND' if hit else 'not found'}")
    lines += ["", f"Interactive elements ({len(inventory)}):"]
    for row in inventory:
        lines.append(str(row))

    report.write_text("\n".join(lines), encoding="utf-8")
    logging.info("Diagnostics written: %s / %s / %s", png, html, report)
    return report


def _dump_debug(page: Page, out_dir: Path, label: str) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        page.screenshot(path=str(out_dir / f"error_{stamp}_{label}.png"), full_page=True)
        (out_dir / f"error_{stamp}_{label}.html").write_text(page.content(), encoding="utf-8")
        logging.error("Saved debug artefacts for %s under %s", label, out_dir)
    except PWError as exc:
        logging.error("Could not capture debug artefacts: %s", exc)


# --------------------------------------------------------------------------- #
# Run loop
# --------------------------------------------------------------------------- #


@dataclass
class Stats:
    requested: int = 0
    published: int = 0
    pending: int = 0
    failed: int = 0
    uncertain: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)

    def record(self, status: str) -> None:
        if status == STATUS_PUBLISHED:
            self.published += 1
        elif status == STATUS_PENDING:
            self.pending += 1
        elif status == STATUS_FAILED:
            self.failed += 1
        elif status == STATUS_UNCERTAIN:
            self.uncertain += 1

    def summary(self) -> str:
        return (
            f"Requested: {self.requested}\n"
            f"Published: {self.published}\n"
            f"Pending moderation: {self.pending}\n"
            f"Failed: {self.failed}\n"
            f"Uncertain: {self.uncertain}"
        )


def _pick_image(images: list[Path], previous: Optional[Path]) -> Path:
    """Random image, avoiding an immediate repeat when more than one exists."""
    pool = [p for p in images if p != previous] or images
    return random.choice(pool)


def _pick_column(columns: list[Column], previous: Optional[Column]) -> Column:
    prev_title = previous.title if previous is not None else None
    pool = [c for c in columns if c.title != prev_title] or columns
    return random.choice(pool)


def _write_csv(stats: Stats, run_stamp: str) -> Path:
    path = LOGS_DIR / f"posts_{run_stamp}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["index", "timestamp", "url", "alias", "image", "column", "status", "detail"],
        )
        writer.writeheader()
        writer.writerows(stats.rows)
    return path


def run(
    url: str,
    count: int,
    *,
    headless: bool = False,
    dry_run: bool = False,
    alias: Optional[str] = None,
    interval: float = MIN_INTERVAL_S,
    diagnose_only: bool = False,
    profile_dir: Optional[Path] = CHROME_PROFILE_DIR,
    security_timeout_ms: int = SECURITY_WAIT_MS,
    cdp_url: Optional[str] = None,
) -> Stats:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    selectors = load_selectors()

    images = load_images()
    fixed_alias = load_alias(override=alias)
    logging.info("Loaded %d image(s); fixed alias = %r", len(images), fixed_alias)

    stats = Stats(requested=count)
    previous_image: Optional[Path] = None
    previous_column: Optional[Column] = None
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    with sync_playwright() as p:
        session = launch_browser(p, headless, profile_dir, cdp_url, url)
        page = session.page
        monitor = NetworkMonitor(page, LOGS_DIR / f"upload_network_{run_stamp}.jsonl")
        logging.info("Upload network log: %s", monitor.path)

        try:
            if not (cdp_url and _same_board(page.url, url)):
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            wait_for_board(page, selectors, security_timeout_ms=security_timeout_ms)

            if diagnose_only:
                report = diagnose(page, selectors, LOGS_DIR)
                print(f"\nDiagnostic report: {report}")
                return stats

            check_not_logged_in(page, selectors)
            set_guest_alias(page, fixed_alias, selectors)
            get_columns(page, selectors)  # fail fast before any posting

            for i in range(count):
                status = STATUS_FAILED
                detail = ""
                image: Optional[Path] = None
                column: Optional[Column] = None
                try:
                    # Re-scan each iteration: React re-renders after a submit and
                    # can discard the temporary data-pb-* markers.
                    columns = get_columns(page, selectors)
                    image = _pick_image(images, previous_image)
                    column = _pick_column(columns, previous_column)
                    previous_image, previous_column = image, column

                    if dry_run:
                        open_editor(page, column, selectors)
                        upload_image(
                            page, image, selectors, monitor=monitor,
                            security_timeout_ms=security_timeout_ms,
                        )
                        close_editor(page, selectors)
                        status = STATUS_DRY_RUN
                        detail = "image attached in editor; not published"
                    else:
                        status = publish_post(
                            page, column, image, fixed_alias, selectors, monitor=monitor,
                            security_timeout_ms=security_timeout_ms,
                        )
                except StopRun:
                    raise
                except (BotError, PWError) as exc:
                    status = STATUS_FAILED
                    detail = str(exc).splitlines()[0][:300]
                    logging.error("Post %d failed: %s", i + 1, exc)
                    _dump_debug(page, LOGS_DIR, f"post{i + 1}")

                image_name = image.name if image else "-"
                column_title = column.title if column else "-"
                logging.info(
                    "[%d/%d] %s | alias=%s | image=%s | column=%s | %s",
                    i + 1, count, status.upper(), fixed_alias, image_name, column_title, detail,
                )
                stats.record(status)
                stats.rows.append(
                    {
                        "index": i + 1,
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "url": url,
                        "alias": fixed_alias,
                        "image": image_name,
                        "column": column_title,
                        "status": status,
                        "detail": detail,
                    }
                )

                # Never double-submit an unknown outcome.
                if status == STATUS_UNCERTAIN:
                    logging.error("Outcome uncertain; stopping to avoid duplicate posts.")
                    break
                if status == STATUS_FAILED and not dry_run:
                    logging.error("Publish failed; stopping after the first failure.")
                    break

                if i < count - 1:
                    page.wait_for_timeout(int(interval * 1000))

        finally:
            monitor.close()
            session.close()

    csv_path = _write_csv(stats, run_stamp)
    if dry_run:
        stats.published = 0
    print("\n" + stats.summary())
    print(f"CSV log: {csv_path}")
    return stats


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def setup_logging(verbose: bool) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / f"run_{stamp}.log", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(message)s",
        handlers=handlers,
    )


def _prompt_url() -> str:
    while True:
        value = input("Padlet URL: ").strip()
        if value.startswith(("http://", "https://")):
            return value
        print("Please enter a full http(s) URL.")


def _prompt_count() -> int:
    while True:
        raw = input("Number of posts: ").strip()
        try:
            value = int(raw)
        except ValueError:
            print("Please enter a whole number.")
            continue
        if value < 1:
            print("Count must be at least 1.")
            continue
        return value


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Post images to a Padlet board as a fixed guest alias.")
    parser.add_argument("--url", help="Padlet board URL")
    parser.add_argument("--count", type=int, help="Number of posts")
    parser.add_argument("--alias", help="Fixed guest alias for the whole run")
    parser.add_argument("--headless", action="store_true", help="Run without a visible browser")
    parser.add_argument("--dry-run", action="store_true", help="Upload only; never publish")
    parser.add_argument("--diagnose", action="store_true", help="Dump selectors and exit; no posting")
    parser.add_argument(
        "--upload-debug",
        action="store_true",
        help="Single-attempt upload diagnosis: no publish, no retry, network log + screenshot",
    )
    parser.add_argument("--image", help="Image to use with --upload-debug (default: random from images/)")
    parser.add_argument(
        "--upload-timeout",
        type=float,
        default=90.0,
        help="Seconds to observe one upload in --upload-debug before reporting UNKNOWN",
    )
    parser.add_argument("--interval", type=float, default=MIN_INTERVAL_S, help="Seconds between posts")
    parser.add_argument(
        "--cdp-url",
        help=(
            "Attach to a Chrome you started with --remote-debugging-port, e.g. "
            "http://127.0.0.1:9222. Use the already-open tab; never closes the browser"
        ),
    )
    parser.add_argument(
        "--security-timeout",
        type=float,
        default=0.0,
        help="Seconds to wait for you to clear a security check (0 = wait forever; window stays open)",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the >%d confirmation" % CONFIRM_THRESHOLD)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    security_timeout_ms = int(max(0.0, args.security_timeout) * 1000)

    url = args.url or _prompt_url()

    if args.upload_debug:
        try:
            return diagnose_upload(
                url,
                image_path=args.image,
                headless=args.headless,
                timeout_s=args.upload_timeout,
                security_timeout_ms=security_timeout_ms,
                cdp_url=args.cdp_url,
            )
        except StopRun as exc:
            logging.error("Stopped: %s", exc)
            print(f"\nSTOPPED: {exc}")
            return 3
        except BotError as exc:
            logging.error("Aborted: %s", exc)
            print(f"\nABORTED: {exc}")
            return 2

    count = args.count if args.count is not None else _prompt_count()

    if count < 1:
        logging.error("Count must be at least 1.")
        return 2
    if count > CONFIRM_THRESHOLD and not args.yes:
        answer = input(f"Post {count} entries? Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            print("Aborted.")
            return 1
    if args.interval < MIN_INTERVAL_S:
        logging.warning("Raising interval from %.1fs to the %.1fs minimum.", args.interval, MIN_INTERVAL_S)
        args.interval = MIN_INTERVAL_S

    try:
        stats = run(
            url,
            count,
            headless=args.headless,
            dry_run=args.dry_run,
            alias=args.alias,
            interval=args.interval,
            diagnose_only=args.diagnose,
            security_timeout_ms=security_timeout_ms,
            cdp_url=args.cdp_url,
        )
    except StopRun as exc:
        logging.error("Stopped: %s", exc)
        print(f"\nSTOPPED: {exc}")
        return 3
    except BotError as exc:
        logging.error("Aborted: %s", exc)
        print(f"\nABORTED: {exc}")
        return 2

    if args.diagnose:
        return 0
    return 0 if stats.failed == 0 and stats.uncertain == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
