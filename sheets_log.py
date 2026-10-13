"""
Appends one run record to a Google Sheet via the Sheets API using a Service Account.
Requires GOOGLE_CREDENTIALS_JSON and GOOGLE_SHEET_ID env vars.
Silently skips if either is missing so the bot never crashes because of this.
"""
import datetime
import json
import logging
import os
from zoneinfo import ZoneInfo

_STOCKHOLM = ZoneInfo("Europe/Stockholm")

import gspread

log = logging.getLogger(__name__)

CREDS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
HEADER = ["timestamp_stockholm", "status", "item_id_added", "item_id_removed", "duration_seconds", "cart_items", "error"]


def log_run(status: str, item_id: str, duration_seconds: float, cart_items: int = -1, error: str = "", removed_item_id: str = "") -> None:
    if not CREDS_JSON or not SHEET_ID:
        log.debug("Google Sheets logging skipped — credentials not set.")
        return

    try:
        gc = gspread.service_account_from_dict(json.loads(CREDS_JSON))
        ws = gc.open_by_key(SHEET_ID).sheet1

        if not ws.get_all_values():
            ws.append_row(HEADER)

        ts = datetime.datetime.now(_STOCKHOLM).strftime("%Y-%m-%d %H:%M:%S")
        cart_str = cart_items if cart_items >= 0 else ""
        ws.append_row([ts, status, item_id, removed_item_id, round(duration_seconds, 1), cart_str, error])
        log.info(f"Run logged to Google Sheets: {status}, added={item_id}, removed={removed_item_id}, {duration_seconds:.1f}s, cart_items={cart_str}")
    except Exception as exc:
        log.warning(f"Google Sheets logging failed (non-fatal): {exc}")
