# Shared stats

Run the server only on a private network or tunnel: bearer tokens and Basic passwords go in clear over HTTP.
Set `SWITCHBOARD_DASH_TOKEN` and a different `SWITCHBOARD_INGEST_TOKEN` in its environment (at least 16 characters
each), then run `python3 skills/ask/dashboard.py --serve --data ./stats-data --host 127.0.0.1 --port 8765`.
`SWITCHBOARD_DASH_DATA` can provide the directory instead. The server never probes providers or the GPU.
Use your browser's Basic authentication prompt with any username and the read token as password, or send
`Authorization: Bearer` from an HTTP client. Tokens belong in headers, never URLs. Ingest can be disabled by
omitting its token. The two tokens cannot be equal.

On each sending machine configure `stats` in machine.json as described in [config.md](config.md). Keep the ingest
token in the configured token file, outside the repository. The client pushes automatically after journal writes
and live quota reads; `python3 skills/ask/stats_push.py` also flushes the backlog manually. `--quiet` suppresses
network errors, `--snapshots-only` leaves journal offsets alone, and `--dry-run` prints counts and snapshot names
without sending or advancing state. Changed snapshots are throttled to once a minute unless rows are sent.
Explicit `ASK_CACHE_DIR` overrides disable automatic spawning; run the client manually for such installations.

Journal byte offsets and snapshot hashes live in `stats_push.json` beside the local journal. Incomplete trailing
lines wait until the next push. Rotation replays the file and the server deduplicates rows. A failed batch stays
queued; earlier acknowledged batches remain acknowledged. The client refuses redirects and bypasses HTTP proxies.
The server stores each machine independently below `machines/`; Claude accounts merge by plan and weekly all-models resets within one hour.
Each account panel shows raw used/left percentages, reset, pace and per-window age/source/machine; optional extra usage appears below.
The dashboard groups cards by machine, merges selected journals and charts, and says “no push” after 15 minutes.
