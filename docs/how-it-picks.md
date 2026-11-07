# How a role picks its model

`ask.py --role review ...` (and `writer.py --role ...`) walk the role's rows in `roles.json` and take the first model
that may go and should go. Every model passed over is said on stderr as `[passed over: <model>: <why>]`.

1. **Wishes become ids.** A row `{"family": "gemini-4-argon*", "effort": ">=high"}` is matched against what this machine
   offers right now (each provider's live list, each Codex account's own). Several matches of one row are equally fit;
   the newest version comes first. No match: `... is not on this machine` or `no model on this machine matches`.
2. **Experience moves rows.** Writer verdicts (`writer.py --verdict`), capped runs and timeouts of the last 30 days,
   started from public benchmarks, move a row up or down by at most two places (`experience` in `roles.json`) and say
   so. This is the order every step below works in; `pinned` and `local_only` roles, `any` groups and subscription
   rows do not move.
3. **Policy.** The call's context (work or personal) drops the accounts it may not use. In work: work accounts first,
   then local models, then personal ones only with `work_ok`. A model goes on every account the policy allows, in that
   order, so a work task reaches a work Codex account even when `CODEX_HOME` is a personal one. Nothing allowed: exit 7.
4. **Needs.** `traits.cannot` says what a provider or a model cannot do on this platform (`python`, `engine`,
   `big-files`); a role's `needs` (and `--needs`) pass such a model over.
5. **Never.** A model in `never` is refused unless `--allow-never`.
6. **Offered.** A model its provider's live list does not hold right now is passed over (`not offered by ...`).
7. **Quota.** Each cloud pool's windows (weekly, 5-hour) with what running calls have reserved taken off. The call's
   share is estimated (its billed tokens: `--tokens`, else the median of the role's recent runs, else `tokens_default`,
   times the pool's learned rate). Below `floors` after it: passed over. A weekly pace over `pace_skip` passes the pool
   over while a calmer model comes later.
8. **Memory, for a local model.** The GPU lock free (one local model at a time), no objection from the machine's hooks,
   and enough free memory for the model. Free memory is what `nvidia-smi` or `rocm-smi` says on NVIDIA and AMD; on a
   Mac, an integrated GPU, a GPU no tool reads, or the CPU it is the budget `hardware.gpu.model_budget_gb` (half the
   RAM by default) less what Ollama holds. What the model needs: for Ollama what it learned, the file's size, or a guess
   from the parameter count; for a local server its `size_gb`, if configured (a static server's model is loaded at its
   start, so nothing more is needed). A memory that cannot be read blocks. A busy GPU makes a `local_only` role wait
   (`--wait`, up to `--max-wait`); any other role goes on to its next model.
9. **What expires first.** Among equally fit models that passed, the one whose weekly leftover burns soonest
   (left / hours to its reset) goes first: unused quota is lost at the reset. The rows' order stays.
10. **Score.** A role with `score.mode: live` orders the allowed models by quality, less a time and a cost penalty
    (then experience and the expiry rule give way to it). The price of a call falls when, at the pool's rate over
    the last day, its quota would be lost unused at the reset.
11. **The Claude subscription** is the last resort: the script never calls it; it prints `use a Claude subagent (<model>)`
    and exits 6, and the session runs that model as its own subagent.

When a call fails on quota or answers nothing, the role goes on to its next model; a deadline (exit 9) or the token
cap (exit 5) does not move it. `ask.py --status` shows the pools, what burns first and the GPU; `ask.py --list` the
models and their roles.

Presets: `manual` preserves today's order and each role's live/shadow scoring. `thrifty`, `strong`, and `fast` use their own time/cost weights, in shadow only for now: the actual pick stays manual. Claude stays last under every preset. `ask.py --preset` compares all roles without calling models; `--preset fast` selects the active column.

Tuning them: the dashboard's presets page (`dashboard.py`, then Presets) and `ask.py --preset edit` make the same edits.
For the manual preset, a role's rows in another order, a row switched off (kept under the role's `off`, never called), or
a model of this machine added; for a weighted preset, its quality, time and cost weights. Beside each role stands whom
the router would pick now under the settings on screen, before anything is saved. A save writes your
`~/.config/ask/roles.json` (through a link, to the file it names) in one rename, and keeps the file before as
`~/.config/ask/roles.json.bak`; a role or preset edited back to the shipped one leaves your file. The dashboard listens
on 127.0.0.1 only; a write needs the token made at its start (in the address it prints and opens), an `Origin` of the
dashboard itself and a loopback `Host`, so another page in the browser cannot change the router's config.
