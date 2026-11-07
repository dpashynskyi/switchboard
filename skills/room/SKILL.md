---
name: room
description: A superchat - several agents (agy, Codex, local models) in one transcript - for a group chat with the user or a council that discusses a task, agrees on a plan and splits the roles by vote. You moderate and do not vote. Use on /room, "superchat", "ask them all together", "let the models discuss it".
---

# /room: a superchat you moderate

Members are `ask.py` calls, so the router's rules hold for every turn (contexts, floors, reservations, the GPU, the journal).
Members only read and talk; nothing in a room edits files. You (Claude) are the moderator: you run the commands, you do not vote,
and the Claude subscription is not a member.

## Open a room
1. Members from what this machine has (`ask.py --list`), two or three, by role or by model, in turn order:
   ```bash
   python3 ~/switchboard/tools/room.py new <slug> --topic "<the task in one line>" \
     --member "codex=--role review" --member "agy=--role second-opinion" [--rule veto]
   ```
   The rule is `veto` unless the user names another (`majority`, `unanimous`, `arbiter --arbiter <member>`).
   Rooms live in `~/.cache/ask/rooms/`, beside the journal, out of git.
2. Name the members and the accounts their calls go to before the first turn, as `/ask` does in a work context.

## Level 0: a group chat
`room.py say <room> "<the user's words, word for word>"` (or `--to <member>`). Show each answer word for word under its
member's name; your own view, if any, after.

## Level 1: a council
1. **Discuss.** `room.py round <room> --note "<what this round is for>"`: members speak in order, each sees the turns before.
   Two or three rounds; stop early when a round adds nothing new.
2. **Propose.** A member's reply may carry a proposal (`p1`, `p2`...); or write one yourself from the discussion:
   `room.py propose <room> plan --file plan.md`, then the split of roles: `room.py propose <room> roles --file roles.md`.
3. **Vote.** `room.py vote <room> p1`. `vetoed` sends it back to a round with the veto's reason as the note (each member
   vetoes once a room); `rejected` means a new proposal.
4. **Stop for the user** when `round` exits 3 (the round limit since the user last spoke), when a member asks the user
   (`asks the owner` in the transcript), or when two proposals in a row fail: give the positions in short and wait.
5. **Out.** The agreed plan and roles go to the user, who approves them before anyone acts on them.

`room.py tail <room>` shows the transcript; `room.py list` the rooms. Crews of subagents under a budget (level 2) are not
built yet.
