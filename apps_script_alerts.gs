const ALERT_EMAIL = 'tua@binordic.com';
const ACTIONS_URL = 'https://github.com/Expelliarmus96/window-shopper/actions';

// Actual sheet columns (A–F, plus G once the bot logs errors):
// DateTime | Status | Item_added | Item_removed | Duration_sec | Nr_of_Items | Error
const COL = { TIMESTAMP: 0, STATUS: 1, ITEM_ID: 2, ITEM_REMOVED: 3, DURATION: 4, CART_ITEMS: 5, ERROR: 6 };
const NUM_COLS = 7;
const LOW_CART_THRESHOLD = 20;

// Night window: the bot empties the cart at 01:10 and refills it at 05:40, so a
// low cart in between is expected. No low-cart emails for runs logged in this
// window (Stockholm time). Failure emails are still sent at any hour.
const QUIET_FROM = '01:00';   // inclusive
const QUIET_UNTIL = '05:40';  // exclusive — the fill run logs after this, so it IS checked

// Archive settings
const KEEP_DAYS = 30;              // rows newer than this stay in the main tab
const ARCHIVE_SHEET = 'Archive';   // created automatically, placed last

// The log tab is always the FIRST tab. Keep "Archive" after it, never first.
function getLogSheet_() {
  return SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
}

function checkLatestRun() {
  const sheet = getLogSheet_();
  const lastRow = sheet.getLastRow();

  if (lastRow <= 1) return; // only header row, nothing to check yet

  const width = Math.min(NUM_COLS, sheet.getLastColumn());
  const row = sheet.getRange(lastRow, 1, 1, width).getValues()[0];
  const status    = String(row[COL.STATUS]).trim();
  const timestamp = String(row[COL.TIMESTAMP]).trim();
  const cartItems = row[COL.CART_ITEMS] !== '' && row[COL.CART_ITEMS] !== undefined
    ? Number(row[COL.CART_ITEMS]) : null;

  const props = PropertiesService.getScriptProperties();
  const isFailure = status.toLowerCase() !== 'success';

  // Expected empty cart overnight: ignore low-cart on successful night runs.
  // Return BEFORE touching 'cartIsLow', so the night doesn't count as "already
  // alerted" — if the 05:40 fill leaves the cart low, that still sends an email.
  if (!isFailure && isQuietTime_(row[COL.TIMESTAMP])) return;

  // Low cart only counts on successful runs (failed runs have no count).
  // Alert when the cart DROPS below the threshold, then stay quiet until it
  // has recovered — otherwise a low cart would send an email every 30 min.
  const lowCart = !isFailure && (cartItems === null || cartItems < LOW_CART_THRESHOLD);
  const wasLow = props.getProperty('cartIsLow') === 'true';
  if (!isFailure) props.setProperty('cartIsLow', lowCart ? 'true' : 'false');

  if (!isFailure && (!lowCart || wasLow)) return; // all good, or already alerted

  // Avoid sending duplicate alerts for the same run
  if (props.getProperty('lastAlertedTimestamp') === timestamp) return;

  // Determine alert type
  const subject = isFailure
    ? `⚠️ Window Shopper failed — ${timestamp}`
    : `⚠️ Window Shopper — No items in cart — ${timestamp}`;

  const cartDisplay = cartItems !== null ? cartItems : 'n/a';
  const error = row[COL.ERROR] || 'n/a (not logged yet)';
  const duration = row[COL.DURATION] || 'n/a';
  const itemId = row[COL.ITEM_ID] || 'n/a';

  const body = isFailure
    ? `The Window Shopper run failed and needs attention.

Timestamp:   ${timestamp}
Status:      ${status}
Item ID:     ${itemId}
Duration:    ${duration}s
Cart items:  ${cartDisplay}
Error:       ${error}

View the full run log:
${ACTIONS_URL}`
    : `Window Shopper ran successfully but found fewer than ${LOW_CART_THRESHOLD} items in the cart (it was fine on the previous run).

Timestamp:   ${timestamp}
Cart items:  ${cartDisplay}
Item ID:     ${itemId}
Duration:    ${duration}s

View the full run log:
${ACTIONS_URL}`;

  MailApp.sendEmail(ALERT_EMAIL, subject, body);

  props.setProperty('lastAlertedTimestamp', timestamp);
  Logger.log(`Alert sent for run at ${timestamp} — failure: ${isFailure}, lowCart: ${lowCart}`);
}

function checkBotIsAlive() {
  const sheet = getLogSheet_();
  const lastRow = sheet.getLastRow();

  if (lastRow <= 1) return; // bara header, inget att kolla

  const lastTimestamp = sheet.getRange(lastRow, 1).getValue();
  const lastRun = new Date(lastTimestamp);
  const now = new Date();
  const hoursSinceLastRun = (now - lastRun) / (1000 * 60 * 60);

  // Skicka alert om ingen körning på timme (boten kör var 30e minut)
  if (hoursSinceLastRun < 1) return;

  const props = PropertiesService.getScriptProperties();
  const lastAlive = props.getProperty('lastAliveAlert');

  // Skicka inte mer än en alert per timme
  if (lastAlive && (now - new Date(lastAlive)) / (1000 * 60 * 60) < 1) return;

  MailApp.sendEmail(
    ALERT_EMAIL,
    `⚠️ Window Shopper — No runs detected`,
    `Window Shopper has not run for ${Math.round(hoursSinceLastRun)} hours.

Last recorded run: ${lastTimestamp}

Check GitHub Actions:
${ACTIONS_URL}`
  );

  props.setProperty('lastAliveAlert', now.toISOString());
  Logger.log('No-run alert sent');
}

// ---------------------------------------------------------------------------
// Archiving: moves rows older than KEEP_DAYS from the log tab to "Archive".
// Rows are appended in time order, so old rows are always a block at the top.
// Always keeps at least the latest row, so checkBotIsAlive keeps working even
// if the bot has been down for a long time.
// ---------------------------------------------------------------------------
function archiveOldRows() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30 * 1000)) return; // another run is busy, try next time

  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sheet = getLogSheet_();
    const lastRow = sheet.getLastRow();
    if (lastRow <= 2) return; // header + at most one row, nothing to move

    const cutoff = new Date(Date.now() - KEEP_DAYS * 24 * 60 * 60 * 1000);
    const timestamps = sheet.getRange(2, 1, lastRow - 1, 1).getValues();

    // Count the old rows at the top, but never the very last row
    let oldCount = 0;
    for (let i = 0; i < timestamps.length - 1; i++) {
      const t = parseTimestamp_(timestamps[i][0]);
      if (t === null || t >= cutoff) break;
      oldCount++;
    }
    if (oldCount === 0) {
      Logger.log('Nothing to archive');
      return;
    }

    // Get or create the archive tab (last position, same header)
    let archive = ss.getSheetByName(ARCHIVE_SHEET);
    if (!archive) {
      archive = ss.insertSheet(ARCHIVE_SHEET, ss.getSheets().length);
      const header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues();
      archive.getRange(1, 1, 1, header[0].length).setValues(header);
      archive.setFrozenRows(1);
    }

    // Copy first, delete after, so nothing is lost if the copy fails
    const rows = sheet.getRange(2, 1, oldCount, sheet.getLastColumn()).getValues();
    archive.getRange(archive.getLastRow() + 1, 1, oldCount, rows[0].length).setValues(rows);
    SpreadsheetApp.flush();
    sheet.deleteRows(2, oldCount);

    Logger.log(`Archived ${oldCount} rows older than ${KEEP_DAYS} days`);
  } finally {
    lock.releaseLock();
  }
}

// True if the run's timestamp (Stockholm time) falls in QUIET_FROM–QUIET_UNTIL.
function isQuietTime_(value) {
  let hhmm;
  if (value instanceof Date) {
    hhmm = Utilities.formatDate(value, 'Europe/Stockholm', 'HH:mm');
  } else {
    // Text like "2026-10-14 01:30:05" — already Stockholm time, written by the bot
    const m = String(value).match(/(\d{1,2}):(\d{2})/);
    if (!m) return false;
    hhmm = m[1].padStart(2, '0') + ':' + m[2];
  }
  return hhmm >= QUIET_FROM && hhmm < QUIET_UNTIL;
}

// Handles both real date cells and text like "2026-10-13 11:00:05"
function parseTimestamp_(value) {
  if (value instanceof Date) return value;
  const s = String(value).trim();
  if (!s) return null;
  const d = new Date(s.replace(' ', 'T'));
  return isNaN(d.getTime()) ? null : d;
}

// Run this ONCE by hand to create the monthly archive trigger.
// 1st of every month around 03:00, away from the :00/:30 bot runs.
function installArchiveTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'archiveOldRows')
    .forEach(t => ScriptApp.deleteTrigger(t));

  ScriptApp.newTrigger('archiveOldRows')
    .timeBased()
    .onMonthDay(1)
    .atHour(3)
    .nearMinute(15)
    .create();

  Logger.log('Monthly archive trigger installed');
}