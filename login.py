#!/usr/bin/env python3
"""One-time ChatGPT login / model-selection helper for the side pipeline.

Opens the SAME Chromium profile the pipeline uses, headed, on chatgpt.com, and
keeps it open until you press Enter in this terminal. Use it to:

  1. Log in to your ChatGPT account (once — the session is saved in the profile).
  2. Select the model you want the runs to use (e.g. GPT Pro) in the ChatGPT UI.
     New chats the pipeline opens inherit whatever model is selected here.
  3. Optionally open your Project so it becomes the active one.

When you're done, come back to this terminal and press Enter to save + close.

    python login.py
"""
from __future__ import annotations

import os
from pathlib import Path

import erdos_common as ec
from playwright.sync_api import sync_playwright


def main() -> None:
    profile_dir = Path(os.environ.get("CHATGPT_PROFILE_DIR") or ec.PROFILE_DIR)
    profile_dir.mkdir(parents=True, exist_ok=True)
    print(f"Opening Chromium on profile: {profile_dir}")
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            no_viewport=False,
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(ec.CHATGPT_URL, wait_until="domcontentloaded")
        print(
            "\n1. Log in to ChatGPT (if prompted).\n"
            "2. Pick the model you want the runs to use (e.g. GPT Pro).\n"
            "3. Optionally open your Project.\n\n"
            "Then return here and press Enter to save the session and close."
        )
        try:
            input()
        finally:
            context.close()


if __name__ == "__main__":
    main()
