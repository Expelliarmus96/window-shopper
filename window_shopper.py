"""
window_shopper.py
Logs in to sellpy.se and runs one of three modes:

  cycle (default)  add a product to cart, wait, remove it (every 30 min)
  fill             add every available item from one favorites list
                   (SELLPY_FAVORITES_LIST, default "300+") to the cart
  empty            remove every item from the cart

Usage: python window_shopper.py [--mode cycle|fill|empty] [--dry-run]
(MODE / DRY_RUN env vars work too; the CLI flags win.)
Runs headless. Triggered exclusively via GitHub Actions — not intended for local use.
"""
import argparse
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

# The "300+" favorites list. fill goes straight here. Override with the
# SELLPY_FAVORITES_URL variable, or set it to "auto" to make the bot find the
# favorites page itself and click the list named SELLPY_FAVORITES_LIST.
_DEFAULT_LIST_URL = f"{BASE}/my-shopping/favorites/list/PMQH2pUIu3"
FAVORITES_URL = os.getenv("SELLPY_FAVORITES_URL", "").strip() or _DEFAULT_LIST_URL
if FAVORITES_URL.lower() == "auto":
    FAVORITES_URL = ""
FAVORITES_FALLBACK_PATHS = ["/my-shopping/favorites", "/favorites"]
# Only items in this favorites list are added. If SELLPY_FAVORITES_URL already
# points straight at the list, the name is only used for logging.
FAVORITES_LIST = os.getenv("SELLPY_FAVORITES_LIST", "").strip() or "300+"

ADD_BTN = "button:has-text('Lägg i varukorg')"
TRASH_BTN = "button:not([aria-label='Close banner']):has([data-testid='icon-TRASH'])"

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


def _login(page) -> bool:
    """Log in. Returns True on success; saves error_login.png on failure."""
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
        return True
    except PWTimeout:
        log.error("Login may have failed — still on login page.")
        page.screenshot(path="error_login.png")
        return False


def _cart_count(page) -> int:
    """Open the cart and count the items in it."""
    page.goto(f"{BASE}/checkout/cart", wait_until="load", timeout=60_000)
    page.wait_for_timeout(3_000)
    _close_banner(page)
    return page.locator(TRASH_BTN).count()


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
            if not _login(page):
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
                _cart_items = page.locator(TRASH_BTN).count()
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


# ── Favorites → cart ("fill") ─────────────────────────────────────────────────

def _open_favorites(page) -> bool:
    """Navigate to the FAVORITES_LIST list. Returns True if we got there."""
    if FAVORITES_URL:
        log.info(f"Opening list '{FAVORITES_LIST}' from SELLPY_FAVORITES_URL: {FAVORITES_URL}")
        page.goto(FAVORITES_URL, wait_until="load", timeout=60_000)
        page.wait_for_timeout(3_000)
        return "/login" not in page.url
    return _open_favorites_page(page) and _open_list(page, FAVORITES_LIST)


def _open_list(page, name: str) -> bool:
    """On the favorites page, click the list called `name` (exact text)."""
    _close_banner(page)
    url_before = page.url
    target = page.get_by_text(name, exact=True).first
    try:
        target.wait_for(state="visible", timeout=10_000)
    except PWTimeout:
        page.screenshot(path="error_favorites.png")
        log.error(f"No favorites list named '{name}' on {page.url}.")
        return False
    target.scroll_into_view_if_needed()
    target.click()
    page.wait_for_timeout(3_000)
    log.info(f"Opened list '{name}': {page.url}")
    if page.url == url_before:
        log.warning("URL didn't change after opening the list — check the item ids "
                    "in this log really are from that list (or set SELLPY_FAVORITES_URL).")
    return True


def _open_favorites_page(page) -> bool:
    """Navigate to the top-level favorites page. Returns True if we got there."""

    # Preferred: follow the heart link in the site header.
    page.goto(BASE, wait_until="load", timeout=60_000)
    page.wait_for_timeout(2_000)
    _accept_cookies(page)
    _close_banner(page)
    link = page.locator(
        "header a:has([data-testid='icon-HEART']), "
        "a[href*='favorit']:not([href*='/item/']), "
        "a[aria-label*='avorit']"
    ).first
    try:
        if link.is_visible(timeout=5_000):
            href = link.get_attribute("href") or ""
            url = href if href.startswith("http") else BASE + href
            log.info(f"Found favorites link in header: {url}")
            page.goto(url, wait_until="load", timeout=60_000)
            page.wait_for_timeout(3_000)
            return True
    except PWTimeout:
        pass

    # Fallback: try known paths; accept one that doesn't redirect elsewhere.
    for path in FAVORITES_FALLBACK_PATHS:
        log.info(f"Trying favorites path {path}…")
        resp = page.goto(BASE + path, wait_until="load", timeout=60_000)
        page.wait_for_timeout(2_000)
        if resp and resp.ok and path in page.url:
            log.info(f"Favorites page: {page.url}")
            return True

    page.screenshot(path="error_favorites.png")
    log.error("Could not find the favorites page — set SELLPY_FAVORITES_URL.")
    return False


def _collect_item_ids(page) -> list:
    """Return unique item ids on the current list, across all pages.

    Handles all three ways a long list can be split: infinite scroll,
    a "Visa fler" (show more) button, and numbered pages with a next link.
    """
    ids = []

    def harvest():
        root = page.locator("main").first if page.locator("main").count() else page
        for lnk in root.locator("a[href*='/item/']").all():
            href = lnk.get_attribute("href") or ""
            item_id = href.rstrip("/").split("/item/")[-1].split("?")[0]
            if item_id and item_id not in ids:
                ids.append(item_id)

    show_more = page.locator(
        "button:has-text('Visa fler'), button:has-text('Ladda fler'), "
        "button:has-text('Visa mer'), button:has-text('Show more'), "
        "button:has-text('Load more')"
    ).first
    next_page = page.locator(
        "a[rel='next'], a[aria-label*='Nästa'], button[aria-label*='Nästa'], "
        "a[aria-label*='Next'], button[aria-label*='Next'], "
        "a:has([data-testid='icon-CHEVRON_RIGHT']), button:has([data-testid='icon-CHEVRON_RIGHT'])"
    ).first

    for page_no in range(1, 51):                      # hard cap: 50 pages
        stable_rounds = 0
        for _ in range(60):                           # scroll + "show more"
            before = len(ids)
            harvest()
            if len(ids) == before:
                stable_rounds += 1
                try:
                    if show_more.is_visible():
                        show_more.scroll_into_view_if_needed()
                        show_more.click()
                        log.info("Clicked 'show more'…")
                        page.wait_for_timeout(2_000)
                        stable_rounds = 0
                        continue
                except PWTimeout:
                    pass
            else:
                stable_rounds = 0
            if stable_rounds >= 3:
                break
            page.mouse.wheel(0, 2_500)
            page.wait_for_timeout(1_000)

        log.info(f"Page {page_no}: {len(ids)} items collected so far")
        try:
            if not (next_page.is_visible() and next_page.is_enabled()
                    and next_page.get_attribute("aria-disabled") != "true"):
                break
            before = len(ids)
            url_before = page.url
            next_page.scroll_into_view_if_needed()
            next_page.click()
            page.wait_for_timeout(3_000)
            harvest()
            if len(ids) == before and page.url == url_before:
                break                                  # next did nothing: last page
        except PWTimeout:
            break
    return ids


_SKIP_REASONS = [
    ("in cart", ["I varukorgen", "Ligger i varukorgen", "Gå till varukorgen"]),
    ("sold", ["Såld", "Sålt", "Slutsåld"]),
    ("reserved", ["Reserverad", "Reserverat", "Någon annan har den i sin varukorg"]),
]


def _skip_reason(page) -> str:
    """Best guess at why an item has no add-to-cart button."""
    root = page.locator("main").first if page.locator("main").count() else page
    for reason, phrases in _SKIP_REASONS:
        for phrase in phrases:
            try:
                if root.get_by_text(phrase).first.is_visible():
                    return reason
            except Exception:
                pass
    return "unknown"


def run_fill(dry_run: bool = False):
    """Add every available item on the favorites list to the cart."""
    log.info(f"Starting FILL session{' (dry run)' if dry_run else ''}…")
    t_start = time.monotonic()
    added, skipped, cart_items = [], [], -1
    reasons = {}
    unknown_shots = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO)
        ctx = browser.new_context(locale="sv-SE", viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            if not _login(page):
                sheets_log.log_run("failure", "FILL", time.monotonic() - t_start, -1, "login failed")
                return
            if not _open_favorites(page):
                sheets_log.log_run("failure", "FILL", time.monotonic() - t_start, -1, f"favorites list '{FAVORITES_LIST}' not found")
                return

            item_ids = _collect_item_ids(page)
            log.info(f"List '{FAVORITES_LIST}' ({page.url}) has {len(item_ids)} items: {item_ids}")
            if not item_ids:
                page.screenshot(path="error_favorites.png")
                raise RuntimeError(f"List '{FAVORITES_LIST}' opened but no items were found")
            if dry_run:
                log.info("Dry run — not adding anything.")
                return

            for item_id in item_ids:
                page.goto(f"{BASE}/item/{item_id}", wait_until="load", timeout=60_000)
                page.wait_for_timeout(1_500)
                _close_banner(page)
                btn = page.locator(ADD_BTN).first
                try:
                    btn.wait_for(state="visible", timeout=5_000)
                    btn.scroll_into_view_if_needed()
                    btn.click()
                    page.wait_for_timeout(1_500)
                    added.append(item_id)
                    log.info(f"Added {item_id} ({len(added)} so far)")
                except PWTimeout:
                    reason = _skip_reason(page)
                    skipped.append(item_id)
                    reasons[reason] = reasons.get(reason, 0) + 1
                    log.info(f"Skipped {item_id} — {reason}")
                    if reason == "unknown" and unknown_shots < 3:
                        unknown_shots += 1
                        page.screenshot(path=f"skip_{item_id}.png", full_page=True)

            cart_items = _cart_count(page)
            why = ", ".join(f"{n} {r}" for r, n in reasons.items())
            log.info(f"FILL done: {len(added)} added, {len(skipped)} skipped ({why}), cart now {cart_items}")
            sheets_log.log_run(
                "success",
                f"FILL {FAVORITES_LIST}: {len(added)} added, {len(skipped)} skipped" + (f" ({why})" if why else ""),
                time.monotonic() - t_start, cart_items,
            )
        except Exception as exc:
            log.error(f"Unexpected error: {exc}", exc_info=True)
            sheets_log.log_run(
                "failure", f"FILL: {len(added)} added before error",
                time.monotonic() - t_start, cart_items, str(exc),
            )
            try:
                page.screenshot(path="error_unexpected.png")
            except Exception:
                pass
        finally:
            browser.close()
            log.info("Browser closed.")


# ── Empty the cart ("empty") ──────────────────────────────────────────────────

def run_empty(dry_run: bool = False):
    """Remove every item from the cart."""
    log.info(f"Starting EMPTY session{' (dry run)' if dry_run else ''}…")
    t_start = time.monotonic()
    removed, cart_items = 0, -1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO)
        ctx = browser.new_context(locale="sv-SE", viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            if not _login(page):
                sheets_log.log_run("failure", "", time.monotonic() - t_start, -1, "login failed", removed_item_id="EMPTY")
                return

            cart_items = _cart_count(page)
            log.info(f"Cart has {cart_items} items.")
            if dry_run:
                log.info("Dry run — not removing anything.")
                return

            # Up to 3 passes; each pass reloads the cart in case some rows
            # didn't render or a click didn't register.
            for pass_no in range(1, 4):
                stuck = 0
                while True:
                    btns = page.locator(TRASH_BTN)
                    n = btns.count()
                    if n == 0:
                        break
                    btn = btns.first
                    btn.scroll_into_view_if_needed()
                    btn.click()
                    try:
                        page.wait_for_function(
                            "([sel, n]) => document.querySelectorAll(sel).length < n",
                            arg=[TRASH_BTN, n],
                            timeout=8_000,
                        )
                        removed += 1
                        stuck = 0
                        if removed % 10 == 0:
                            log.info(f"Removed {removed} so far…")
                    except PWTimeout:
                        stuck += 1
                        log.warning(f"Remove click didn't take effect (attempt {stuck}).")
                        if stuck >= 3:
                            break
                cart_items = _cart_count(page)
                log.info(f"After pass {pass_no}: {cart_items} items left.")
                if cart_items == 0:
                    break

            status = "success" if cart_items == 0 else "failure"
            error = "" if cart_items == 0 else f"{cart_items} items left in cart after emptying"
            if cart_items:
                page.screenshot(path="debug_cart.png")
            log.info(f"EMPTY done: {removed} removed, cart now {cart_items}")
            sheets_log.log_run(
                status, "", time.monotonic() - t_start, cart_items, error,
                removed_item_id=f"EMPTY: {removed} removed",
            )
        except Exception as exc:
            log.error(f"Unexpected error: {exc}", exc_info=True)
            sheets_log.log_run(
                "failure", "", time.monotonic() - t_start, cart_items, str(exc),
                removed_item_id=f"EMPTY: {removed} removed before error",
            )
            try:
                page.screenshot(path="error_unexpected.png")
            except Exception:
                pass
        finally:
            browser.close()
            log.info("Browser closed.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["cycle", "fill", "empty"],
                        default=(os.getenv("MODE") or "cycle").strip().lower())
    parser.add_argument("--dry-run", action="store_true",
                        default=os.getenv("DRY_RUN", "").strip().lower() == "true")
    args = parser.parse_args()

    if not EMAIL or not PASSWORD:
        log.error("SELLPY_EMAIL / SELLPY_PASSWORD not set — aborting.")
        return

    if args.mode == "fill":
        run_fill(args.dry_run)
    elif args.mode == "empty":
        run_empty(args.dry_run)
    else:
        run_session()


if __name__ == "__main__":
    main()
