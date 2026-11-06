---
name: limits
description: Show the weekly limits of every pool (the Claude subscription, ChatGPT through Codex, agy) as one small table - what is left, when it resets, the pace, what burns first. Use on /limits, "limits?", "how much quota is left", "show usage", and at the start and end of a round instead of gathering numbers by hand.
---

# /limits: the limits as one table

1. Read the Claude subscription's limits with the app's `get_usage` tool (no script can see them).
2. Hand the reply to the script: it stores it for the router (`~/.cache/ask/claude_usage.json`) and prints the table:
   ```bash
   python3 ~/switchboard/skills/ask/limits.py --claude-json - <<'EOF'
   <get_usage's reply as it is, JSON>
   EOF
   ```
   Without `get_usage`: `python3 ~/switchboard/skills/ask/limits.py` (Claude from the last file, a line under the table says how old).
3. Show the table as it is. The State column: `calm` ✅, `fast` ⚠️, `skipped` ⚠️ (the router passes the pool over: its pace is over `pace_skip`), `spent` ❌. Under it one or two lines of conclusion: what burns first and where to put the work; do not retell the table.

Nearby: `skills/ask/statusline_usage.py` updates the "all models" weekly window from the status line at no quota cost; `ask.py --status` shows the same with reservations, the GPU and the journal; `dashboard.py` is a live view in the browser; the limits band keeps these numbers above the prompt.
