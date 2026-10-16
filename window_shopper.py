"""
window_shopper.py
Logs in to sellpy.se and runs one of three modes:

  cycle (default)  every 30 min: add any "300+" items missing from the cart;
                   if none were added, add a random product, wait, remove it.
                   (Between 01:00 and 05:40 only the random add/remove runs,
                   so the nightly empty isn't undone.)
  fill             add every available item from one favorites list
                   (SELLPY_FAVORITES_LIST, default "300+") to the cart
  empty            remove every item from the cart

Usage: python window_shopper.py [--mode cycle|fill|empty] [--dry-run]
(MODE / DRY_RUN env vars work too; the CLI flags win.)
Runs headless. Triggered exclusively via GitHub Actions — not intended for local use.
"""
import argparse
import datetime
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

# cycle doesn't top up from the list in this window (cart is emptied at 01:10
# and refilled by fill at 05:40). Stockholm time, (hour, minute).
NIGHT_START, NIGHT_END = (1, 0), (5, 40)
# cycle stops adding list items after this long, so it stays within the
# workflow's 12-minute timeout. Anything left is picked up next cycle.
CYCLE_FAVORITES_BUDGET_S = 7 * 60

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

            # ── 1b. Top up from the favorites list ────────────────────────────
            # Any "300+" items not in the cart get added. If at least one was
            # added, that's this run's activity — skip the random add/remove.
            if _in_night_window():
                log.info("Night window — skipping the favorites top-up.")
            else:
                try:
                    _, missing = _missing_from_cart(page)
                    if missing:
                        added, skipped, reasons = _add_items(
                            page, missing, deadline=t_start + CYCLE_FAVORITES_BUDGET_S)
                        if added:
                            _cart_items = _cart_count(page)
                            log.info(f"Topped up {len(added)} from '{FAVORITES_LIST}', cart now "
                                     f"{_cart_items} — skipping random add/remove.")
                            sheets_log.log_run(
                                "success",
                                f"{FAVORITES_LIST}: {len(added)} added"
                                + (f", {len(skipped)} skipped ({_why(reasons)})" if skipped else ""),
                                time.monotonic() - t_start, _cart_items,
                            )
                            return
                    log.info("Nothing from the list could be added — doing a random add/remove.")
                except Exception as exc:
                    # Don't let a favorites problem stop the regular cycle.
                    log.warning(f"Favorites top-up failed ({exc}) — doing a random add/remove.")

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


def _page_url(url: str, n: int) -> str:
    """Return `url` with its ?page= query parameter set to n."""
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
    parts = urlparse(url)
    query = parse_qs(parts.query)
    query["page"] = [str(n)]
    return urlunparse(parts._replace(query=urlencode(query, doseq=True)))


def _collect_item_ids(page) -> list:
    """Return unique item ids on the current list, across all its pages.

    Sellpy splits favorites lists into pages of 60 items, each at its own
    address (...?page=2, ...?page=3). We open page 1, 2, 3… in turn and stop
    at the first page that adds no new items.
    """
    ids = []

    def harvest() -> int:
        """Collect ids on the page as loaded (scrolling for lazy items)."""
        before, stable_rounds = len(ids), 0
        for _ in range(30):
            n = len(ids)
            root = page.locator("main").first if page.locator("main").count() else page
            for lnk in root.locator("a[href*='/item/']").all():
                href = lnk.get_attribute("href") or ""
                item_id = href.rstrip("/").split("/item/")[-1].split("?")[0]
                if item_id and item_id not in ids:
                    ids.append(item_id)
            stable_rounds = stable_rounds + 1 if len(ids) == n else 0
            if stable_rounds >= 2:
                break
            page.mouse.wheel(0, 2_500)
            page.wait_for_timeout(800)
        return len(ids) - before

    base_url = page.url
    for page_no in range(1, 51):                      # hard cap: 50 pages
        if page_no > 1:
            page.goto(_page_url(base_url, page_no), wait_until="load", timeout=60_000)
            page.wait_for_timeout(2_500)
            _close_banner(page)
        new = harvest()
        log.info(f"Page {page_no}: {new} new items, {len(ids)} total")
        if new == 0:
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


def _in_night_window() -> bool:
    from zoneinfo import ZoneInfo
    now = datetime.datetime.now(ZoneInfo("Europe/Stockholm"))
    return NIGHT_START <= (now.hour, now.minute) < NIGHT_END


def _cart_html(page) -> str:
    """Open the cart and return the HTML of the cart ROWS only.

    Only the rows (the smallest block around each trash button that has an
    image) count. The rest of the page can't be used: Sellpy embeds the
    user's favourites in every page, so every favourite's id appears in the
    page HTML even when the cart is empty.
    """
    page.goto(f"{BASE}/checkout/cart", wait_until="load", timeout=60_000)
    page.wait_for_timeout(3_000)
    _close_banner(page)
    last_height = -1
    for _ in range(40):                       # scroll so every row renders
        height = page.evaluate("document.body.scrollHeight")
        if height == last_height:
            break
        last_height = height
        page.mouse.wheel(0, 3_000)
        page.wait_for_timeout(600)
    rows = page.evaluate("""() => {
        const out = [];
        for (const b of document.querySelectorAll('button')) {
            if (!b.querySelector('[data-testid="icon-TRASH"]')) continue;
            if (b.getAttribute('aria-label') === 'Close banner') continue;
            let el = b.parentElement;
            while (el && !el.querySelector('img, a[href*="/item/"]')) el = el.parentElement;
            if (el) out.push(el.outerHTML);
        }
        return out;
    }""")
    log.info(f"Cart has {len(rows)} rows")
    return "\n".join(rows)


def _add_items(page, item_ids, deadline=None):
    """Open each item and click add-to-cart. Returns (added, skipped, reasons).

    Stops early (leaving the rest for later) if `deadline` (monotonic) passes.
    """
    added, skipped, reasons, unknown_shots = [], [], {}, 0
    for item_id in item_ids:
        if deadline and time.monotonic() > deadline:
            log.info(f"Time budget used up — {len(item_ids) - len(added) - len(skipped)} items left for next run.")
            break
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
    return added, skipped, reasons


def _missing_from_cart(page):
    """Return (list_ids, ids on the list that aren't in the cart)."""
    if not _open_favorites(page):
        raise RuntimeError(f"favorites list '{FAVORITES_LIST}' not found")
    list_ids = _collect_item_ids(page)
    if not list_ids:
        page.screenshot(path="error_favorites.png")
        raise RuntimeError(f"List '{FAVORITES_LIST}' opened but no items were found")
    html = _cart_html(page)
    missing = [i for i in list_ids if i not in html]
    log.info(f"List '{FAVORITES_LIST}': {len(list_ids)} items, "
             f"{len(list_ids) - len(missing)} already in cart, {len(missing)} not in cart")
    return list_ids, missing


def _why(reasons: dict) -> str:
    return ", ".join(f"{n} {r}" for r, n in reasons.items())


def run_fill(dry_run: bool = False):
    """Add every available item on the favorites list that isn't already in the cart."""
    log.info(f"Starting FILL session{' (dry run)' if dry_run else ''}…")
    t_start = time.monotonic()
    added, cart_items = [], -1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO)
        ctx = browser.new_context(locale="sv-SE", viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            if not _login(page):
                sheets_log.log_run("failure", "FILL", time.monotonic() - t_start, -1, "login failed")
                return

            list_ids, missing = _missing_from_cart(page)
            log.info(f"Not in cart: {missing}")
            if dry_run:
                log.info("Dry run — not adding anything.")
                return

            added, skipped, reasons = _add_items(page, missing)
            in_cart = len(list_ids) - len(missing)
            cart_items = _cart_count(page)
            log.info(f"FILL done: {len(added)} added, {in_cart} already in cart, "
                     f"{len(skipped)} skipped ({_why(reasons)}), cart now {cart_items}")
            sheets_log.log_run(
                "success",
                f"FILL {FAVORITES_LIST}: {len(added)} added, {len(skipped)} skipped"
                + (f" ({_why(reasons)})" if reasons else "")
                + (f", {in_cart} already in cart" if in_cart else ""),
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
