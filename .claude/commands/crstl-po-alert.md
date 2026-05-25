---
description: Run the Crstl PO coverage alert routine — pull new Crstl PO emails, aggregate by vendor style, look up RDZ on-hand, and post the coverage table to #b2b-fulfillment.
argument-hint: [since:YYYY-MM-DD] [--dry-run]
---

Run the Crstl PO coverage alert routine.

Follow the procedure documented in `@.claude/skills/crstl-po-alert/SKILL.md`
**verbatim** — every step, in order, including the configuration values,
parsing rules, Slack message format, edge cases, and the dry-run /
labeling logic.

Arguments provided by the user: `$ARGUMENTS`

Parse `$ARGUMENTS` according to the "Arguments" section of the SKILL.md:
- `--dry-run` skips the Slack post and Gmail label step.
- `since:YYYY-MM-DD` bounds the Gmail search to `after:YYYY/MM/DD`.
- No arguments → live run against all unprocessed Crstl emails.
