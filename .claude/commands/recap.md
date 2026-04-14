---
description: Generate a daily trading recap and save it to the Obsidian algotradervault
argument-hint: "[YYYY-MM-DD]  (optional — defaults to today)"
---

You are generating a daily trading recap for the Alpaca Bot. This command is designed to run either interactively on a Mac or headlessly via cron on a Raspberry Pi — adapt to whichever environment you're in.

## Target date

If `$ARGUMENTS` is non-empty, treat it as the target date (format `YYYY-MM-DD`). Otherwise use today's date in `America/New_York` (the NYSE session day, not UTC). Use this same date for the filename, the recap headline, and when filtering logs.

## Resolve vault path

Run `echo "${VAULT_PATH:-}"` first. If set, use that. Otherwise detect the machine:

- `uname -n` contains `MacBook` or similar Mac host → `/Users/oliver/Obsidian Vaults/algotrader` (local folder is `algotrader` singular; remote repo is `algotradervault`)
- Otherwise (Pi / Linux) → `~/algotradervault` (expand with `echo $HOME/algotradervault`)

Verify the path exists and is a git repo. If it doesn't, stop and tell the user the vault isn't cloned on this machine.

## Gather trading state

Pull everything you need to write a substantive recap:

1. **Account state** — use the project's Alpaca client (see `scanner/live_scanner.py` and `webapp/server.py` for how it's instantiated). Get: current equity, cash, buying power, today's P&L, and daily change %.

2. **Positions** — fetch current open positions with entry price, current price, unrealized P&L, side, and qty.

3. **Orders filled today** — fetch orders with status `filled` and `filled_at` on the target date. Separate into entries (opening) and exits (closing).

4. **Equity curve** — read `equity_logs/` (most recent CSV) for today's intra-day equity points, so you can describe the trajectory (monotonic up, drawdown then recovery, etc.) rather than just start/end.

5. **Scanner logs** — grep `logs/equity_scanner.log` and `logs/crypto_scanner.log` for the target date. Pull signal events, rejected trades, and any errors. These tell you *why* the bot did (or didn't) act.

If any data source is missing (e.g., bot wasn't running, no equity log for that day), note the gap explicitly in the recap — don't fabricate numbers.

## Categorize

Organize everything into three buckets:

- **Entered today** — new positions opened on the target date
- **Exited today** — positions closed on the target date (compute realized P&L per trade)
- **Still held** — positions opened earlier and still open at end-of-day on the target date (show unrealized P&L and entry date)

## Explain reasoning

For each trade, cross-reference with the strategy that produced the signal:

- Equity side uses `HybridTrendMRStrategy` (200-SMA regime filter + BB/RSI mean reversion). Name the specific trigger: BB-lower touch, RSI < threshold, regime flip, etc.
- Crypto side uses `CryptoTrendFollowingStrategy`. Name the trigger: trend confirmation, breakout, pullback entry, etc.
- For exits, distinguish: stop-loss hit, take-profit hit, exit signal from strategy, manual intervention.

If you can't determine the reason from logs, say so rather than guessing.

## Self-audit

This is the most important section — it's what turns the recap into a learning tool. Critique honestly:

- **Went right**: trades where the thesis played out, where sizing was appropriate, where the bot behaved as designed.
- **Went wrong**: whipsawed trades, missed signals, stops hit immediately (MAE), position sizing errors, execution slippage, scanner errors.
- **Unknowns**: anything you couldn't determine from the available data — flag it so it can be instrumented.
- **Adjustments for next session**: concrete changes to consider (parameter tweaks, code fixes, new instrumentation). Keep these actionable, not vague.

Don't soft-pedal. If every exit today was a stop-out, say so.

## Severity tags (required on every Adjustment)

Every item in "Adjustments for next session" MUST start with a severity tag. This is what drives the audit→fix loop — items tagged 🔴 and 🟡 get filed as GitHub issues automatically.

- **🔴 critical** — Live bug actively affecting trading. Order rejected, position stuck, signal misfiring on real money. Anything that blocks the bot from doing its job. These should never wait — file immediately.
- **🟡 instrumentation** — Missing visibility, gaps in logging, unverified assumptions, investigations needed. Anything that prevented this recap from being precise. Worth a tracked issue.
- **🟢 enhancement** — Nice-to-have, future optimization, parameter exploration. Not worth a tracked issue — leave it as recap-only.

Format each adjustment as:

```
🔴 **<one-line title>** — <2-3 sentence description, including file path and proposed approach>
```

Example:

```
🔴 **Cancel bracket children before strategy-driven exit** — In `scanner/live_scanner.py` the EXIT signal path submits a market sell directly, but bracket STOP/LIMIT children hold the qty as `held_for_orders`, causing 403s on every poll. Cancel the bracket siblings first via `client.cancel_order_by_id()`, then submit the exit.
```

## Write to vault

File path: `<VAULT_PATH>/Daily Recaps/<YYYY-MM-DD>.md`

Create `Daily Recaps/` if it doesn't exist. If a file already exists for that date, append a `## Addendum <HH:MM>` section rather than overwriting (multiple recaps per day are fine — crypto runs 24/7).

Use this template:

```markdown
# <YYYY-MM-DD> Trading Recap

**Session**: equity + crypto | crypto-only | equity-only
**Equity**: $<start> → $<end> (<±%>)
**Realized P&L**: $<N>
**Trade count**: <N entries>, <N exits>, <N still open>

## Entered

### <SYMBOL> — <qty> @ $<price> (<strategy>)
- **Signal**: <specific trigger>
- **Reasoning**: <1–2 sentence thesis>
- **Stop / Target**: $<stop> / $<target>
- **Risk**: $<dollar risk> (<% of equity>)

## Exited

### <SYMBOL> — $<entry> → $<exit> (<±$N>, <±%>)
- **Held**: <duration>
- **Exit reason**: <stop | target | signal | manual>
- **Retrospective**: <what this trade tells us>

## Still Held

### <SYMBOL> — entered <YYYY-MM-DD> @ $<entry>
- **Current**: $<price> (<unrealized ±$N>)
- **Stop / Target**: $<stop> / $<target>

## Audit

### What went right
- <point>

### What went wrong
- <point>

### Unknowns
- <point>

### Adjustments for next session
- 🔴 **<title>** — <description>
- 🟡 **<title>** — <description>
- 🟢 **<title>** — <description>

## Raw logs referenced
- equity_scanner.log lines <N–M>
- crypto_scanner.log lines <N–M>
- equity_logs/<filename>.csv

---
#daily-recap #<YYYY-MM>
```

## Commit and push

After writing, `cd` into the vault path and run:

```bash
git add "Daily Recaps/<YYYY-MM-DD>.md"
git commit -m "Daily recap <YYYY-MM-DD>"
git push
```

If `git push` fails because of auth (common on a Pi the first time), stop and report the error — don't silently proceed. The user needs to fix credentials.

If running on the Mac, obsidian-git will also eventually push — the explicit push here is idempotent and ensures the recap lands on GitHub immediately.

## File audit findings as GitHub issues

After the recap is written and pushed, parse the "Adjustments for next session" section and file each 🔴 and 🟡 item as a GitHub issue on the **alpaca-bot** repo (NOT the vault repo). 🟢 items stay recap-only.

Preconditions:
- `gh` must be installed and authenticated (`gh auth status` should succeed)
- The repo must have a `bot-audit` label (create once with `gh label create bot-audit --color FBCA04 --description "Surfaced by /recap audit" 2>/dev/null || true`)

For each 🔴 and 🟡 adjustment, run:

```bash
gh issue create \
  --repo OliverF21/alpaca-bot \
  --title "<severity emoji> <title from adjustment>" \
  --label bot-audit \
  --label "<severity-label>" \
  --body "$(cat <<'BODY'
**Surfaced by**: [Daily Recap <YYYY-MM-DD>](https://github.com/OliverF21/algotradervault/blob/main/Daily%20Recaps/<YYYY-MM-DD>.md)

**Severity**: <🔴 critical | 🟡 instrumentation>

## Description

<full description from the adjustment>

## Context from recap

<1-2 sentences from "What went wrong" or "Unknowns" that motivated this item>

## Suggested approach

<the proposed fix from the adjustment>

---
*Filed automatically by `/recap`. Run `/fix-from-recap` to work through open audit issues.*
BODY
)"
```

Severity labels: `critical` for 🔴, `instrumentation` for 🟡. Create them once if they don't exist:

```bash
gh label create critical --color B60205 --description "Live bug affecting trading" 2>/dev/null || true
gh label create instrumentation --color FBCA04 --description "Missing visibility / investigation" 2>/dev/null || true
```

**Skip filing** if:
- `gh auth status` fails (e.g., running on a Pi without auth) — instead, write the would-be issues to `<vault>/Daily Recaps/<YYYY-MM-DD>-issues.json` so they can be filed later from a machine with gh access.
- An issue with the same exact title already exists open on the repo (avoid duplicates from re-runs of the same recap).

After filing, append to the recap file a `## Filed Issues` section listing each created issue with its number and URL, e.g.:

```markdown
## Filed Issues
- 🔴 [#47](https://github.com/OliverF21/alpaca-bot/issues/47) — Cancel bracket children before strategy-driven exit
- 🟡 [#48](https://github.com/OliverF21/alpaca-bot/issues/48) — Add per-order reason logging
```

Then commit + push the updated recap (small second commit is fine).

## Report

At the end, print a one-line summary: `Recap written to <vault path>/Daily Recaps/<YYYY-MM-DD>.md and pushed.` Include the recap's top-level stats so the user can eyeball them without opening the file.
