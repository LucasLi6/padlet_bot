#!/usr/bin/env python3
"""Open Padlet in a real, visible Google Chrome with a persistent profile.

This is the manual/session setup helper:

* ``channel="chrome"``  -> real Google Chrome, not bundled Chromium
* ``headless=False``    -> visible window, so a security check can be completed
* ``user_data_dir``     -> cookies / Cloudflare clearance persist across runs

The window is left open; close it yourself when finished.
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = Path("./chrome_profile").resolve()
START_URL = "https://padlet.com"


def main() -> None:
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(START_URL)
        print(f"Chrome opened with profile: {PROFILE_DIR}", flush=True)
        print("Complete any sign-in / security check in the window.", flush=True)
        print("The window stays open; close it when finished.", flush=True)

        try:
            while True:
                page.wait_for_timeout(1000)
        except Exception as exc:  # window closed by the user
            print(f"browser window closed: {exc}", flush=True)


if __name__ == "__main__":
    main()
