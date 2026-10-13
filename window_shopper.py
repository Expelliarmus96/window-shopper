"""
window_shopper.py
Logs in to sellpy.se, adds a product to cart, removes it.
Runs headless. Triggered exclusively via GitHub Actions — not intended for local use.
"""
import logging
import os
import time

from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

import sheets_log

load_dotenv()

EMAIL = os.getenv("SELLPY_EMAIL")
PASSWORD = os.getenv("SELLPY_PASSWORD")

HEADLESS = True
SLOW_MO = 0

BASE = "https://www.sellpy.se"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def _accept_cookies(page):
    """Dismiss cookie banner if present."""
    for sel in [
        "button:has-text('Tillåt alla cookies')",
        "button:has-text('Accept all')",
        "#onetrust-accept-btn-handler",
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                btn.click()
                page.wait_for_timeout(500)
                return
        except PWTimeout:
            pass


def _close_banner(page):
    """Dismiss any promo/info banner if present."""
    try:
        btn = page.locator("button[aria-label='Close banner']").first
        if btn.is_visible(timeout=2000):
            btn.click()
            page.wait_for_timeout(300)
    except PWTimeout:
        pass


def run_session():
    """One full automation cycle: login → add to cart → remove from cart."""
    if not EMAIL or not PASSWORD:
        log.error("SELLPY_EMAIL / SELLPY_PASSWORD not set in .env — aborting.")
        return

    log.info("Starting session…")
    t_start = time.monotonic()
    _item_id = "n/a"
    _removed_item_id = ""
    _cart_items = -1
    _error = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO)
        ctx = browser.new_context(locale="sv-SE", viewport={"width": 1280, "height": 800})
        page = ctx.new_page()

        try:
            # ── 1. Login ──────────────────────────────────────────────────────
            log.info("Navigating to login page…")
            page.goto(f"{BASE}/login", wait_until="load", timeout=60_000)
            page.wait_for_timeout(1_500)
            _accept_cookies(page)

            # Step 1: enter email
            log.info("Entering email…")
            page.locator("input[name='email']").fill(EMAIL)
            page.locator("button[type='submit']").first.click()

            # Step 2: wait for password field
            log.info("Waiting for password field…")
            page.wait_for_selector("input[type='password']", timeout=10_000)
            page.locator("input[type='password']").fill(PASSWORD)
            page.locator("button[type='submit']").first.click()

            # Confirm login succeeded by checking for a logged-in indicator
            try:
                page.wait_for_url(f"{BASE}/**", timeout=10_000)
                # Wait until the login page is gone
                page.wait_for_function(
                    "() => !window.location.pathname.includes('/login')",
                    timeout=10_000,
                )
                log.info("Logged in successfully.")
            except PWTimeout:
                log.error("Login may have failed — still on login page.")
                page.screenshot(path="error_login.png")
                return

            # ── 2. Navigate to a product ──────────────────────────────────────
            log.info("Fetching homepage to find a product…")
            page.goto(BASE, wait_until="load", timeout=60_000)
            page.wait_for_timeout(3_000)
            _accept_cookies(page)
            _close_banner(page)

            # Try products until we find one with an active add-to-cart button
            product_links = page.locator("a[href*='/item/']")
            product_links.first.wait_for(timeout=15_000)
            all_hrefs = [
                lnk.get_attribute("href")
                for lnk in product_links.all()[:20]
            ]
            # Deduplicate while preserving order
            seen, unique_hrefs = set(), []
            for h in all_hrefs:
                if h and h not in seen:
                    seen.add(h)
                    unique_hrefs.append(h)

            product_url = item_id = None
            for href in unique_hrefs:
                candidate_url = href if href.startswith("http") else BASE + href
                log.info(f"Trying product: {candidate_url}")
                page.goto(candidate_url, wait_until="load", timeout=60_000)
                page.wait_for_timeout(2_000)
                _close_banner(page)
                add_btn = page.locator("button:has-text('Lägg i varukorg')").first
                try:
                    add_btn.wait_for(timeout=5_000)
                    if add_btn.is_visible():
                        product_url = candidate_url
                        item_id = href.rstrip("/").split("/item/")[-1].split("?")[0]
                        _item_id = item_id
                        break
                except PWTimeout:
                    log.info("Add-to-cart not available, skipping…")

            if not product_url:
                log.error("No available product found with add-to-cart button.")
                return

            # ── 3. Add to cart ────────────────────────────────────────────────
            log.info(f"Adding item {item_id} to cart…")
            add_btn.scroll_into_view_if_needed()
            add_btn.click()
            log.info("Item added to cart.")

            # Small pause so the cart state settles
            page.wait_for_timeout(3_000)

            # ── 4. Go to cart and verify item is present ──────────────────────
            log.info("Navigating to cart…")
            page.goto(f"{BASE}/checkout/cart", wait_until="load", timeout=60_000)
            page.wait_for_timeout(3_000)

            log.info(f"Verifying item {item_id} is in cart…")
            try:
                verify_img = page.locator(f"img[src*='{item_id}']").first
                verify_img.wait_for(state="attached", timeout=30_000)
                log.info(f"Item {item_id} confirmed in cart.")
            except PWTimeout:
                log.warning(f"Cart verification timed out for {item_id} (lazy load) — continuing.")
            log.info("Waiting 2 minutes before removal…")
            page.wait_for_timeout(120_000)

            # ── 5. Remove the exact item we added ────────────────────────────
            log.info(f"Removing item {item_id} from cart…")
            removed = False

            # The cart renders all items in the DOM even if off-screen, so we
            # wait for "attached" (not "visible"), scroll it into view, then click.
            try:
                img = page.locator(f"img[src*='{item_id}']").first
                # Wait for element to be in DOM regardless of scroll position
                img.wait_for(state="attached", timeout=15_000)
                img.scroll_into_view_if_needed()
                page.wait_for_timeout(500)
                # Walk up to the NEAREST ancestor div that actually contains a
                # trash button, then click it. This is robust to per-item nesting
                # differences (sale badges etc. add wrapper divs, so a fixed
                # ancestor depth would target a neighbouring item's button).
                btn = img.locator(
                    "xpath=ancestor::div[.//button[descendant::*[@data-testid='icon-TRASH']]][1]"
                ).locator("button:has([data-testid='icon-TRASH'])").first
                btn.wait_for(state="visible", timeout=5_000)
                btn.click()
                page.wait_for_timeout(2_000)
                _removed_item_id = item_id
                log.info(f"Item {item_id} removed precisely via img[src*] selector.")
                removed = True
                # Count remaining cart items (excludes the item we just removed)
                _cart_items = page.locator(
                    "button:not([aria-label='Close banner']):has([data-testid='icon-TRASH'])"
                ).count()
                log.info(f"Remaining cart items: {_cart_items}")
            except PWTimeout:
                log.warning("Primary remove selector timed out.")

            if not removed:
                page.screenshot(path="debug_cart.png")
                log.warning(
                    "Could not remove item — screenshot saved to debug_cart.png."
                )
            if removed:
                log.info("Session completed successfully.")
                sheets_log.log_run("success", _item_id, time.monotonic() - t_start, _cart_items, removed_item_id=_removed_item_id)

        except Exception as exc:
            _error = str(exc)
            log.error(f"Unexpected error: {exc}", exc_info=True)
            sheets_log.log_run("failure", _item_id, time.monotonic() - t_start, _cart_items, _error, removed_item_id=_removed_item_id)
            try:
                page.screenshot(path="error_unexpected.png")
            except Exception:
                pass
        finally:
            page.wait_for_timeout(2_000)
            browser.close()
            log.info("Browser closed.")


def main():
    run_session()


if __name__ == "__main__":
    main()
