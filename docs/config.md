# Configuration

Two files and a handful of environment variables (an older `~/.config/ask/models.json` with tool paths is still read,
below `paths`). Every key below is optional; what is left out takes the default.
A test (`skills/ask/test_docs.py`) fails when the code reads a key or a variable this page
does not name.

## `~/.config/ask/machine.json`: this machine

`ask.py --init` writes the first one. Nothing in it leaves the machine.

| Key | What it does | Default |
|---|---|---|
| `preset` | active preset, below `--preset` and `ASK_PRESET`; blank is unset | `manual` |
| `paths` | where each tool is: `{"agy": "/path/agy", "codex": "...", "ollama": "http://127.0.0.1:11434"}`. A path (Ollama: its API URL), `"auto"` (look for it), or `null` (not on this machine: its models are never offered) | the old `models.json`'s path if it has one, else `auto` |
| `context_paths` | a folder's context: `{"~/work": "work", "~": "personal"}`; the deepest folder holding the current one wins | none |
| `work_domains` | your employer's email domains: an account whose email ends with one (or a subdomain) is a work account | none |
| `accounts` | an account's label by its id from `--list`: `{"codex:~/.codex-work": {"label": "work"}, "agy": {"label": "personal", "work_ok": true}}`. `label` is `work`, `personal` or `local`; `email` stands in for an email the tool cannot read; `work_ok: true` lets a personal account take work tasks (a conscious choice) | by email domain, else `personal` |
| `hardware` | `{"gpu": {"kind": "nvidia", "model_budget_gb": 12}}`: under `gpu`, `kind` and `model_budget_gb`. `kind`: `nvidia` (`nvidia-smi` says the free memory), `amd` (`rocm-smi` says it), `apple` or `unified` (local models get `model_budget_gb` of the memory less what Ollama holds: a Mac, an integrated GPU, a GPU no tool reads, or the CPU; unset: half the RAM, `0`: none), `none` (no local models). `device`: which card a model goes to (its index in `nvidia-smi` or `rocm-smi`; default: the one with the most free memory). On shared memory the budget is never more than the system has available now. A memory that cannot be read blocks local models; a kind guessed from a tool on PATH whose tool reads nothing tries the other tool, then the budget | `apple` on macOS, else `nvidia` or `amd` when its tool is on PATH, else `unified` |
| `local_servers` | OpenAI-compatible local servers, each a provider by its name: `{"lmstudio": {"url": "http://127.0.0.1:1234/v1"}}`. Per server: `url`; `models` (the ids to offer, and the names for ids that are file paths); `static` (one model fixed at the server's start, as llama.cpp and MLX); `size_gb` (a number, or `{model: number}`: turns on the VRAM check); `load_timeout_s` (a cold start's time, default 120, on top of the answer's); `api_key_env` (the variable that holds a key, sent as a bearer token, never logged). A name equal to a built-in provider or holding `:` is skipped | none |
| `hooks` | `{"module": "~/path/my_hooks.py", ...}`: a Python file with what only this machine knows (a build engine, games); its functions are listed in `skills/ask/hooks.py`; the section's other keys are its settings. A module that fails to load blocks local models | none |

The context of a call, the first that is found: `--context`; `ASK_CONTEXT`; the nearest `.switchboard-context` file up
from the folder (one word, `work` or `personal`; `ask.py --context-here work` writes it); `context_paths`; else `work`,
the safe side. A work task never goes to a personal account (unless `work_ok`), a personal task never to a work account.

## `skills/ask/roles.json` and `~/.config/ask/roles.json`: roles and quotas

The shipped file holds the defaults; yours, `~/.config/ask/roles.json`, holds only what you change and goes over it, so
an update of switchboard brings its new defaults and keeps your order. Over it: a role of yours replaces the shipped
role of that name whole (`null` removes it); a dict at the top (`floors`, `tokens_default`, ...) merges key by key;
anything else replaces. `ask.py --roles` (and `--init`'s last step) writes your file from the models this machine has:
a role keeps the shipped rows in their order, and only a role that reaches too few models here is filled from the
others, ranked by the role's `score.metrics` in `benchmarks.json` and your journal (two cloud models, one for a role
that spares the quota, two local ones for a local role); a role whose rows are a Claude subagent only is never filled.
It shows each role before and after and writes on a yes. `ASK_ROLES` names one whole file instead, with nothing over it
(the tests use `skills/ask/test_roles.json`).

**A role**, under `roles` (`"roles": {"review": {...}}`):

| Key | What it does |
|---|---|
| `prefer` | the rows in order. A row wishes for a model: `{"family": "gemini-4-argon*", "effort": ">=high", "provider": "agy"}` (`family` a glob over the ids of `--list`; `effort` `>=x` the cheapest at or above, `<=x` the strongest at or below, `x` exactly; `provider` optional). Or it names one: `{"provider": "agy", "model": "gemini-4-argon-high"}`. Or a group of equally fit ones: `{"any": [row, row], "effort": ">=high"}` (the quota that expires first goes first) |
| `models`, `names` | older spellings of `prefer`, read the same way |
| `needs` | what the work needs: `python`, `engine`, `big-files`; a model that `traits` says cannot is passed over |
| `local_only` | never the cloud: waits for the GPU; a machine with no local provider ends at once (exit 8) |
| `off` | rows switched off: kept for the presets editor (`ask.py --preset edit`, the dashboard's presets page) to switch back on, never called |
| `pinned` | experience never moves this role's order |
| `no_engine` | `writer.py --engine` is refused for the role |
| `exit_code` | the exit code when no model is left (6 for writers: take a Claude subagent) |
| `fallback`, `if_none` | the message when no model is left |
| `score` | `{"mode": "live" \| "shadow", "metrics": [...], "time": 0.5, "cost": 0.3, "time_budget_s": 900, "aa_time_scale": 0.6}`: `live` orders by quality from `benchmarks.json` and verdicts (times `quality`, default 1), less a time and a cost penalty; `shadow` only says what it would pick. `time_budget_s` is also the role's default `--deadline` |

**Top level**:

| Key | What it does |
|---|---|
| `models` | `{id: {"provider": ...}}`: a provider for an id no live list names |
| `presets` | named weight dictionaries: `manual: {}`, `thrifty: {"time": 0.2, "cost": 1.5}`, `strong: {"time": 0.05, "cost": 0.05}`, `fast: {"time": 1.5, "cost": 0.2}`; a preset may also carry `quality` (default 1). Top-level overlay merges by preset name; missing weights inherit the role. Weighted presets only report shadow picks. `ask.py --preset edit` and the dashboard's presets page change them in your file |
| `subscription_models` | the Claude subscription's model ids (its subagents) |
| `traits` | `{"cannot": {"local": {"*": ["python", "engine", "big-files"]}, "codex": {"win32": ["python"]}}}`: what a provider (or `local`, every local one) or a model cannot do, by platform (`sys.platform`, `*` any) |
| `never` | ids or globs never called (`--allow-never` overrides), `never_reason` says why |
| `floors` | `{"weekly": 0.1, "5h": 0.1}`: a pool window below this share left is not used |
| `pace_skip` | a weekly pace above this (x the week) passes the pool over while a calmer one comes later |
| `pool_covers` | `{"Gemini Models": ["gemini-*"], ...}`: which model ids a quota pool covers |
| `tokens_default` | billed tokens a call of each role is guessed at before its own runs are known |
| `cache_weight` | how much a cached token counts in a pool, by pool |
| `rate_default` | a pool window's share per 1M billed tokens until enough runs teach it |
| `experience` | how far writer verdicts, capped runs and timeouts may move a role's order: `enabled`; `values` (what `ok`, `fixed`, `rejected`, `capped`, `timeout` are worth); `prior`, `prior_weight` (the start from `benchmarks.json`); `other_role_weight`, `role_category` (evidence from related roles); `window_days`, `min_evidence`; `demote_below`, `demote_twice_below`, `promote_min_evidence`, `promote_above`; `probation_verdicts`, `probation_tokens` (an expensive model with few verdicts) |
| `capabilities` | notes on what a model did well or badly, with evidence; shown by `--list --notes` |
| `score_defaults` | `owner_step`, `owner_cap` (the bonus for a row's place in the role's order), `prior_runs`, `size_clamp`; `burn_floor` (minimum cost multiplier, 0.1), `recent_hours` (24), `history_min_span_h` (2), `history_min_gap_s` (600) |

## Environment variables

| Variable | What it does |
|---|---|
| `ASK_PRESET` | active preset, below `--preset`, above machine.json `preset`; blank is unset; default `manual` |
| `ASK_CONTEXT` | `work` or `personal`, over the folder (e.g. from direnv) |
| `ASK_LANG` | `en` or `uk`: the words of `/limits` and the band (default `en`) |
| `ASK_ROLES` | another `roles.json` |
| `ASK_CLAUDE_USAGE_PATH` | another file for the Claude subscription's limits, which `/limits` and the status line write and the router reads (default `~/.cache/ask/claude_usage.json`) |
| `ASK_CONFIG_DIR` | another folder for `machine.json`, the old `models.json` and your `roles.json` (default `~/.config/ask`) |
| `ASK_CACHE_DIR` | another folder for the journal, logs and caches (default `~/.cache/ask`) |
| `ASK_ROUTE_FAKE` | a fake machine state (JSON or a file) for tests: nothing outside is asked |
| `SWITCHBOARD_ROOT` | where the plugin lies, for the band (default `~/switchboard`) |
| `CODEX_HOME` | Codex's active account folder; another account the policy picks runs with its own |
| `CLAUDE_CONFIG_DIR` | the Claude Code profile, shown by `--list` |
| `LOCALAPPDATA` | where Windows keeps agy and Codex |
| `NO_COLOR`, `FORCE_COLOR` | colour off or on |
| `BROKER_FAKE_SCRIPT` | the fake agent's script, in the broker's tests |

## Files it writes

All under `~/.cache/ask/`: `usage.jsonl` (the journal, a row a call), `logs/` (raw output), `reasoning/` (reasoning
summaries), `reservations.json` (running calls' quota shares), `pool_history.json` (eight days of weekly remaining shares), `claude_usage.json`, the providers' model and usage caches
(`agy_models.json`, `codex_models.json`, ...), `local.lock` (the GPU lock), `ask_last_conversation.json` (for
`--continue`), `handoff/`.
