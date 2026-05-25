---
name: crstl-po-alert
description: Crstl PO coverage alert routine — searches Gmail for new Crstl "new Purchase Order(s)" emails since the last run, aggregates ordered units by vendor style, looks up on-hand inventory in the RDZ sheet, and posts a consolidated coverage-status alert to #b2b-fulfillment. Use when the user runs /crstl-po-alert or asks to run the Crstl PO alert, the PO coverage alert, the Monday/Thursday PO routine, or similar.
---

# Crstl PO Coverage Alert

Aubrey runs this Monday and Thursday mornings (and ad-hoc) to flag B2B
inventory coverage gaps before Target ship windows hit. The routine reads
new Crstl PO emails from Gmail, cross-references the RDZ inventory sheet,
and posts a single consolidated table to Slack #b2b-fulfillment.

## Arguments

Accepts a free-form argument string. Recognized tokens (any order, all optional):

- `--dry-run` — do everything except post to Slack and apply Gmail labels.
  Show the final assembled Slack message in chat instead so the user can review.
- `since:YYYY-MM-DD` — bound the Gmail search to emails received after this
  date (`after:YYYY/MM/DD` in Gmail syntax). Useful for first-run or for
  re-running a specific window. Without this token, all unprocessed Crstl
  emails are picked up (state = the `processed/crstl-po-alert` Gmail label).

If the user provides no arguments, run live (real Slack post, real labels)
across all unprocessed Crstl emails.

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
| Gmail processed label | processed/crstl-po-alert |
| Display timezone | America/New_York |

## Procedure

Run the steps in order. If any step fails, stop and report the failure
to the user — do not partially complete the run.

### 1. Find unprocessed Crstl emails

Build the Gmail query:

```
from:support@crstl.so subject:"new Purchase Order(s)" -label:processed/crstl-po-alert
```

If the user passed `since:YYYY-MM-DD`, append `after:YYYY/MM/DD`.

Call the Gmail search-threads tool. Page through results until exhausted
(pageSize 50 per page is fine). Collect every matching thread ID.

If zero threads match: **exit silently**. Print one line to the user
("No unprocessed Crstl PO emails found — nothing to post.") and stop.
Do not post anything to Slack. Do not create or apply the Gmail label.

### 2. Fetch each thread's full content

For each thread, call the Gmail get-thread tool with `messageFormat: FULL_CONTENT`.
Crstl threads are single-message. Capture, per email:
- The internal `date` field (UTC ISO timestamp — this is when the email arrived).
- The `subject`.
- The `htmlBody`.

### 3. Parse each email into a structured PO set

**Vendor (from subject):**
- Subject format: `You have received N new Purchase Order(s) from VENDOR_TEXT`.
- Extract the substring after "from " to end of subject.
- Map the long form to a short name: strip a trailing parenthetical
  hyphenated qualifier (e.g. ` - Distribution Center (DC)` → drop both
  the dash-clause and the parenthetical → keep `Target`, then re-append
  just the bracketed token → `Target DC`).
  - Concretely: `Target - Distribution Center (DC)` → `Target DC`.
  - For an unknown vendor with no parenthetical, use the raw vendor text
    verbatim. Do not crash on unfamiliar shapes.

**Received timestamp (from email Date header):**
- Convert UTC to America/New_York.
- Format as `H:MM AM/PM ET, M/D/YY` with **no leading zeros** on hour or
  date (so `5:19 AM ET, 5/24/26`, never `05:19` or `5/24/2026`).
- Use America/New_York via zoneinfo-equivalent logic — handle DST naturally,
  do not hardcode an offset.

**Body table (from htmlBody):**
The Crstl email body is a Postmark HTML table with header row
`PO Number | Amount | Dates | Items`, then one row per individual PO.
Parse the HTML (regex over the cell text is fine for this stable
Postmark template; if any line fails to match, log the line and continue
— don't crash the whole run on a single weird row).

For each PO row:
- **PO Number cell** → text content (strip the wrapping `<a>` tag).
  Format is `NNNNNNNNNNN-NNNN` (11-digit set prefix, dash, 4-digit individual).
  The 11-digit prefix is the **PO set ID**.
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

### 4. Validate and group by PO set

Within each email:
- Verify all PO numbers share the same 11-digit prefix. If a single email
  contains POs with multiple prefixes, split it into multiple sets and
  emit a warning to the user. (Realistic only as an edge case.)
- Verify all rows in the email share one ship window. If multiple windows
  appear, group sub-tables under that PO set's header — one table per
  distinct window.

### 5. Aggregate within each PO set

Sum `qty` by `vendor_style` across every individual PO row in the set.
Drop per-PO-number detail — the alert is set-level, not row-level.

### 6. Look up on-hand inventory

Call the Drive read-file-content tool on sheet
`1y3-daeuMeQVkLYRP89go9KfXMjBwfT-_VyzeMqwczD8` (this returns the whole
sheet as text; for the Inventory Summary tab the columns are pipe-rendered).

Build an in-memory map `vendor_style → on_hand_qty` from columns A → I.

The first row is the header row. Skip it. For each subsequent row:
- Column A is the vendor style key (text, e.g. `K-60WIP-AP-COM`).
- Column I is the on-hand integer (may contain commas, e.g. `4,126`;
  parse to int by stripping commas).
- If column I is blank, `N/A`, or non-numeric, treat that style's
  on-hand as **missing** (not 0).

### 7. Compute coverage per aggregated line

For each (po_set, vendor_style, qty_ordered):
- on_hand is missing or style not in the map → **⚠️**, and append
  ` (style not found in RDZ sheet)` to the On Hand cell text.
- on_hand >= qty_ordered → **🟢**
- on_hand <  qty_ordered → **🔴**

### 8. Sort lines within each set

Sort descending by `qty_ordered`. Ties: break alphabetically by vendor_style.

### 9. Sort PO sets within the run

Oldest received_ts → newest.

### 10. Assemble the Slack message

Use Slack `mrkdwn` with native Markdown table syntax (Slack supports
this natively as of early 2025; the reference rendering in #b2b-fulfillment
on May 25 confirms it renders cleanly).

**Header line** (one line, then a blank line):

```
*New POs received — Month D, YYYY*
```

Where `Month D, YYYY` is today's date in America/New_York (e.g.
`May 25, 2026` — no leading zero on the day).

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
- If `earliest` and `latest` ship are in the same month and year, render
  as `Mon D – Mon D, YYYY` (year only once at the end). The May 25
  reference uses `May 29 – May 30, 2026` and `Jun 1 – Jun 2, 2026`.
- Numbers use thousands separators (`1,234`).
- Column alignment: Vendor Style left, Qty Ordered + On Hand right,
  Coverage centered — encoded via `|---|---:|---:|:---:|`.

**Footer** (single line, blank line before it):

```
<@U0A0L7AFUHH> <@U0AEDDK51PY> please verify and flag coverage gaps as appropriate. cc <@U047ZL3SCFP>
```

**Total length check:** If the assembled message exceeds ~3,500 chars,
split into multiple Slack posts (one per PO set, with the header line
repeated only on the first post). This is unlikely on a normal Mon/Thu
run; only a multi-day backlog would trigger it.

### 11. Post to Slack (skip if `--dry-run`)

Call the Slack send-message tool with channel `C0ARHBQ476D` and the
assembled message body. The Claude bot user `U0ADC0J9GTA` is already
a member of the channel — no join step required.

### 12. Apply the processed label (skip if `--dry-run`)

Only after a successful Slack post:

1. Ensure the label `processed/crstl-po-alert` exists. Call the Gmail
   list-labels tool. If absent, create it via Gmail create-label.
2. Apply the label to **every thread** included in this run via the
   Gmail label-thread tool.

If the Slack post failed, do **not** apply labels — the next run should
retry the whole batch.

### 13. Report to the user

Print a concise summary in chat:
- Number of emails processed.
- Number of PO sets.
- Number of total line rows.
- Number of 🔴 and ⚠️ rows.
- A Slack permalink to the posted message if available (otherwise the
  channel name).
- For `--dry-run`: the full message body that would have been posted,
  fenced in a code block.

## Edge cases

- **Zero unprocessed emails** → exit silently per step 1.
- **Multiple ship windows in one email** → multiple sub-tables under one
  set header, one per window.
- **PO numbers without a common prefix in one email** → split into
  multiple sets, emit a warning, post each as its own block.
- **Vendor not Target** → use the subject's verbatim vendor string if the
  short-name heuristic doesn't apply.
- **Vendor style missing from RDZ sheet** → ⚠️ + `(style not found in RDZ sheet)`.
  Do not skip the row.
- **On-hand cell blank / "N/A" / non-numeric** → treat as missing (⚠️).
- **Slack 4000-char block limit** → split per PO set.

## Acceptance test

Run with `since:2026-05-24 --dry-run`. The output should contain three PO
sets, in this order, with these aggregated quantities:

- `10001901402` (Target DC, Received 5:19 AM ET, 5/24/26, ship May 29 – May 30, 2026).
  Top line: `P-DIS-EUC` with `3,486 Qty Ordered`.
- `10001902323` (Target DC, Received 5:18 AM ET, 5/25/26, ship Jun 1 – Jun 2, 2026).
- `10001903266` (Target DC, Received 5:23 AM ET, 5/25/26, ship May 30 – May 31, 2026).

The full reference rendering of this run is the message Aubrey posted
to #b2b-fulfillment at 10:08 EDT on 2026-05-25 — match its structure,
ordering, and per-set aggregates exactly. The On Hand and Coverage
columns of that reference are filler; only structure/ordering/totals
need to match.
