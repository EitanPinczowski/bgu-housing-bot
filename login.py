"""
One-time Facebook login for the scraper.

Opens a REAL, non-headless Chromium window backed by a persistent profile
directory (config.SCRAPER_PROFILE_DIR). You log in by hand — with 2FA, the
usual "was this you?" checks, whatever — exactly as a human would. The session
cookies are written into the profile dir, so scraper.py can reuse them later
without ever touching your password or injecting cookies.

Why this way (see CLAUDE.md → SAFETY CONSTRAINTS): the user has only their
personal Facebook account. A persistent real-browser profile that a human
logged into is far less suspicious than headless cookie injection, and it keeps
the user in control.

    python login.py

Run it once. Re-run only if the session expires or Facebook logs you out.
"""
from __future__ import annotations

from playwright.sync_api import sync_playwright

import config
import scraper


def main() -> None:
    # Don't open the profile while a scheduled scraper run has it (they deadlock).
    if not scraper.acquire_lock():
        print("A scraper session is currently running — wait for it to finish, then re-run login.py.")
        return
    config.SCRAPER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Launching a browser with profile: {config.SCRAPER_PROFILE_DIR}")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(config.SCRAPER_PROFILE_DIR),
            headless=False,
            locale="he-IL",
            timezone_id="Asia/Jerusalem",
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.facebook.com")
        print(
            "\nA Facebook window is open.\n"
            "  1. Log in fully (including any 2FA / verification).\n"
            "  2. Make sure you land on your normal feed.\n"
            "  3. Come back here and press Enter to save the session and close.\n"
        )
        input("Press Enter when you're logged in... ")
        context.close()
    scraper.release_lock()
    print("Session saved. You can now run the scraper (main.py).")


if __name__ == "__main__":
    main()
