---
name: crstl-po-alert
description: Crstl PO coverage alert routine — searches Gmail for new Crstl "new Purchase Order(s)" emails received since the last routine-output Slack post in #b2b-fulfillment, aggregates ordered units by vendor style, looks up on-hand inventory in the RDZ sheet, and posts a consolidated coverage-status alert. Use when the user runs /crstl-po-alert or asks to run the Crstl PO alert, the PO coverage alert, the Monday/Thursday PO routine, or similar.
---

# Crstl PO Coverage Alert

Aubrey runs this Monday and Thursday mornings (and ad-hoc) to flag B2B
inventory coverage gaps before Target ship windows hit. The routine reads
new Crstl PO emails from Gmail, cross-references the RDZ inventory sheet,
and posts a single consolidated table to Slack #b2b-fulfillment.

## State model

The lookback floor is **the timestamp of the last routine-output Slack
post in #b2b-fulfillment**. No Gmail mutations, no local state file —
the visible Slack post itself is the high-water mark.

Routine-output posts are identified by **content marker**, not by
sender: the first non-empty line begins with `*New POs received — `
(em-dash). This makes the routine robust to running under any
connector identity (the Slack MCP posts as the connected user, not as
a separate bot account).

On each run, the latest matching message in the channel defines the
cutoff; any Crstl PO email with a received timestamp strictly greater
than that cutoff is in scope.

**Fresh-channel / first-run behavior:** if no prior routine-output post
is found in the channel, fall back to "emails received in the last 24
hours" and include a one-line note in the output so it's obvious. The
user can pass an explicit `since:YYYY-MM-DD` to override.

## Arguments

Accepts a free-form argument string. Recognized tokens (any order, all optional):

- `--dry-run` — do everything except post to Slack. Show the final
  assembled message in chat instead so the user can review.
- `since:YYYY-MM-DD` — explicit lookback floor (date at 00:00:00
  America/New_York). Overrides the Slack-derived floor. Useful for
  re-running a specific window or validating against a known set of
  emails.

No arguments → automatic Slack-derived floor.

## Configuration (locked — do not ask the user)

| Setting | Value |
|---|---|
| Gmail account | aubrey@getbiom.co |
| Slack channel ID | C0ARHBQ476D (#b2b-fulfillment) |
| Karina Slack ID | U0A0L7AFUHH |
| Alicia Slack ID | U0AEDDK51PY |
| Neal Slack ID | U047ZL3SCFP |
| Google Sheet ID | 1y3-daeuMeQVkLYRP89go9KfXMjBwfT-_VyzeMqwczD8 |
| Sheet tab | Inventory Summary |
| Vendor Style column | A ("Item #") |
| On-hand column | I ("Qty Remaining (each/unit)") |
| Display timezone | America/New_York |
| Routine-output marker | First non-empty line starts with `*New POs received — ` |

## Procedure

Run the steps in order. If any step fails, stop and report the failure
to the user — do not partially complete the run.

### 1. Determine the lookback floor

**If the user passed `since:YYYY-MM-DD`:** the floor is that date at
00:00:00 America/New_York. Skip to step 2.

**Otherwise, derive the floor from Slack:**

1a. Read recent messages in channel `C0ARHBQ476D` via the Slack
    read-channel tool. Start with limit 100; Slack returns newest
    first. Routine-output posts appear at most twice a week, so 100
    messages typically covers many weeks of history. Only paginate
    further if no marker is found in the first page.

1b. Identify routine-output posts. A message qualifies if its first
    non-empty line begins with `*New POs received — ` (with the
    em-dash `—`, not a hyphen). Do **not** filter by sender — the
    posting identity depends on which Slack identity the connector
    holds.

1c. Take the latest qualifying message (the first match in newest-first
    iteration order). Convert its `ts` field (epoch-seconds string like
    `1779718105.182259`) to a UTC datetime, then to America/New_York.
    This is the floor.

1d. If no qualifying message is found, the floor is "now − 24 hours"
    in America/New_York. Remember this for the report at step 13.

### 2. Find candidate Crstl emails

Build the Gmail query:

```
from:support@crstl.so subject:"new Purchase Order(s)" after:YYYY/MM/DD
```

Where `YYYY/MM/DD` is the floor's date in America/New_York. (Gmail's
`after:` operator is date-granular, so this is intentionally
permissive — the precise per-second filter happens in step 3.)

Page through Gmail search-threads results until exhausted.

### 3. Filter by precise timestamp

Fetch each candidate thread's full content via the Gmail get-thread
tool (`messageFormat: FULL_CONTENT`). For each email, compare its
internal `date` (UTC ISO timestamp) against the floor.

**Drop any email whose received timestamp is less than or equal to the
floor.**

This compensates for Gmail's date-granular `after:` operator: if the
floor is 10:08 AM today and an email arrived at 5:18 AM today, Gmail
returns it (same date) but we exclude it because the routine already
covered it in the prior run.

If zero emails remain after filtering: **exit silently**. Print one
line to the user (e.g., "No new Crstl PO emails since [floor
timestamp] — nothing to post.") and stop. Do not post to Slack.

### 4. Parse each email into a structured PO set

**Vendor (from subject):**
- Subject format: `You have received N new Purchase Order(s) from VENDOR_TEXT`.
- Extract the substring after `from ` to end of subject.
- Map the long form to a short name: strip a trailing parenthetical
  hyphenated qualifier (e.g. ` - Distribution Center (DC)` → drop both
  the dash-clause and the parenthetical → keep `Target`, then re-append
  just the bracketed token → `Target DC`).
  - Concretely: `Target - Distribution Center (DC)` → `Target DC`.
  - For an unknown vendor with no parenthetical, use the raw vendor
    text verbatim. Do not crash on unfamiliar shapes.

**Received timestamp (from email Date header):**
- Convert UTC to America/New_York.
- Format as `H:MM AM/PM ET, M/D/YY` with **no leading zeros** on hour
  or date (so `5:19 AM ET, 5/24/26`, never `05:19` or `5/24/2026`).
- Use timezone-aware logic via zoneinfo-equivalent — handle DST
  naturally, do not hardcode an offset.

**Body table (from htmlBody):**
The Crstl email body is a Postmark HTML table with header row
`PO Number | Amount | Dates | Items`, then one row per individual PO.
Parse the HTML (regex over the cell text is fine for this stable
Postmark template; if any line fails to match, log the line and
continue — don't crash the whole run on a single weird row).

For each PO row:
- **PO Number cell** → text content (strip the wrapping `<a>` tag).
  Format is `NNNNNNNNNNN-NNNN` (11-digit set prefix, dash, 4-digit
  individual). The 11-digit prefix is the **PO set ID**.
- **Dates cell** → extract two dates from
  `Earliest ship: Mon DD, YYYY <br> Latest ship: Mon DD, YYYY`.
- **Items cell** → extract every occurrence of
  `(UPC: ..., Vendor Style: STYLE, Buyer Catalog: ...) : N Units <br>`.
  Pull out `STYLE` and the integer `N`.

Build per-email PO records:
```
{
  vendor: "Target DC",
  po_set_id: "10001901402",
  received_ts: "2026-05-24T05:19:04-04:00",  // ET
  ship_window: {earliest: "2026-05-29", latest: "2026-05-30"},
  lines: [
    {vendor_style: "P-DIS-EUC", qty: 96},
    {vendor_style: "P-DIS-WHI", qty: 12},
    ...
  ]
}
```

### 5. Validate and group by PO set

Within each email:
- Verify all PO numbers share the same 11-digit prefix. If a single
  email contains POs with multiple prefixes, split it into multiple
  sets and emit a warning to the user. (Realistic only as an edge case.)
- Verify all rows in the email share one ship window. If multiple
  windows appear, group sub-tables under that PO set's header — one
  table per distinct window.

### 6. Aggregate within each PO set

Sum `qty` by `vendor_style` across every individual PO row in the set.
Drop per-PO-number detail — the alert is set-level, not row-level.

### 7. Look up on-hand inventory

Call the Drive read-file-content tool on sheet
`1y3-daeuMeQVkLYRP89go9KfXMjBwfT-_VyzeMqwczD8` (this returns the whole
sheet as text; for the Inventory Summary tab the columns are
pipe-rendered).

Build an in-memory map `vendor_style → on_hand_qty` from columns A → I.
The first row is the header row — skip it. For each subsequent row:
- Column A is the vendor style key (text, e.g. `K-60WIP-AP-COM`).
- Column I is the on-hand integer (may contain commas, e.g. `4,126`;
  parse to int by stripping commas).
- If column I is blank, `N/A`, or non-numeric, treat that style's
  on-hand as **missing** (not 0).

### 8. Compute coverage per aggregated line

For each `(po_set, vendor_style, qty_ordered)`:
- on_hand is missing or style not in the map → **⚠️**, and append
  ` (style not found in RDZ sheet)` to the On Hand cell text.
- on_hand >= qty_ordered → **🟢**
- on_hand <  qty_ordered → **🔴**

### 9. Sort lines within each set

Sort descending by `qty_ordered`. Ties: break alphabetically by
vendor_style.

### 10. Sort PO sets within the run

Oldest `received_ts` → newest.

### 11. Assemble the Slack message

Use Slack `mrkdwn` with native Markdown table syntax (Slack supports
this natively as of early 2025; the May 25 reference message in
#b2b-fulfillment confirms it renders cleanly).

**Header line** (one line, then a blank line):

```
*New POs received — Month D, YYYY*
```

Where `Month D, YYYY` is today's date in America/New_York (e.g.
`May 25, 2026` — no leading zero on the day). **This line is the
routine-output marker that the next run will use to find this post —
do not change the prefix `*New POs received — `.**

**Per PO set** (one block per set, separated by one blank line):

```
*Vendor* · PO set PO_SET_ID — Received H:MM AM ET, M/D/YY
Ship window: Mon D – Mon D, YYYY

| Vendor Style | Qty Ordered | On Hand (RDZ) | Coverage |
|---|---:|---:|:---:|
| STYLE_A | 1,234 | 1,500 | 🟢 |
| STYLE_B | 500 | 320 | 🔴 |
```

Notes on punctuation:
- Vendor wrapped in `*...*` (bold in Slack mrkdwn).
- Middle dot `·` between vendor and "PO set" (Unicode U+00B7).
- Em-dash `—` between "PO set X" and "Received ...".
- En-dash `–` between earliest and latest ship dates.
- If `earliest` and `latest` ship are in the same month and year,
  render as `Mon D – Mon D, YYYY` (year only once at the end). The
  May 25 reference uses `May 29 – May 30, 2026` and `Jun 1 – Jun 2, 2026`.
- Numbers use thousands separators (`1,234`).
- Column alignment: Vendor Style left, Qty Ordered + On Hand right,
  Coverage centered — encoded via `|---|---:|---:|:---:|`.

**Footer** (single line, blank line before it):

```
<@U0A0L7AFUHH> <@U0AEDDK51PY> please verify and flag coverage gaps as appropriate. cc <@U047ZL3SCFP>
```

**Total length check:** If the assembled message exceeds ~3,500 chars,
split into multiple Slack posts. The **first** post must carry the
`*New POs received — …*` marker line so the next run's floor detection
works; subsequent posts in the same run can omit the marker.

### 12. Post to Slack (skip if `--dry-run`)

Call the Slack send-message tool with channel `C0ARHBQ476D` and the
assembled message body.

The act of posting IS the state update — once the message is in the
channel with its routine-output marker, the next run will use this
post's timestamp as its floor. No further mutations needed.

### 13. Report to the user

Print a concise summary in chat:
- **Floor used** and how it was derived:
  - "From previous routine post at H:MM AM ET, M/D/YY" (Slack-derived);
  - "From `since:YYYY-MM-DD` argument";
  - "First run — defaulted to last 24h (no prior routine post found)".
- Number of emails processed (after the precise-timestamp filter).
- Number of PO sets.
- Number of total line rows.
- Number of 🔴 and ⚠️ rows.
- For `--dry-run`: the full message body that would have been posted,
  fenced in a code block.

## Edge cases

- **No prior routine-output post in channel** → fall back to last 24h,
  flag in report.
- **Slack channel read returns nothing** → treat as first-run (24h fallback).
- **Multiple routine-output posts on the same day** → use the latest by
  `ts`. Correct behavior — the most recent post defines the floor.
- **Someone deletes a routine-output post** → next run uses the
  second-most-recent matching post as the floor, which may cause
  re-posting of emails covered by the deleted post. Acceptable
  trade-off — visible state means visible failure modes.
- **Multiple ship windows in one email** → multiple sub-tables under
  one set header, one per window.
- **PO numbers without a common prefix in one email** → split into
  multiple sets, emit a warning, post each as its own block.
- **Vendor not Target** → use the subject's verbatim vendor string if
  the short-name heuristic doesn't apply.
- **Vendor style missing from RDZ sheet** → ⚠️ +
  `(style not found in RDZ sheet)`. Do not skip the row.
- **On-hand cell blank / "N/A" / non-numeric** → treat as missing (⚠️).
- **Slack 4000-char block limit** → split per PO set, marker on first
  post only.

## Acceptance test

Run with `since:2026-05-24 --dry-run`. The output should contain three
PO sets, in this order, with these aggregated quantities:

- `10001901402` (Target DC, Received 5:19 AM ET, 5/24/26, ship May 29 – May 30, 2026).
  Top line: `P-DIS-EUC` with `3,486 Qty Ordered`.
- `10001902323` (Target DC, Received 5:18 AM ET, 5/25/26, ship Jun 1 – Jun 2, 2026).
- `10001903266` (Target DC, Received 5:23 AM ET, 5/25/26, ship May 30 – May 31, 2026).

Match the structure, ordering, and per-set aggregates of the May 25
reference message in #b2b-fulfillment exactly. The On Hand and Coverage
columns of that reference are filler; only structure/ordering/totals
need to match.
