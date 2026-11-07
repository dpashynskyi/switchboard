# Command line

Every script's `--help` under Python 3.14; `python3 tools/cli_docs.py` rewrites this page.

## ask

```text
usage: ask.py [-h] [--role ROLE] [--tokens TOKENS] [--force] [--continue] [--thread THREAD]
              [--effort {low,medium,high,xhigh,max,ultra}] [--agy ID | --ollama ID | --server NAME |
              --codex ID] [--file FILE] [--think] [--reasoning {file,live,show}] [--handoff [HANDOFF] |
              --handoff-back ID MODE | --reasoning-read [REASONING_READ] |
              --reasoning-open [REASONING_OPEN]] [--deadline DEADLINE] [--ask {never,human}]
              [--ask-timeout ASK_TIMEOUT] [--max-tokens MAX_TOKENS] [--answer VALUE [VALUE ...]] [--wait]
              [--max-wait MAX_WAIT] [--keep KEEP] [--unload] [--preset [PRESET]] [--status] [--verbose]
              [--allow-never] [--list] [--refresh] [--context CONTEXT] [--notes]
              [--context-here WORK|PERSONAL] [--init] [--machine {personal,work,both}]
              [--work-domain WORK_DOMAIN] [--roles] [--yes] [--needs NEEDS]
              [name] [text ...]

positional arguments:
  name                  model id (see --list)
  text

options:
  -h, --help            show this help message and exit
  --role ROLE           route by role: review, second-opinion, first-read, draft, index, writer, code-
                        writer, analysis, engine-writer (roles.json)
  --tokens TOKENS       override estimated billed tokens for pre-run fit and reservation
  --force               run an explicitly named model even if its estimate crosses the floor
  --continue            agy/Codex: carry on the last /ask conversation
  --thread THREAD       agy/Codex: resume this session/thread id
  --effort {low,medium,high,xhigh,max,ultra}
                        Codex reasoning effort (the model's own list: --list); default is the model's own
  --agy ID              explicit agy model id
  --ollama ID           explicit Ollama model id
  --server NAME         configured local OpenAI-compatible server
  --codex ID            explicit Codex model id
  --file FILE           a file appended after the question (repeatable)
  --think               qwen3: think first (slower)
  --reasoning {file,live,show}
  --handoff [HANDOFF]
  --handoff-back ID MODE
  --reasoning-read [REASONING_READ]
  --reasoning-open [REASONING_OPEN]
  --deadline DEADLINE   agent seconds; 0 disables (role budget in role mode, otherwise 1800)
  --ask {never,human}
  --ask-timeout ASK_TIMEOUT
  --max-tokens MAX_TOKENS
  --answer VALUE [VALUE ...]
  --wait                wait every 30 s while GPU is busy (default on local_only roles)
  --max-wait MAX_WAIT   max seconds or duration (e.g. 2h) to wait for GPU (default: 2h)
  --keep KEEP           Ollama keep_alive duration (e.g. 10m) for a series of questions; default 0
  --unload              unload whatever Ollama holds in video memory
  --preset [PRESET]     preset name; without a question, compare all presets (shadow)
  --status              show pools, reservations, GPU, Ollama and last 24h journal
  --verbose             stream agy's steps one line each as they happen
  --allow-never         allow calling a model in roles.json's never list
  --list                provider -> account (label) -> model -> efforts, and what this context allows
  --refresh             with --list: read the providers' live lists again, past their caches
  --context CONTEXT     work or personal; over ASK_CONTEXT; anything else is work (the strict side)
  --notes               include model capabilities and recent writer verdicts in --list
  --context-here WORK|PERSONAL
                        write .switchboard-context in this folder: it and every folder under it are that
                        context
  --init                look at this machine, propose ~/.config/ask/machine.json, write it on a yes
  --machine {personal,work,both}
                        with --init: what this machine is for
  --work-domain WORK_DOMAIN
                        with --init: your employer's email domain (repeatable)
  --roles               propose the roles from this machine's models, show the change, write
                        ~/.config/ask/roles.json on a yes
  --yes                 with --init or --roles: write without asking
  --needs NEEDS         comma-separated needs: python, engine, big-files
```

## writer

```text
usage: writer.py [-h] [--preset PRESET] [--cwd CWD] [--brief BRIEF] [--model MODEL] [--tokens TOKENS]
                 [--needs NEEDS] [--force] [--python PYTHON] [--add-dir ADD_DIR] [--continue CONVERSATION]
                 [--log LOG] [--engine] [--allow-never] [--role ROLE] [--max-tokens MAX_TOKENS]
                 [--deadline DEADLINE] [--ask {human,never}] [--ask-timeout ASK_TIMEOUT]
                 [--answer VALUE [VALUE ...]] [--agy | --codex] [--verdict ID VERDICT NOTE]
                 [text]

positional arguments:
  text                  the message (with --continue), or added after the brief

options:
  -h, --help            show this help message and exit
  --preset PRESET       active preset (weighted presets are shadow only)
  --cwd CWD             the folder the writer works in
  --brief BRIEF         the brief, a file the writer is told to read and follow
  --model MODEL         model id; given again with --continue
  --tokens TOKENS       override estimated billed tokens for pre-run fit and reservation
  --needs NEEDS         comma-separated needs: python, engine, big-files
  --force               run an explicitly named model even if its estimate crosses the floor
  --python PYTHON       the interpreter for agy_run.py (default: <cwd>/.venv's python, else python or
                        python3)
  --add-dir ADD_DIR     another folder the writer may read and write (repeatable)
  --continue CONVERSATION
                        carry on a conversation or thread by its id
  --log LOG             where the JSON result goes (default: <cwd>/writer_last.json)
  --engine              the writer may build and run the engine, through the brief's scripts only
  --allow-never         allow a model in roles.json's never list
  --role ROLE           the workload's role in roles.json (default: writer; analysis, engine-writer, ...)
  --max-tokens MAX_TOKENS
                        hard stop: the writer is stopped once this run's billed tokens (fresh + cached x w)
                        pass N; carry on with --continue
  --deadline DEADLINE   agent seconds before stopping (default: 3600; 0 disables); time spent waiting for a
                        human answer does not count
  --ask {human,never}   broker approvals (default: human)
  --ask-timeout ASK_TIMEOUT
                        seconds to wait for a human (default: 600)
  --answer VALUE [VALUE ...]
                        <base> <option> [text]: answer a pending request
  --agy                 use agy for --model
  --codex               use Codex for --model
  --verdict ID VERDICT NOTE
                        record ok, fixed or rejected for a writer run

Broker: agy's usage is read from its transcript after each model step, so --max-tokens stops it there.
```

## limits

```text
usage: limits.py [-h] [--claude-json CLAUDE_JSON]

The week's limits of every pool as one small table.

options:
  -h, --help            show this help message and exit
  --claude-json CLAUDE_JSON
                        get_usage's reply to store first: a file, or - for stdin
```

## dashboard

```text
usage: dashboard.py [-h] [--port PORT] [--no-browser] [--once FILE]

On-demand, loopback-only quota dashboard.

options:
  -h, --help    show this help message and exit
  --port PORT
  --no-browser
  --once FILE   write an inlined snapshot and exit
```

## experience

```text
usage: experience.py [-h] (--check | --role ROLE)

Small, owner-bounded adjustments from public priors and local outcomes.

options:
  -h, --help   show this help message and exit
  --check
  --role ROLE
```

## comment_check

```text
usage: comment_check.py [-h] [--cwd CWD] [--json] [--limit LIMIT] FROM [TO]

List comments added between git trees, with history signs first.

positional arguments:
  FROM           starting commit or tree
  TO             ending commit or tree (default: working tree snapshot)

options:
  -h, --help     show this help message and exit
  --cwd CWD      repository folder
  --json         print entries as JSON
  --limit LIMIT  maximum entries in the report
```
