"""
search_watcher.py
Watches Sellpy searches and puts newly listed items into one favorites list.

The searches live in the "Search watcher" tab of the log spreadsheet, one per
row (column A = search URL). The tab is created automatically, after the log
tab, the first time the script runs.

For each search, sorted by "latest" (sortBy=saleStartedAt_desc):
  * The tab remembers the ids of the 5 newest items from the previous run.
  * Everything above the first of those that is still listed is new: it gets
    favorited and put in the list (WATCH_LIST_URL).
  * The 5 newest ids are then stored for next time.
  * A search with no stored ids (first run) is only recorded, nothing is added.
  * If none of the stored ids is listed any more, nothing is added and the
    search is re-recorded, so a glitch can never favorite a whole search.

Afterwards the list is opened to verify every added item is really in it.

Usage: python search_watcher.py [--dry-run]
(DRY_RUN=true works too.) Exits with code 1 if anything failed, so the
GitHub Actions run turns red.
"""
import argparse
import datetime
import json
import logging
import os
import re
import sys
import time
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

# Reuse login, banner handling and list reading from the cart bot.
from window_shopper import (
    BASE, EMAIL, HEADLESS, PASSWORD, SLOW_MO,
    _close_banner, _collect_item_ids, _login,
)

log = logging.getLogger("search_watcher")

LIST_URL = (os.getenv("WATCH_LIST_URL", "").strip()
            or f"{BASE}/my-shopping/favorites/list/F4ykSR5Ys0")
LIST_NAME = os.getenv("WATCH_LIST_NAME", "").strip()   # empty = read it from the list page

SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
CREDS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
TAB = "Search watcher"
HEADER = ["search_url", "label", "latest_ids", "last_checked", "last_status",
          "last_added", "total_added"]
SEED_SEARCHES = [["https://www.sellpy.se/search?query=brighten", "brighten"]]

MARKERS = 5             # newest ids remembered per search
MAX_NEW_PER_SEARCH = 15 # safety cap per search per run
SORT = "saleStartedAt_desc"
_STHLM = ZoneInfo("Europe/Stockholm")


# ── Google Sheet ──────────────────────────────────────────────────────────────

def _open_tab():
    import gspread
    if not CREDS_JSON or not SHEET_ID:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON / GOOGLE_SHEET_ID not set")
    sh = gspread.service_account_from_dict(json.loads(CREDS_JSON)).open_by_key(SHEET_ID)
    try:
        return sh.worksheet(TAB)
    except gspread.WorksheetNotFound:
        # Always after the existing tabs: the Apps Script reads the FIRST tab
        # as the cart-bot log.
        ws = sh.add_worksheet(TAB, rows=100, cols=len(HEADER), index=len(sh.worksheets()))
        ws.update(range_name="A1", values=[HEADER] + [r + [""] * (len(HEADER) - 2) for r in SEED_SEARCHES])
        ws.freeze(rows=1)
        log.info(f"Created tab '{TAB}' with {len(SEED_SEARCHES)} starter search(es).")
        return ws


def _read_searches(ws):
    """Return [(row_number, dict)] for every row with a search URL."""
    rows = ws.get_all_values()
    out = []
    for i, row in enumerate(rows[1:], start=2):
        row = row + [""] * (len(HEADER) - len(row))
        rec = dict(zip(HEADER, row))
        if rec["search_url"].strip().startswith("http"):
            out.append((i, rec))
    return out


def _write_result(ws, row_no, rec):
    ws.update(range_name=f"C{row_no}:G{row_no}", values=[[
        rec["latest_ids"], rec["last_checked"], rec["last_status"],
        rec["last_added"], rec["total_added"],
    ]])


# ── Sellpy ────────────────────────────────────────────────────────────────────

def _sorted_url(url: str) -> str:
    parts = urlparse(url.strip())
    q = parse_qs(parts.query)
    q["sortBy"] = [SORT]
    q.pop("page", None)
    return urlunparse(parts._replace(query=urlencode(q, doseq=True)))


def _search_ids(page, url: str) -> list:
    """Item ids of a search, newest first (first page only)."""
    page.goto(_sorted_url(url), wait_until="load", timeout=60_000)
    page.wait_for_timeout(3_000)
    _close_banner(page)
    ids = []
    for _ in range(4):                        # a little scrolling for lazy tiles
        for href in page.eval_on_selector_all(
            "main article a[href*='/item/']", "els => els.map(e => e.getAttribute('href'))"
        ):
            item_id = href.rstrip("/").split("/item/")[-1].split("?")[0]
            if item_id and item_id not in ids:
                ids.append(item_id)
        if len(ids) >= 30:
            break
        page.mouse.wheel(0, 2_500)
        page.wait_for_timeout(700)
    return ids


def _list_name(page) -> str:
    """Read the favorites list's name from its page."""
    page.goto(LIST_URL, wait_until="load", timeout=60_000)
    page.wait_for_timeout(3_000)
    _close_banner(page)
    if "/login" in page.url:
        raise RuntimeError("list page redirected to login")
    for sel in ["main h1", "main h2", "h1"]:
        loc = page.locator(sel).first
        try:
            if loc.is_visible(timeout=2_000):
                name = loc.inner_text().strip()
                if name:
                    return name
        except PWTimeout:
            pass
    raise RuntimeError("couldn't read the list name — set WATCH_LIST_NAME")


def _debug_dump(page, tag: str):
    try:
        page.screenshot(path=f"watch_{tag}.png", full_page=True)
        with open(f"watch_{tag}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass


def _favorite_to_list(page, item_id: str, list_name: str) -> str:
    """Favorite one item and put it in the list. Returns 'ok' or a reason."""
    page.goto(f"{BASE}/item/{item_id}", wait_until="load", timeout=60_000)
    page.wait_for_timeout(2_000)
    _close_banner(page)

    # The item's own favorite button ("Spara som favorit"); the similar-item
    # tiles further down also have hearts, so match on the text.
    fav = page.locator("main button:has([data-testid^='icon-HEART'])").filter(
        has_text=re.compile("favorit", re.I)).first
    try:
        fav.wait_for(state="visible", timeout=8_000)
    except PWTimeout:
        _debug_dump(page, f"nobutton_{item_id}")
        return "no favorite button"
    if fav.locator("[data-testid='icon-HEART_FILLED']").count():
        # Already a favorite. Un- and re-favoriting would drop it from other
        # lists, so leave it and report it.
        return "already a favorite (not moved)"

    fav.click()
    choose = page.get_by_role("button", name="Välj lista").first
    try:
        choose.wait_for(state="visible", timeout=8_000)
    except PWTimeout:
        _debug_dump(page, f"notoast_{item_id}")
        return "favorited, but 'Välj lista' didn't appear"
    choose.click()

    dialog = page.get_by_role("dialog").last
    try:
        dialog.wait_for(state="visible", timeout=8_000)
        target = dialog.get_by_text(list_name, exact=True).first
        target.wait_for(state="visible", timeout=8_000)
    except PWTimeout:
        _debug_dump(page, f"nolist_{item_id}")
        return f"favorited, but list '{list_name}' not found in the chooser"
    target.click()
    page.wait_for_timeout(1_500)

    for name in ["Spara", "Klar", "Stäng"]:
        btn = dialog.get_by_role("button", name=name, exact=True).first
        try:
            if btn.is_visible(timeout=1_000):
                btn.click()
                page.wait_for_timeout(1_000)
                break
        except PWTimeout:
            pass
    else:
        page.keyboard.press("Escape")
    return "ok"


# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run: bool) -> bool:
    """Returns True if everything went fine."""
    t0 = time.monotonic()
    ws = _open_tab()
    searches = _read_searches(ws)
    log.info(f"{len(searches)} search(es) to check{' (dry run)' if dry_run else ''}.")
    if not searches:
        return True

    ok_all = True
    added_all = []           # (row_no, item_id)
    now = datetime.datetime.now(_STHLM).strftime("%Y-%m-%d %H:%M:%S")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO)
        ctx = browser.new_context(locale="sv-SE", viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            if not _login(page):
                raise RuntimeError("login failed")
            list_name = LIST_NAME or _list_name(page)
            log.info(f"Target list: '{list_name}' ({LIST_URL})")

            results = {}
            for row_no, rec in searches:
                label = rec["label"] or rec["search_url"]
                markers = [m for m in rec["latest_ids"].split(",") if m.strip()]
                try:
                    ids = _search_ids(page, rec["search_url"])
                except Exception as exc:
                    ok_all = False
                    log.error(f"[{label}] search failed: {exc}")
                    _debug_dump(page, f"search_row{row_no}")
                    rec.update(last_checked=now, last_status=f"error: {exc}"[:200])
                    results[row_no] = rec
                    continue

                if not ids:
                    log.warning(f"[{label}] no results — keeping the stored ids.")
                    rec.update(last_checked=now, last_status="no results")
                    results[row_no] = rec
                    continue

                hit = next((i for i, x in enumerate(ids) if x in markers), None)
                if not markers:
                    log.info(f"[{label}] first run — recording {ids[:MARKERS]}, nothing added.")
                    rec.update(latest_ids=",".join(ids[:MARKERS]), last_checked=now,
                               last_status="baseline recorded", last_added="")
                    results[row_no] = rec
                    continue
                if hit is None:
                    log.warning(f"[{label}] none of the stored ids are listed any more — "
                                "re-recording, nothing added.")
                    rec.update(latest_ids=",".join(ids[:MARKERS]), last_checked=now,
                               last_status="stored ids gone — re-recorded, nothing added")
                    results[row_no] = rec
                    continue

                new = ids[:hit]
                if len(new) > MAX_NEW_PER_SEARCH:
                    log.warning(f"[{label}] {len(new)} new — only adding the newest {MAX_NEW_PER_SEARCH}.")
                    new = new[:MAX_NEW_PER_SEARCH]
                log.info(f"[{label}] {len(new)} new: {new}")

                done, problems = [], []
                for item_id in reversed(new):            # oldest first
                    if dry_run:
                        done.append(item_id)
                        continue
                    try:
                        res = _favorite_to_list(page, item_id, list_name)
                    except Exception as exc:
                        res = f"error: {exc}"
                        _debug_dump(page, f"error_{item_id}")
                    log.info(f"[{label}] {item_id}: {res}")
                    if res == "ok":
                        done.append(item_id)
                        added_all.append((row_no, item_id))
                    else:
                        problems.append(f"{item_id}: {res}")

                if problems:
                    ok_all = False
                status = (("dry run: would add " if dry_run else "added ") + f"{len(done)}"
                          if new else "nothing new")
                if problems:
                    status += f"; {len(problems)} failed — " + " | ".join(problems)
                rec.update(last_checked=now, last_status=status[:500])
                if new:
                    rec["last_added"] = ",".join(done)
                if not dry_run:
                    rec["latest_ids"] = ",".join(ids[:MARKERS])
                    rec["total_added"] = str(int(rec["total_added"] or 0) + len(done))
                results[row_no] = rec

            # Verify the added items really are in the list.
            if added_all:
                page.goto(LIST_URL, wait_until="load", timeout=60_000)
                page.wait_for_timeout(3_000)
                _close_banner(page)
                in_list = set(_collect_item_ids(page))
                missing = [(r, i) for r, i in added_all if i not in in_list]
                if missing:
                    ok_all = False
                    log.error(f"Not found in the list after adding: {[i for _, i in missing]}")
                    for r, i in missing:
                        results[r]["last_status"] += f"; {i} NOT in list after adding"
                else:
                    log.info(f"Verified: all {len(added_all)} added item(s) are in the list.")
        except Exception as exc:
            ok_all = False
            log.error(f"Run failed: {exc}", exc_info=True)
            _debug_dump(page, "run_error")
            for row_no, rec in searches:
                rec.update(last_checked=now, last_status=f"run failed: {exc}"[:200])
            results = dict(searches)
        finally:
            browser.close()

    if dry_run:
        for row_no, rec in results.items():
            log.info(f"[dry run] row {row_no}: {rec['last_status']}")
    else:
        for row_no, rec in results.items():
            _write_result(ws, row_no, rec)
    log.info(f"Done in {time.monotonic() - t0:.0f}s — {'OK' if ok_all else 'with problems'}.")
    return ok_all


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        default=os.getenv("DRY_RUN", "").strip().lower() == "true")
    args = parser.parse_args()
    if not EMAIL or not PASSWORD:
        log.error("SELLPY_EMAIL / SELLPY_PASSWORD not set — aborting.")
        sys.exit(1)
    sys.exit(0 if run(args.dry_run) else 1)


if __name__ == "__main__":
    main()
