---
name: ask
description: Pass a question (or an image request, "draw") to another model word for word and bring its answer back word for word - Gemini, Claude and GPT models through the Antigravity CLI (agy, a Google quota), GPT through Codex (a ChatGPT plan), local models through Ollama; or by role (review, second-opinion, first-read, draft, index, writer, code-writer, analysis) and the router picks the model by quota, pace, experience and the GPU. Use on /ask, "ask gemini", "what would gpt say", or when you offer a second opinion and the user agrees.
---

# /ask: ask another model

## Rules
- The user's question goes over word for word, and the answer comes back word for word (in a quote block), not retold; your own view, if needed, comes separately and after. Every output starts with the model's name: `[<model>, <pool>]`.
- Models are named by their ids only (`ask.py --list`).
- Attach only the files, code or context the user named. If what is attached may hold anyone's personal data, ask first (what, where to, why): agy and Codex send everything to Google and OpenAI. In a work context, name the account a cloud call goes to before it goes.
- What this machine has right now (providers, accounts, models, efforts, roles, what is allowed): `ask.py --list`. The order of models in a role: `roles.json` (the bundle's), with your own `~/.config/ask/roles.json` over it; `ask.py --roles` proposes the roles from the models here.

## Calls

```bash
python3 ~/switchboard/skills/ask/ask.py <model> "question"
python3 ~/switchboard/skills/ask/ask.py --role review --file plan.md "Review this"   # the router picks the role's model
python3 ~/switchboard/skills/ask/ask.py <codex model> --effort high "question"      # effort (Codex; agy carries it in the id)
python3 ~/switchboard/skills/ask/ask.py --codex <model> "question"                  # through this provider explicitly
python3 ~/switchboard/skills/ask/ask.py <model> --tokens 120000 "question"          # token estimate for the reservation
python3 ~/switchboard/skills/ask/ask.py <model> --force "question"                  # despite a fit check below the floor
python3 ~/switchboard/skills/ask/ask.py <model> --continue "follow-up"              # the same conversation; --thread ID names one
python3 ~/switchboard/skills/ask/ask.py <model> --file path "question"              # a file appended after the question
python3 ~/switchboard/skills/ask/ask.py <local model> --wait "Index this"           # wait until the GPU is free
python3 ~/switchboard/skills/ask/ask.py <local model> --keep 10m "question"         # keep the model loaded for a series
python3 ~/switchboard/skills/ask/ask.py --unload                                    # unload whatever Ollama holds
python3 ~/switchboard/skills/ask/ask.py --status                                    # context, policy, pools, reservations, GPU, journal
python3 ~/switchboard/skills/ask/ask.py --context personal <model> "question"       # the context explicitly, over ASK_CONTEXT
python3 ~/switchboard/skills/ask/ask.py --list [--refresh] [--notes]                # the inventory; live lists past the cache
python3 ~/switchboard/skills/ask/limits.py                                          # the week's limits as one table
python3 ~/switchboard/skills/ask/dashboard.py                                       # a limits dashboard in the browser (127.0.0.1)
python3 ~/switchboard/skills/ask/ask.py draw "description" --out path/name          # an image (agy's image-generator)
```

| Provider | What it is | Quota pools |
|---|---|---|
| agy | Antigravity CLI: Gemini, Claude and GPT on a Google account's quota | groups from `agy -p "/usage"` |
| Codex | `codex app-server`, a ChatGPT plan | the weekly and 5-hour windows from `account/rateLimits/read` |
| Ollama | local models on this machine's GPU | the GPU (free VRAM, a lock) |
| a local server | any OpenAI-compatible server named in `local_servers` (LM Studio, llama.cpp, vLLM, MLX, Jan, LocalAI); `--server NAME <model>` | the GPU lock; VRAM only with a configured `size_gb` |
| Claude subscription | the session's own subagents only: the script never calls them, it prints `use a Claude subagent (<model>)` and exits 6 | `~/.cache/ask/claude_usage.json`, written by the session |

## Roles (`--role`, `roles.json`)

| Role | For | Notes |
|---|---|---|
| `review` | a code review of a change | |
| `second-opinion` | a second view of a diagnosis or a brief | |
| `first-read` | the first read of a long source | a local model while the GPU is free, else the cloud |
| `draft` | a draft | local models first |
| `index` | an index of figures and quotes | `local_only`: never the cloud, waits for the GPU; no local provider: exit 8 at once |
| `writer` | a writer agent on a brief (`tools/writer.py`) | cloud models, then a Claude subagent (exit 6) |
| `code-writer` | a patch without a build engine | `no_engine`: `--engine` is refused |
| `analysis` | reading code and records into a findings file | `no_engine` |

A role row names a model (`{"provider": "agy", "model": "gemini-4-argon-high"}`) or wishes for one:
`"prefer": [{"family": "gemini-4-argon*", "effort": ">=high"}, {"family": "gpt-6.1*"}]`. A wish is matched against what
this machine offers when the role is called: a new version under the glob is taken without an edit, a missing provider
gives `[passed over: ... is not on this machine]`. `>=high` takes the cheapest effort at or above the bound, `<=medium` the
strongest at or below it. Several matches of one row are equally fit. The order of the rows is the user's; within it the
router passes over what may not or should not go, and says why (`[passed over: ...]` on stderr).

**The choice in a role**, in turn:
1. **Policy** of the context and the accounts (below): what is refused goes.
2. **Needs**: `traits.cannot` in `roles.json` says what a provider or model cannot do on this platform (`python`, `engine`, `big-files`); `--needs a,b` adds to the role's needs. An unfit model is passed over.
3. **Quotas**: pools from agy, Codex and `claude_usage.json` (older than 6 h: stale, not blocking). Before a run its share of the window is estimated (billed tokens / 1M × the pool's rate; `--tokens`, else the median of recent runs, else `tokens_default`). Running calls reserve their share in `~/.cache/ask/reservations.json`. When `left − reserved − needed < floors`, the model is passed over (a named one refused; `--force` allows). A weekly pace over `pace_skip` with a calmer model later in the role: passed over.
4. **What expires first**: among equally fit models (an `any` group, or several matches of one wish), the one whose weekly leftover burns soonest (`left / hours to reset`) goes first; `[expires first: ...]` says so.
5. **Experience**: a prior from `benchmarks.json`, writer verdicts, capped runs and timeouts over 30 days move a model within `experience` limits in `roles.json` (at most two places, said in `[experience: ...]`). `pinned`, `local_only`, `any` groups and subscription rows do not move. `python3 skills/ask/experience.py --role <role>` shows the reckoning.
6. **Score** (`score` in a role): `live` orders by quality minus time and cost penalties; `shadow` keeps the old choice and shows `[score would pick: ...]`.

`quota` and an empty answer move the role to its next model; a deadline (exit 9) and the token cap (exit 5) do not: carry on with `--thread ID`.

## Context and account policy
- **Context**: `--context`, else `ASK_CONTEXT` (`work` | `personal`, e.g. from direnv), else the nearest `.switchboard-context` file up from the folder (one word, `work` or `personal`; `ask.py --context-here work` writes it), else by folder: `context_paths` in `machine.json` (`{"~/work": "work", "~": "personal"}`, the longest wins). Nothing fits or a wrong value: `work`, the strict side, and stderr says so. A first `machine.json`: `ask.py --init` (`--machine personal|work|both`, `--work-domain`, `--yes` without questions).
- **Account label** (`work` | `personal` | `local`): explicit in `machine.json` (`accounts`), else by the email's domain (`work_domains`), else `personal`. Ollama is `local`, the Claude subscription is the session's own. Accounts come from the inventory: `agy` (one a machine), `codex:~/.codex`, `codex:~/.codex-work`, ... (every `~/.codex*` with an `auth.json`). A call goes to the account the policy ranks first (in `work`, a work `~/.codex-work` before a personal one), with its own `CODEX_HOME`, pool and model list.
- **Policy**: in `work` the order is work → local → personal only with `work_ok: true` (false by default: a conscious choice); in `personal` work accounts are refused. A refusal: the reason on stderr, a `refused` row in the journal, **exit 7**. A work → personal crossing: `crossing: ...` on stderr and `"crossed": true` in the journal.

## Machine config `~/.config/ask/machine.json`

```json
{"work_domains": ["corp.example"],
 "context_paths": {"~/work": "work", "~": "personal"},
 "accounts": {"agy": {"label": "personal"}, "codex": {"label": "personal", "work_ok": false}},
 "paths": {"codex": "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex", "ollama": null},
 "hardware": {"gpu": {"kind": "apple", "model_budget_gb": 12}},
 "hooks": {"module": "~/path/to/my_hooks.py"}}
```

- `paths`: a path (for `ollama`, the API URL), `"auto"` or no key (look for it: PATH, `~/.local/bin/agy`, ChatGPT.app's `codex`, the newest `%LOCALAPPDATA%/OpenAI/Codex/bin/*/codex.exe`, Ollama by its API or binary), `null` (not on this machine: its models are never offered).
- `local_servers`: `{"lmstudio": {"url": "http://127.0.0.1:1234/v1"}, "llamacpp": {"url": "http://127.0.0.1:8080/v1", "models": ["qwen3-14b"], "static": true, "size_gb": 9.5, "load_timeout_s": 180, "api_key_env": "LLAMACPP_KEY"}}`. The name is the provider (not a built-in one, no `:`). `static` (or a server listing one id) serves one model only; ids that are file paths take `models` or the file's name; `size_gb` turns on the VRAM check; `load_timeout_s` gives a cold start its time; the key is read from the variable `api_key_env` names.
- `hardware.gpu`: `nvidia` (the Windows default) asks `nvidia-smi`; `apple` (the Mac default) gives local models `model_budget_gb` of shared memory, less what Ollama already holds (unset: half of the RAM; `0`: no local models).
- `hooks` (optional): a Python file with what only this machine knows (a build engine, games, a quiet-start script); the interface is in `hooks.py` (`local_blockers`, `claim_blockers`, `build_blockers`, `writer_rules`, `engine_markers`), the section's other keys are its settings. Without it, the plain GPU and VRAM checks only. A module that fails to load blocks local models.

## How a call runs
- **Broker**: by default `agy acp` / `codex app-server` in a read-only room, the question by the protocol, not the command line. Commands and writes need a human: `--ask never` (the default: a blocked Claude session cannot answer) refuses them; `--ask human` for a background run, the request in `<log>.pending.json`, the answer `ask.py --answer <base> <option> [text]`. `--deadline` counts the agent's seconds without the human waits (`0` turns it off); `--max-tokens N` caps billed tokens; `--verbose` shows the live log.
- **Codex**: `--effort low|medium|high|xhigh|max|ultra`; without it, the model's own. `--continue` resumes the provider's last /ask conversation.
- **agy**: the effort is part of the id (`<family>-low|medium|high`); `--list` groups ids by family.
- **Reasoning**: the broker keeps the reasoning summary in `~/.cache/ask/reasoning/<conversation>.md`. `--reasoning file` (default) prints its path; `live` adds a `tail -f` line; `show` prints it before the answer. Later: `--reasoning-read [<conversation>]`, `--reasoning-open [<conversation>]`.
- **Handing a conversation over**: the user wants to talk to the model themselves → `ask.py --handoff [<conversation>]` prints a command for a new terminal tab; read nothing of that conversation until they say they are done, then `ask.py --handoff-back <conversation> full|summary|none`.
- **Local models and the GPU**: only with enough free memory for the model, a free `~/.cache/ask/local.lock`, and no objection from the machine's hooks. Free memory: what `nvidia-smi` or `rocm-smi` says on NVIDIA and AMD; elsewhere (a Mac, an integrated GPU, the CPU) the budget `hardware.gpu.model_budget_gb` (half the RAM by default) less what Ollama holds. A memory that cannot be read blocks. A busy GPU makes a `local_only` role wait (`--wait`, every 30 s up to `--max-wait`, 2 h); any other role goes on to its next model.
- **Journal and logs**: `~/.cache/ask/usage.jsonl`, a row a call; raw output in `~/.cache/ask/logs/`.
- **The Claude subscription's limits** no script can read: the session reads them with the app's `get_usage` and writes `~/.cache/ask/claude_usage.json` (`/limits`).

## Writers (`tools/writer.py`)
- Name the kind of work, not the model: `python3 ~/switchboard/tools/writer.py --cwd <folder> --brief BRIEF.md [--role writer|code-writer|analysis]`, with the same choice, reservation, journal and verdicts as `/ask`. `--model <id>` with `--agy`/`--codex`, `--continue <conversation>` (with `--model`), `--add-dir`, `--needs`, `--max-tokens N` (a hard stop past N billed tokens: exit 5), `--deadline` (3600 s by default, exit 9).
- The broker's policy allows reading and writing in the run's folders and the writer's exact command; refuses git push and commit, killing processes, secrets folders; everything else goes to a human (`--ask human`, answered with `writer.py --answer <log> <option id> [text]`). Logs are redacted.
- Codex writes with `-s workspace-write` anywhere in `--cwd`: give it its own copy of the repository (a git worktree) when other uncommitted work is near.
- After every writer, record a verdict: `python3 ~/switchboard/tools/writer.py --verdict <conversation> ok|fixed|rejected "what it was"`; verdicts move models within `experience`.

## Exit codes

| Code | What |
|---|---|
| 2 | a provider, model or argument error |
| 3 | the GPU is busy (a local model) |
| 5 | `--max-tokens` exceeded |
| 6 | the fallback: take a Claude subagent |
| 7 | refused by the context or account policy |
| 8 | the role cannot run on this machine |
| 9 | the deadline passed |
