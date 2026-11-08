# switchboard

Ask another model from Claude Code, verbatim. One router over **agy** (Antigravity CLI: Gemini, Claude and GPT on a Google
quota), **Codex** (the ChatGPT plan), **Ollama** and any OpenAI-compatible local server (LM Studio, llama.cpp, vLLM, MLX, Jan,
LocalAI), that knows each pool's weekly quota and pace and picks
a model by **role**: `review`, `second-opinion`, `first-read`, `draft`, `index`, `writer`, `code-writer`, `analysis`.

| Part | What it does |
|---|---|
| `/ask` (`skills/ask`) | `ask.py <model> "question"` or `ask.py --role review --file diff "..."`. Roles are wishes (`{"family": "gemini-4-argon*", "effort": ">=high"}`) matched against the live model lists, so a new model version is taken without an edit. Skips a pool under its floor or burning faster than the week, prefers the quota that expires first, keeps local models off a busy GPU, keeps work tasks off personal accounts (`work`/`personal` context). |
| writer broker (`tools/writer.py`, `tools/broker.py`) | runs an agy or Codex agent on a brief through `agy acp` / `codex app-server`, under a permission policy: its folders only, no push or commit, no secrets folders, everything else to a human. A turn that ends while the agent's own plan (its todo list) still has open items sends it back to them, up to `--todo-turns` times (default 3, `0` turns it off); a carry-on that closes nothing, a deadline, the token cap or a spent quota stop it sooner, and what is left open goes into the result. At the end of a run writer.py lists the comments the run added (`comments added`), those with a date, a round or a review first; it never blocks. |
| `/room` (`skills/room`, `tools/room.py`) | a superchat: several agents in one transcript, moderated by Claude. A group chat (`say`, everyone answers the user at once) or a council (`round`, members speak in turn and see each other; `propose` a plan or a split of roles; `vote` by `veto`, `majority`, `unanimous` or `arbiter`). Every turn is an `ask.py` call, so the router's rules hold; members only read. |
| cloud sessions (`tools/cloud.py`) | hands a brief to a Claude Code cloud session (claude.ai/code): `cloud.py start --cwd <repo> --brief BRIEF.md` runs `claude --cloud` there, or `--routine trig_...` fires a scheduled task's API trigger (token from `CLAUDE_ROUTINE_TOKEN`); `cloud.py send <session> "..."` follows up, `cloud.py list` shows them. The context policy and the Claude floors hold, every start goes to the journal; the session works on its own branch, so what comes back is a branch or a pull request. |
| `/limits` and the limits band | the week of every pool in one table, and a band above the prompt (a Claude Code mod), which also stores the Claude week after each turn, in the desktop app too. |

## Install (a minute)

Needs Python 3.9+ (standard library only) and at least one of `agy`, `codex` (signed in), `ollama` or a local server.

```bash
git clone https://github.com/dpashynskyi/switchboard ~/switchboard
python3 -B ~/switchboard/skills/ask/ask.py --init      # is this machine personal, work or both? writes machine.json
python3 -B ~/switchboard/skills/ask/ask.py --list      # what this machine has: providers, accounts, models, roles
```

`/ask` keeps work and personal apart on every call: a work task never goes to a personal account, and a personal one
never spends a work account's quota. With nothing said it assumes work, the safe side, and on a fresh install every
account is personal, so every call would be refused: `--init` says what the machine is for. Doing work here too? Run
`ask.py --context-here work` in a folder of work code (or above several): a `.switchboard-context` file there makes it
and everything under it work, wherever it lies. Work accounts are told apart by your employer's email domain
(`--init` offers the ones it finds) or by name in `accounts`.

Load it in Claude Code with `claude --plugin-dir ~/switchboard`, or for every session add
`"env": {"CLAUDE_CODE_PLUGIN_DIRS": "/absolute/path/to/switchboard"}` to `~/.claude/settings.json`. The skills call
`~/switchboard/...`; cloned elsewhere, set `SWITCHBOARD_ROOT` for the band and edit the paths in the two `SKILL.md`.

More: every flag in [docs/cli.md](docs/cli.md), every setting in [docs/config.md](docs/config.md), how a role picks
its model in [docs/how-it-picks.md](docs/how-it-picks.md).

## Config files

| File | What |
|---|---|
| `~/.config/ask/machine.json` | this machine (`--init` writes the first one): CLI paths (`null` = not here), `context_paths` (`{"~/work": "work", "~": "personal"}`), `work_domains`, account labels (`accounts`, `work_ok`), `hardware.gpu`, `local_servers` (`{"lmstudio": {"url": "http://127.0.0.1:1234/v1"}}`), optional `hooks` (a Python file with the machine's own GPU or build rules, see `skills/ask/hooks.py`) |
| `skills/ask/roles.json` | the shipped roles and their order, `never`, quota `floors`, `pace_skip`, token and rate defaults; your changes go in `~/.config/ask/roles.json`, over it, so an update keeps them. `ask.py --roles` writes that file from the models this machine has, showing each role before and after |

Tested with **agy 1.5.1** (ACP 1) and **codex app-server 0.166.0-alpha.6**, on macOS and Windows. Tables and the band
speak English; `ASK_LANG=uk` switches them to Ukrainian.

```bash
cd ~/switchboard/skills/ask && python3 -B -m unittest       # offline: every provider is a fake
python3 -B ~/switchboard/tools/test_broker.py
python3 -B ~/switchboard/mods/limits-band/test_band.py
```

You answer for the terms of your own subscriptions (Google, OpenAI, Anthropic): this tool only drives the CLIs you
installed and signed in to, with your accounts. MIT licence.

## Developing

Run the offline suites from the repository root:

```sh
(cd skills/ask && python3 -B -m unittest)
python3 -B tools/test_broker.py
python3 -B mods/limits-band/test_band.py
python3 -B tools/test_pii_sweep.py
python3 -B tools/test_cloud.py
```

Run `python3 tools/pii_sweep.py --install-hook` once per clone. After changing any `--help`, run
`python3 tools/cli_docs.py` to refresh the reference. Before a release, run
`python3 tools/clean_install_check.py [--repo <path or URL>] [--live]`.
