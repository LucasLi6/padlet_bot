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

SUPPORTED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_ALIAS = "TestBot"
MIN_INTERVAL_S = 5.0
CONFIRM_THRESHOLD = 20

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
        {"kind": "text", "value": r"(error (when |while )?uploading|failed to upload|couldn'?t upload|could not upload|sorry,? there.?s an? error)"},
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
    """Padlet displayed an upload-failure message in the composer."""


class StopRun(Exception):
    """A condition that must abort the whole run (captcha, rate limit, ...)."""


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


def wait_for_board(page: Page, selectors: dict[str, list[dict[str, Any]]], timeout_ms: int = 30_000) -> None:
    """Wait until the board UI is interactive, aborting on security walls."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if exists(page, "security_text", selectors):
            raise StopRun(
                "Padlet returned a security check / captcha. Automation stopped; "
                "complete the check manually and rerun."
            )
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
        raise BotError(
            "'Change name' was not available in the visitor menu. The board may "
            "forbid anonymous renaming, or this is not a guest session."
        ) from exc
    change.click()
    field = resolve(page, "name_input", selectors, timeout_ms=5000)
    return avatar, field


def set_guest_alias(page: Page, name: str, selectors: dict[str, list[dict[str, Any]]]) -> None:
    """Set the fixed guest alias once and verify it persisted.

    Verification reopens the 'Change name' dialog and reads the stored value --
    stronger than checking the avatar, since Padlet renders the avatar as an
    image with no text.
    """
    if not name.strip():
        raise BotError("Alias must not be empty.")

    avatar, field = _open_change_name(page, selectors)
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
    """Tag and return every column that has a usable add-post button."""
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


def upload_image(page: Page, image_path: Path, selectors: dict[str, list[dict[str, Any]]]) -> Optional[str]:
    """Attach ``image_path``; return the preview src.

    A transient Padlet upload error (the "error when uploading" toast) is retried
    once. Nothing has been submitted at this point, so retrying cannot duplicate a
    post. Timeouts are *not* retried, to avoid attaching the file twice.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, 4):
        if attempt > 1:
            # A failed attempt leaves its toast on screen. Clear it first, otherwise
            # the next wait_upload_complete would read the stale message and abort
            # before the new upload has a chance.
            _wait_upload_error_gone(page, selectors)
            page.wait_for_timeout(2000 * (attempt - 1))
        composer = resolve(page, "composer", selectors, timeout_ms=8_000)
        try:
            _attach_file(page, composer, image_path, selectors)
            return wait_upload_complete(page, composer, image_path, selectors)
        except UploadError as exc:
            last_error = exc
            logging.warning("Upload attempt %d/3 failed: %s", attempt, exc)
    raise BotError(f"Upload failed after 3 attempts: {last_error}")


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


def wait_upload_complete(
    page: Page,
    scope: Any,
    image_path: Path,
    selectors: dict[str, list[dict[str, Any]]],
    timeout_ms: int = 60_000,
) -> str:
    """Wait for a rendered preview with no upload spinner; return its src.

    Raises UploadError immediately when Padlet shows its upload-failure toast.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
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
) -> str:
    """Open composer -> upload -> submit -> verify. Returns a status constant."""
    open_editor(page, column, selectors)
    editor = resolve(page, "editor", selectors, timeout_ms=5_000)
    upload_image(page, image_path, selectors)
    before_posts = _count_post_cards(page, selectors)

    resolve(page, "publish_button", selectors, timeout_ms=8_000).click()
    return verify_post(page, column, editor, before_posts, alias, selectors)


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
) -> str:
    """Return published / pending_moderation / failed / uncertain.

    Success requires the composer to close *and* the uploaded image to be visible
    in the target column. Anything less is ``uncertain`` -- never assumed success.
    """
    # 1) Editor should close.
    editor_closed = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if not editor.is_visible():
                editor_closed = True
                break
        except PWError:
            editor_closed = True
            break
        page.wait_for_timeout(200)

    # 2) Explicit signals win over inference.
    if exists(page, "security_text", selectors):
        raise StopRun("Security check / captcha appeared during publish. Stopped.")

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
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            wait_for_board(page, selectors)

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
                        upload_image(page, image, selectors)
                        close_editor(page, selectors)
                        status = STATUS_DRY_RUN
                        detail = "image attached in editor; not published"
                    else:
                        status = publish_post(page, column, image, fixed_alias, selectors)
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
            try:
                context.close()
                browser.close()
            except PWError:
                pass

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
    parser.add_argument("--interval", type=float, default=MIN_INTERVAL_S, help="Seconds between posts")
    parser.add_argument("--yes", action="store_true", help="Skip the >%d confirmation" % CONFIRM_THRESHOLD)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    url = args.url or _prompt_url()
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
