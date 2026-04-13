---
description: Pick an open audit issue (filed by /recap) and work through a fix
argument-hint: "[#issue-number]  (optional — if omitted, you'll see a list)"
---

You are working through an audit finding from a daily recap. The goal is to fix one issue cleanly, with verification, and close the loop.

## Source of truth

Open audit issues live as GitHub issues on `OliverF21/alpaca-bot` with the `bot-audit` label. They were filed by `/recap`. Each issue has:
- A severity label (`critical` for 🔴, `instrumentation` for 🟡)
- A link back to the recap markdown in the algotradervault repo that surfaced it
- A "Suggested approach" section

## Step 1 — Pick an issue

If `$ARGUMENTS` contains an issue number (e.g. `#47` or `47`), use that one directly. Verify it's open and labeled `bot-audit`:

```bash
gh issue view <N> --repo OliverF21/alpaca-bot --json number,title,state,labels,body
```

Otherwise list open audit issues, sorted with critical first:

```bash
gh issue list --repo OliverF21/alpaca-bot --label bot-audit --state open --json number,title,labels --jq 'sort_by(if (.labels | map(.name) | index("critical")) then 0 else 1 end)'
```

Display them to the user with severity emoji and ask which to tackle. **Always start with `critical` if any are open** — those are live bugs.

## Step 2 — Read the issue and the linked recap

Fetch the issue body and the linked recap markdown from the vault. Read both fully. The recap has crucial context (what symptoms, what logs, what was already ruled out) that the issue body summarizes but doesn't fully reproduce.

## Step 3 — Reproduce or verify the bug

Before changing any code, prove the bug exists:

- For 🔴 bugs (e.g. failing order submissions, broken signal handling): grep the relevant scanner logs (`logs/equity_scanner_*.log`, `logs/crypto_scanner_*.log`) for the error pattern. Show the user the matching log lines.
- For 🟡 instrumentation gaps: identify the file/function where the missing logging should be added. Read it.

If you can't reproduce or confirm the issue from the available data, report that and ask the user before proceeding — don't speculate-fix.

## Step 4 — Plan the fix

Write a short plan in the chat (3-7 bullets):
1. What file(s) will change
2. What the change does
3. How you'll verify it (test? log inspection? dry-run script?)
4. What could go wrong

Wait for user confirmation before editing code that touches order execution. Trading code changes are not allowed to be one-shot. Anything in `scanner/`, `strategy_ide/execution/`, or `risk/` requires approval.

For pure instrumentation/logging changes, you can proceed without confirmation but still write the plan first.

## Step 5 — TDD where possible

If the change has testable behavior (a function that returns something, a state machine that transitions), write a failing test FIRST. The project uses pytest in `strategy_ide/tests/`. Run:

```bash
python -m pytest strategy_ide/tests/<new_test>.py -v
```

Confirm it fails for the right reason, then implement, then confirm it passes.

For changes that can't be unit-tested cleanly (live order management, log format additions), use the next-best verification: run a dry-run script, inspect a sample log line, or simulate the failing condition.

## Step 6 — Implement

Make the smallest change that fixes the issue. Don't refactor adjacent code "while you're there." If you spot related issues, note them — they should become their own audit findings, not bundled in.

## Step 7 — Verify

- Tests pass: `python -m pytest strategy_ide/tests/ -v` (or the specific test file)
- For live-trading code: run the affected scanner in a dry-run mode if available, OR show the user the diff and ask them to restart the scanner and watch for the error pattern to disappear
- For instrumentation: tail the log briefly to confirm the new line appears

## Step 8 — Commit and link

Commit message format:
```
fix(<area>): <one-line summary> (audit #<issue>)

<2-3 sentence body explaining what changed and why>

Closes #<issue>
```

Use `Closes #<N>` so GitHub auto-closes the issue on merge. If the user is committing to a branch (not main), use `Refs #<N>` and let the PR close it.

After the commit lands, leave a comment on the issue summarizing the fix:

```bash
gh issue comment <N> --repo OliverF21/alpaca-bot --body "Fixed in <commit-sha>. <One-line summary of approach.>"
```

## Step 9 — Report

Tell the user: which issue was fixed, what file(s) changed, how it was verified, and whether there are any remaining open audit issues they should know about. Suggest the next one if it's critical.

## Notes

- Never bundle multiple audit issues in one fix unless they're literally the same code change. Each issue should map to one focused commit so the recap → fix → close cycle stays clean.
- If you discover the issue is actually a duplicate of another open issue, close it as duplicate referencing the original — don't fix the same thing twice.
- If the audit finding turns out to be wrong (false positive — the bug doesn't actually exist), close the issue with a comment explaining what you verified, and add a note to the recap so the next recap doesn't refile it.
