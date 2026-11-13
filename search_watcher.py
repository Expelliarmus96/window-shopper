"""
search_watcher.py
Watches Sellpy searches and puts newly listed items into one favorites list.

The searches live in the "Search watcher" tab of the log spreadsheet, one per
row (column A = search URL). The tab is created automatically, after the log
tab, the first time the script runs. URLs listed in searches.txt are appended
to the tab on each run if they aren't there yet (existing rows are never
changed, moved or deleted).

For each search, sorted by "latest" (sortBy=saleStartedAt_desc), first page:
  * Each item's first-listed time (firstOfferedAt_SE) is read from the
    search's own data. Sellpy's "latest" order is not strictly by date, so
    position in the list is not used to decide what is new.
  * New = first listed after the newest listing time stored for the search
    (column newest_listed_at), wherever it sits in the list. It gets
    favorited and put in the list (WATCH_LIST_URL), oldest first, at most
    15 per search per run. Items that come back on sale (e.g. after a cart
    reservation runs out) keep their old listing time and are not new.
  * The newest listing time and the 5 newest ids are then stored. The ids
    are a backup for items without a listing time, and for when no times
    can be read at all: then only items above the first stored id still
    listed count, and nothing is added if none of them is listed any more.
  * A search with nothing stored (first run) is only recorded.
  * A row stored before listing times existed: items listed after its
    last_checked time are new; the newest listing time is then stored.
  * A search Sellpy confirms is empty stores "(empty)" and the current time,
    so whatever is first listed after that counts as new.

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
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

# Reuse login, banner handling and list reading from the cart bot.
from window_shopper import (
    BASE, EMAIL, HEADLESS, PASSWORD, SLOW_MO,
    _ITEM_LINKS_JS, _close_banner, _collect_item_ids, _harvest, _login, _wait_rendered,
)

log = logging.getLogger("search_watcher")

LIST_URL = (os.getenv("WATCH_LIST_URL", "").strip()
            or f"{BASE}/my-shopping/favorites/list/F4ykSR5Ys0")
LIST_NAME = os.getenv("WATCH_LIST_NAME", "").strip()   # empty = read it from the list page

SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
CREDS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
TAB = "Search watcher"
HEADER = ["search_url", "label", "latest_ids", "last_checked", "last_status",
          "last_added", "total_added", "newest_listed_at"]
SEED_SEARCHES = [["https://www.sellpy.se/search?query=brighten", "brighten"]]

MARKERS = 5             # newest ids remembered per search
MAX_NEW_PER_SEARCH = 15 # safety cap per search per run
SORT = "saleStartedAt_desc"
EMPTY = "(empty)"       # latest_ids marker: Sellpy confirmed the search had no items
# When an item was first put on sale on sellpy.se (ms since epoch), from the
# search's Algolia response. Unlike saleStartedAt it is NOT reset when an item
# comes back on sale (e.g. after a cart reservation runs out).
LISTED_FIELD = "firstOfferedAt_SE"
_UTC = datetime.timezone.utc
_STHLM = ZoneInfo("Europe/Stockholm")


# ── Google Sheet ──────────────────────────────────────────────────────────────

def _open_tab(create: bool = True):
    """Return the tab, creating it if needed. With create=False a missing tab
    gives None (a dry run must not touch the sheet)."""
    import gspread
    if not CREDS_JSON or not SHEET_ID:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON / GOOGLE_SHEET_ID not set")
    sh = gspread.service_account_from_dict(json.loads(CREDS_JSON)).open_by_key(SHEET_ID)
    try:
        return sh.worksheet(TAB)
    except gspread.WorksheetNotFound:
        if not create:
            return None
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


def _ensure_columns(ws):
    """Tabs created before newest_listed_at existed have 7 columns: add the
    missing column(s) and header cells. Existing cells are not touched."""
    if ws.col_count < len(HEADER):
        ws.add_cols(len(HEADER) - ws.col_count)
    head = ws.row_values(1)
    if len(head) < len(HEADER):
        first = len(head)
        ws.update(range_name=f"{chr(65 + first)}1", values=[HEADER[first:]])


def _write_result(ws, row_no, rec):
    ws.update(range_name=f"C{row_no}:H{row_no}", values=[[
        rec["latest_ids"], rec["last_checked"], rec["last_status"],
        rec["last_added"], rec["total_added"], rec["newest_listed_at"],
    ]])


SEARCHES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "searches.txt")


def _url_key(url: str) -> str:
    """Identity of a search: host (any case), decoded path without trailing
    slash, and the decoded filters in any order. page and sortBy are left
    out because the bot always sets its own sort and reads page 1."""
    parts = urlparse(url.strip())
    q = sorted((k, v) for k, vs in parse_qs(parts.query).items() for v in vs
               if k not in ("page", "sortBy"))
    return f"{parts.netloc.lower()}{unquote(parts.path).rstrip('/')}?{urlencode(q)}"


def _file_searches() -> list:
    """[url, label] pairs from searches.txt ("url | label" per line, # = comment)."""
    if not os.path.exists(SEARCHES_FILE):
        return []
    out = []
    with open(SEARCHES_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            url, _, label = line.partition("|")
            if url.strip().startswith("http"):
                out.append([url.strip(), label.strip()])
    return out


def _sync_file_searches(ws, dry_run: bool) -> list:
    """Append searches.txt URLs that aren't in the tab yet, below the last
    used row. Never touches existing rows. Returns the [url, label] pairs
    that were (or, in a dry run, would be) added."""
    rows = ws.get_all_values()
    have = {_url_key(r[0]) for r in rows[1:] if r and r[0].strip()}
    new, seen = [], set()
    for url, label in _file_searches():
        key = _url_key(url)
        if key not in have and key not in seen:
            seen.add(key)
            new.append([url, label])
    if not new:
        return []
    for url, label in new:
        log.info(f"searches.txt: {'would add' if dry_run else 'adding'} '{label or url}'")
    if not dry_run:
        first = len(rows) + 1
        last = first + len(new) - 1
        if last > ws.row_count:
            ws.add_rows(last - ws.row_count)
        ws.update(range_name=f"A{first}:B{last}", values=new)
    return new


# ── Sellpy ────────────────────────────────────────────────────────────────────

def _sorted_url(url: str) -> str:
    parts = urlparse(url.strip())
    q = parse_qs(parts.query)
    q["sortBy"] = [SORT]
    q.pop("page", None)
    return urlunparse(parts._replace(query=urlencode(q, doseq=True)))


def _listing_times(responses, ids: list) -> dict:
    """{item_id: first-listed ms} from the Algolia responses the search page
    made. Picks the result whose hits best match the tiles we read."""
    best, best_overlap = None, 0
    for r in responses:
        try:
            data = r.json()
        except Exception:
            continue
        for res in data.get("results", []):
            hits = res.get("hits") or []
            overlap = len({h.get("objectID") for h in hits} & set(ids))
            if overlap > best_overlap:
                best, best_overlap = hits, overlap
    times = {}
    for h in best or []:
        t = h.get(LISTED_FIELD)
        if h.get("objectID") in ids and isinstance(t, (int, float)) and t > 0:
            times[h["objectID"]] = int(t)
    return times


def _search_ids(page, url: str):
    """(item ids of a search in page order, {id: first-listed ms}).
    First page only (up to 60 items); ids without a known time are left out
    of the dict."""
    responses = []

    def grab(r):
        if "algolia" in r.url and "/queries" in r.url:
            responses.append(r)

    page.on("response", grab)
    try:
        page.goto(_sorted_url(url), wait_until="load", timeout=60_000)
        _wait_rendered(page)
        _close_banner(page)
        ids = []
        _wait_own_tiles(page)
        _harvest(page, ids, want=30)
    finally:
        page.remove_listener("response", grab)
    return ids, _listing_times(responses, ids)


def _confirmed_empty(page) -> bool:
    """True if the search page shows Sellpy's own "no items" message (as
    opposed to a page that just didn't load)."""
    try:
        return page.get_by_text("Inga fler varor med dina filter").first.is_visible(timeout=2_000)
    except PWTimeout:
        return False


def _wait_own_tiles(page, timeout: int = 15_000):
    """Wait until tiles that are NOT in the recommendation carousel show up.
    (_wait_rendered is satisfied by the carousel alone, which can load before
    the list or search results themselves.) An empty page just times out."""
    try:
        page.wait_for_function(f"({_ITEM_LINKS_JS})().length > 0", timeout=timeout)
    except PWTimeout:
        pass


def _list_item_ids(page) -> list:
    """Item ids in the watch list, across all its pages (carousel excluded)."""
    page.goto(LIST_URL, wait_until="load", timeout=60_000)
    _wait_own_tiles(page)
    return _collect_item_ids(page)


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
    # Toast: "… sparad till Alla favoriter. Gå till favoriter eller lägg
    # favoriten i en egen [lista]" — the button's text is just "lista".
    choose = page.get_by_role("button", name="lista", exact=True).first
    try:
        choose.wait_for(state="visible", timeout=8_000)
    except PWTimeout:
        _debug_dump(page, f"notoast_{item_id}")
        return "favorited, but the toast's 'lista' button didn't appear"
    choose.click()

    # Dialog "Välj lista": one row per list, <p>name</p> + a heart button
    # (aria-label "Lägg till i favoriter"). Names can have trailing spaces.
    dialog = page.get_by_role("dialog", name="Välj lista").last
    name_re = re.compile(rf"^\s*{re.escape(list_name.strip())}\s*$")
    try:
        dialog.wait_for(state="visible", timeout=8_000)
        row_name = dialog.locator("p", has_text=name_re).first
        row_name.wait_for(state="visible", timeout=8_000)
    except PWTimeout:
        _debug_dump(page, f"nolist_{item_id}")
        return f"favorited, but list '{list_name}' not found in the chooser"
    heart = row_name.locator("xpath=..").locator("button:has([data-testid^='icon-HEART'])").first
    if not heart.locator("[data-testid='icon-HEART_FILLED']").count():
        heart.click()
        try:
            heart.locator("[data-testid='icon-HEART_FILLED']").wait_for(state="attached", timeout=8_000)
        except PWTimeout:
            _debug_dump(page, f"notadded_{item_id}")
            return f"favorited, but adding to '{list_name}' wasn't confirmed"

    close = dialog.get_by_role("button", name="Stäng", exact=True).first
    try:
        close.click(timeout=3_000)
    except PWTimeout:
        page.keyboard.press("Escape")
    page.wait_for_timeout(1_000)
    return "ok"


# ── What counts as new ────────────────────────────────────────────────────────

def _fmt_ms(ms) -> str:
    if ms is None:
        return ""
    dt = datetime.datetime.fromtimestamp(ms / 1000, _UTC)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_ms(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return int(dt.timestamp() * 1000)


def _parse_checked_ms(text):
    """last_checked is written as Stockholm local time."""
    try:
        dt = datetime.datetime.strptime((text or "").strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return int(dt.replace(tzinfo=_STHLM).timestamp() * 1000)


def _decide(ids, times, rec, now_ms, empty_confirmed=False):
    """Decide what is new in one search. Pure function (no browser, no sheet).

    ids    item ids in page order; times {id: first-listed ms} (may be partial)
    rec    the search's row (latest_ids, newest_listed_at, last_checked, ...)
    Returns (new ids oldest first, row updates, status, log level, message).
    status is None when the caller should go on and add `new`.

    New = first listed after the stored newest listing time, wherever the
    item sits in the list. Items that come back on sale keep their old
    listing time, so they never count. The stored ids are a backup, for
    items without a listing time.
    """
    markers = [m.strip() for m in (rec.get("latest_ids") or "").split(",") if m.strip()]
    real_markers = [m for m in markers if m != EMPTY]
    stored = _parse_ms(rec.get("newest_listed_at"))
    known = [times[i] for i in ids if i in times]
    newest = max(known) if known else None
    top = ",".join(ids[:MARKERS])

    def latest(*vals):
        vals = [v for v in vals if v is not None]
        return _fmt_ms(max(vals)) if vals else ""

    # Nothing listed.
    if not ids:
        if not empty_confirmed:
            return [], {}, "no results", "warning", "no results — keeping the stored point."
        # Sellpy itself says the search is empty: anything listed from now on is new.
        status = ("baseline recorded (no items yet)" if not (markers or stored)
                  else "no items listed")
        return ([], {"latest_ids": EMPTY, "newest_listed_at": latest(stored, now_ms)}, status,
                "info", "no items listed (confirmed) — anything listed from now on counts as new.")

    # Very first run: only record.
    if not markers and stored is None:
        return ([], {"latest_ids": top, "newest_listed_at": latest(newest), "last_added": ""},
                "baseline recorded", "info",
                f"first run — recording newest listing {_fmt_ms(newest) or '(unknown)'}, nothing added.")

    # Where "new" starts.
    if stored is not None:
        cutoff, how = stored, "listed after the stored newest listing"
    else:
        # A row from before listing times were stored: what was listed at the
        # previous check is not new, so new starts at that check.
        cutoff, how = _parse_checked_ms(rec.get("last_checked")), "listed after the previous check"
        if cutoff is None:
            return ([], {"latest_ids": top, "newest_listed_at": latest(newest)},
                    "listing time recorded, nothing added", "info",
                    "no stored listing time or previous check — recording the newest listing, nothing added.")

    hit = next((i for i, x in enumerate(ids) if x in real_markers), None)
    if not known:
        # No listing times at all (Algolia response missing): fall back to the
        # stored ids, the old way — only what is above them is new.
        if hit is None:
            return ([], {"latest_ids": top}, "no listing times and stored ids gone — nothing added",
                    "warning", "no listing times and none of the stored ids are listed — nothing added.")
        new, how, level = ids[:hit], "no listing times — used the stored ids", "warning"
    else:
        timed_new = sorted((i for i in ids if i in times and times[i] > cutoff),
                           key=lambda i: -times[i])                      # newest first
        # Backup for items without a listing time: new only if above the
        # first stored id that is still listed.
        untimed_new = [i for i in ids[:hit or 0] if i not in times]
        new, level = untimed_new + timed_new, "info"

    updates = {"latest_ids": top, "newest_listed_at": latest(stored, newest)}
    if len(new) > MAX_NEW_PER_SEARCH:
        how += f"; {len(new)} new — only adding the newest {MAX_NEW_PER_SEARCH}"
        level = "warning"
        new = new[:MAX_NEW_PER_SEARCH]
    return list(reversed(new)), updates, None, level, how


# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run: bool) -> bool:
    """Returns True if everything went fine."""
    t0 = time.monotonic()
    ws = _open_tab(create=not dry_run)
    if ws is None:
        # Dry run before the tab exists: check the starter searches in memory.
        log.info(f"No '{TAB}' tab yet — dry run uses the starter search(es); the sheet is not touched.")
        searches = [(i, dict(zip(HEADER, r + [""] * (len(HEADER) - len(r)))))
                    for i, r in enumerate(SEED_SEARCHES, start=2)]
    else:
        if not dry_run:
            _ensure_columns(ws)
        added = _sync_file_searches(ws, dry_run)
        searches = _read_searches(ws)
        if dry_run and added:
            # Not written in a dry run: check them in memory as first runs.
            nxt = max([r for r, _ in searches], default=1) + 1
            searches += [(nxt + i, dict(zip(HEADER, r + [""] * (len(HEADER) - 2))))
                         for i, r in enumerate(added)]
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
                try:
                    ids, times = _search_ids(page, rec["search_url"])
                except Exception as exc:
                    ok_all = False
                    log.error(f"[{label}] search failed: {exc}")
                    _debug_dump(page, f"search_row{row_no}")
                    rec.update(last_checked=now, last_status=f"error: {exc}"[:200])
                    results[row_no] = rec
                    continue

                new, updates, status, level, msg = _decide(
                    ids, times, rec, int(time.time() * 1000),
                    empty_confirmed=(not ids and _confirmed_empty(page)))
                if ids and len(times) < len(ids):
                    log.info(f"[{label}] listing time known for {len(times)}/{len(ids)} items.")
                if status is not None:                     # nothing to add this run
                    getattr(log, level)(f"[{label}] {msg}")
                    if not dry_run:
                        rec.update(updates)
                    rec.update(last_checked=now, last_status=status)
                    results[row_no] = rec
                    continue
                getattr(log, level)(f"[{label}] {len(new)} new ({msg})" + (":" if new else "."))
                for item_id in new:
                    log.info(f"[{label}]   {item_id}  listed {_fmt_ms(times.get(item_id)) or '(time unknown)'}"
                             f"  position {ids.index(item_id) + 1}")

                done, problems = [], []
                for item_id in new:                      # oldest first
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
                    rec.update(updates)
                    rec["total_added"] = str(int(rec["total_added"] or 0) + len(done))
                results[row_no] = rec

            # Verify the added items really are in the list.
            if added_all:
                in_list = set(_list_item_ids(page))
                missing = [(r, i) for r, i in added_all if i not in in_list]
                if missing:
                    # The list can lag right after a change: look once more.
                    log.info(f"{len(missing)} added item(s) not in the list yet — checking again in 10 s.")
                    page.wait_for_timeout(10_000)
                    in_list |= set(_list_item_ids(page))
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
