"""A superchat room: several agents in one transcript, a group chat (level 0) and a council (level 1).

The moderator is the Claude session that runs these commands; it does not vote. Members are ask.py calls, so every
turn goes through the router as it is: contexts, floors, reservations, the GPU lock, the journal. Members only read.

    python3 tools/room.py new <slug> --member "codex=--codex gpt-5.5" --member "agy=--role second-opinion" [--rule veto]
    python3 tools/room.py say <room> "text" [--to <member>]   # the owner's message; members answer at once
    python3 tools/room.py round <room> [--note "text"]       # members take turns in order, each sees the turns before
    python3 tools/room.py propose <room> plan|roles --file body.md [--by <member>]
    python3 tools/room.py vote <room> <proposal id>          # every member votes; the room's rule decides
    python3 tools/room.py say <room> "question" --as moderator  # the moderator's own question, not the owner's
    python3 tools/room.py retry <room> <member>               # take a member's last failed turn again
    python3 tools/room.py note <room> "text"                 # the moderator writes without asking anyone
    python3 tools/room.py tail <room> [-n 20] | list

Rules: majority (more yes than no), unanimous (every yes), veto (majority, but a member may veto once in the room and
the proposal goes back to discussion), arbiter (the member named by --arbiter decides, the others advise).
Exit codes: 0 ok; 2 bad use; 3 deadlock or the round limit, which is the owner's call; 4 no member answered.
"""
import argparse
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import broker  # noqa: E402

ASK = HERE.parent / "skills" / "ask" / "ask.py"
CACHE_DIR = os.environ.get("ASK_CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "ask")
RULES = ("majority", "unanimous", "veto", "arbiter")
KINDS = ("plan", "roles")
CHOICES = ("yes", "no", "veto")
THREAD = re.compile(r"\[conversation ([^\s:\]]+): carry on with --thread")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

REPLY_SHAPE = """Answer with one JSON object and nothing else:
{"say": "your message to the room", "propose": {"kind": "plan|roles", "body": "..."}, "ask_owner": "a question"}
Only "say" is required. Leave out what you do not need."""
VOTE_SHAPE = """Vote with one JSON object and nothing else:
{"vote": "yes|no|veto", "why": "one or two sentences"}"""


def rooms_dir():
    # Beside the journal, out of every git (the owner, 7 November 2026).
    return Path(os.environ.get("ASK_ROOMS_DIR") or os.path.join(CACHE_DIR, "rooms"))


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class Room:
    def __init__(self, path):
        self.path = Path(path)
        self.config_path = self.path / "room.json"
        self.transcript_path = self.path / "transcript.jsonl"
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))

    @classmethod
    def find(cls, name):
        path = Path(name)
        if not (path / "room.json").is_file():
            matches = sorted(p for p in rooms_dir().glob("*") if (p / "room.json").is_file()
                             and (p.name == name or p.name.endswith("-" + name)))
            if not matches:
                raise SystemExit(f"No room {name!r} in {rooms_dir()}; room.py list shows them.")
            path = matches[-1]
        return cls(path)

    @classmethod
    def create(cls, slug, members, rule="veto", arbiter=None, max_rounds=6, topic=""):
        if not SLUG.match(slug):
            raise ValueError("a slug is lowercase letters, digits and dashes")
        if rule not in RULES:
            raise ValueError(f"rule is one of {', '.join(RULES)}")
        if not members:
            raise ValueError("a room needs at least one member")
        names = [m["name"] for m in members]
        if len(set(names)) != len(names):
            raise ValueError("member names must differ")
        if rule == "arbiter" and arbiter not in names:
            raise ValueError("the arbiter rule needs --arbiter, one of the members")
        path = rooms_dir() / f"{datetime.date.today().isoformat()}-{slug}"
        if path.exists():
            raise ValueError(f"{path} already exists")
        path.mkdir(parents=True)
        config = {"slug": slug, "topic": topic, "created": now(), "level": 1, "rule": rule, "arbiter": arbiter,
                  "max_rounds": max_rounds, "moderator": "claude",
                  "members": [{"name": m["name"], "ask": m["ask"], "thread": None, "seen": 0, "veto_used": False}
                              for m in members]}
        broker.atomic_json(path / "room.json", config)
        room = cls(path)
        room.append({"kind": "open", "by": "moderator", "text": topic, "members": names, "rule": rule})
        return room

    def save(self):
        broker.atomic_json(self.config_path, self.config)

    def entries(self):
        if not self.transcript_path.exists():
            return []
        out = []
        for line in self.transcript_path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def append(self, item):
        entries = self.entries()
        item = broker.redact(dict(item, n=len(entries) + 1, time=now()))
        with self.transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        return item

    def member(self, name):
        for member in self.config["members"]:
            if member["name"] == name:
                return member
        raise SystemExit(f"No member {name!r}; members: {', '.join(m['name'] for m in self.config['members'])}")

    def proposal(self, pid):
        for item in self.entries():
            if item.get("kind") == "proposal" and item.get("id") == pid:
                return item
        raise SystemExit(f"No proposal {pid!r} in this room.")

    def rounds_since_owner(self):
        count = 0
        for item in reversed(self.entries()):
            if item.get("kind") == "owner":
                break
            if item.get("kind") == "round":
                count += 1
        return count


def render(item):
    kind, by = item.get("kind"), item.get("by", "")
    if kind == "owner":
        return f"[{item['n']}] the owner{' to ' + item['to'] if item.get('to') else ''}: {item.get('text', '')}"
    if kind == "ask":
        return f"[{item['n']}] moderator asks{' ' + item['to'] if item.get('to') else ''}: {item.get('text', '')}"
    if kind == "say":
        return f"[{item['n']}] {by}: {item.get('text', '')}"
    if kind == "proposal":
        return f"[{item['n']}] proposal {item['id']} ({item['proposal']}) by {by}:\n{item.get('text', '')}"
    if kind == "vote":
        return f"[{item['n']}] {by} votes {item.get('choice')} on {item.get('proposal')}: {item.get('why', '')}"
    if kind == "result":
        return f"[{item['n']}] proposal {item.get('proposal')}: {item.get('outcome')} ({item.get('tally')})"
    if kind == "ask_owner":
        return f"[{item['n']}] {by} asks the owner: {item.get('text', '')}"
    if kind in ("note", "round", "open"):
        label = {"note": "moderator", "round": "moderator, a round", "open": "room opened"}[kind]
        return f"[{item['n']}] {label}: {item.get('text', '')}".rstrip(": ")
    if kind == "error":
        return f"[{item['n']}] {by} did not answer: {item.get('text', '')}"
    return f"[{item.get('n')}] {kind}: {item.get('text', '')}"


def parse_reply(text):
    """The member's JSON object, from the whole answer or its first {...}; otherwise the answer as say."""
    text = (text or "").strip()
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        candidates.insert(0, fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value, True
    return {"say": text}, False


def ask_command(member, prompt):
    base = os.environ.get("ROOM_ASK_CMD")
    command = shlex.split(base) if base else [sys.executable, str(ASK)]
    command += shlex.split(member["ask"])
    if member.get("thread"):
        command += ["--thread", member["thread"]]
    return command + [prompt]


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def clean_answer(stdout):
    """ask.py's answer without its header lines ([model, pool], [passed over: ...]); the model from the header."""
    lines, model = ANSI.sub("", stdout or "").splitlines(), None
    while lines and (not lines[0].strip() or re.match(r"^\[.*\]$", lines[0].strip())):
        line = lines.pop(0).strip()
        if line.startswith("[") and "," in line and not line.startswith(("[passed over", "[conversation")):
            model = model or line[1:].split(",", 1)[0]
    return "\n".join(lines).strip(), model


# ask.py's own notes on stderr ([reasoning: <file>], [conversation ...]) say where things are, not what went wrong.
NOTE_LINE = re.compile(r"^\[(?:reasoning|conversation|passed over)\b")


def cause(stderr):
    """The line of ask.py's stderr that says why a turn failed: the last one that is not one of its notes."""
    lines = [l.strip() for l in ANSI.sub("", stderr or "").splitlines() if l.strip()]
    told = [l for l in lines if not NOTE_LINE.match(l)]
    return (told or lines or ["no output"])[-1][:300]


def call_member(member, prompt, timeout):
    try:
        done = subprocess.run(ask_command(member, prompt), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, None, f"no answer in {timeout} s"
    found = THREAD.findall(done.stderr or "")
    thread = found[-1] if found else None
    if done.returncode != 0 or not (done.stdout or "").strip():
        return None, thread, f"exit {done.returncode}: {cause(done.stderr)}"
    answer, model = clean_answer(done.stdout)
    if model:
        member["model"] = model
    return answer or done.stdout.strip(), thread, None


def brief(room, member, task):
    """What a member reads this turn: who it is, the room's rules, the transcript it has not seen, the task."""
    entries = room.entries()
    # A member without a carried conversation (a local model) reads the whole transcript every turn.
    unseen = entries[member["seen"]:] if member.get("thread") else entries
    names = ", ".join(m["name"] for m in room.config["members"] if m["name"] != member["name"]) or "nobody else"
    head = (f"You are {member['name']}, a member of a room of agents ({names}) moderated by Claude for the owner. "
            f"Topic: {room.config.get('topic') or 'see the transcript'}. The room decides by the {room.config['rule']} "
            "rule. You only read and talk here: do not edit files or run commands that change anything. "
            "Some members may not run commands at all: if a tool or command is refused, answer from the transcript "
            "and what you know without it. "
            "Be brief, say where you agree or disagree with the others, and add what is new.")
    lines = [render(e) for e in unseen if e.get("kind") not in ("open",)]
    return f"{head}\n\nThe transcript since your last turn:\n" + ("\n".join(lines) or "(nothing yet)") + f"\n\n{task}"


RETRY_NOTE = ("Your last attempt at this turn ended without an answer. Answer now from the transcript only, "
              "without tools or commands.")


def take_turn(room, member, task, timeout):
    answer, thread, error = call_member(member, brief(room, member, task), timeout)
    if error and error.startswith("exit 2:"):
        # Once, from the transcript only: a member that may not run commands ends its turn without text (exit 2) on
        # a refused one. A spent quota or a policy refusal (other exits) would only fail again.
        if thread:
            member["thread"] = thread
        answer, again, second = call_member(member, brief(room, member, task + "\n\n" + RETRY_NOTE), timeout)
        thread = again or thread
        error = f"{error}; once more from the transcript: {second}" if second else None
    return member["name"], answer, thread, error, task


def record_turn(room, name, answer, thread, error, task=""):
    member = room.member(name)
    if thread:
        member["thread"] = thread
    member["seen"] = len(room.entries())
    if error:
        item = room.append({"kind": "error", "by": name, "text": error, "task": task})
        room.save()
        return [item]
    reply, structured = parse_reply(answer)
    said = room.append({"kind": "say", "by": name, "text": str(reply.get("say") or answer), "structured": structured})
    out = [said]
    proposal = reply.get("propose")
    if isinstance(proposal, dict) and proposal.get("kind") in KINDS and proposal.get("body"):
        out.append(add_proposal(room, proposal["kind"], proposal["body"], name))
    if reply.get("ask_owner"):
        out.append(room.append({"kind": "ask_owner", "by": name, "text": str(reply["ask_owner"])}))
    member["seen"] = len(room.entries())
    room.save()
    return out


def add_proposal(room, kind, body, by):
    count = sum(1 for e in room.entries() if e.get("kind") == "proposal")
    body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, indent=1)
    return room.append({"kind": "proposal", "id": f"p{count + 1}", "proposal": kind, "by": by, "text": body})


def tally(room, pid, votes):
    """votes: {member: choice}. Returns (outcome, tally text). Outcomes: agreed, rejected, vetoed, waiting."""
    yes = sum(1 for c in votes.values() if c == "yes")
    no = sum(1 for c in votes.values() if c == "no")
    vetoes = [n for n, c in votes.items() if c == "veto"]
    text = f"{yes} yes, {no} no" + (f", veto by {', '.join(vetoes)}" if vetoes else "")
    rule = room.config["rule"]
    if rule == "arbiter":
        choice = votes.get(room.config.get("arbiter"))
        if choice is None:
            return "waiting", text + "; the arbiter did not vote"
        return ("agreed" if choice == "yes" else "rejected"), text + f"; the arbiter said {choice}"
    if rule == "veto" and vetoes:
        return "vetoed", text
    if rule == "unanimous":
        return ("agreed" if votes and yes == len(votes) else "rejected"), text
    return ("agreed" if yes > no else "rejected"), text


def parse_vote(answer):
    reply, _ = parse_reply(answer)
    choice = str(reply.get("vote") or "").strip().lower()
    if choice not in CHOICES:
        # A plain-text vote: its first word.
        word = (answer or "").strip().split(maxsplit=1)
        choice = word[0].lower().strip(".,:;!") if word else ""
        if choice not in CHOICES:
            return None, (answer or "")[:300]
    return choice, str(reply.get("why") or "")


def members_for(room, to=None):
    return [room.member(to)] if to else list(room.config["members"])


def parallel_turns(room, members, task, timeout):
    workers = max(1, len(members))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(take_turn, room, m, task, timeout) for m in members]
        results = []
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    # Answers land in the order they finished.
    return [item for r in results for item in record_turn(room, *r)]


def answered(entries):
    return 0 if any(e.get("kind") == "say" for e in entries) else 4


def cmd_new(a):
    members = []
    for spec in a.member:
        name, sep, args = spec.partition("=")
        if not sep or not name.strip() or not args.strip():
            raise SystemExit("--member is name=<ask.py arguments>, e.g. codex=--codex gpt-5.5")
        members.append({"name": name.strip(), "ask": args.strip()})
    try:
        room = Room.create(a.slug, members, a.rule, a.arbiter, a.max_rounds, a.topic or "")
    except ValueError as error:
        raise SystemExit(str(error))
    print(room.path)
    return 0


def cmd_say(a):
    room = Room.find(a.room)
    # The moderator's own question (--as moderator) is not the owner's word and does not end a deadlock.
    kind = "owner" if a.as_ == "owner" else "ask"
    room.append({"kind": kind, "text": a.text, **({"to": a.to} if a.to else {})})
    who = "the owner's" if kind == "owner" else "the moderator's"
    out = parallel_turns(room, members_for(room, a.to), f"Answer {who} last message. " + REPLY_SHAPE, a.timeout)
    for item in out:
        print(render(item))
    return answered(out)


def cmd_round(a):
    room = Room.find(a.room)
    if room.rounds_since_owner() >= room.config["max_rounds"]:
        print(f"{room.config['max_rounds']} rounds since the owner last spoke: a deadlock is the owner's call. "
              "Summarise the positions for the owner and wait for their word (room.py say).", file=sys.stderr)
        return 3
    room.append({"kind": "round", "text": a.note or ""})
    task = (a.note + "\n\n" if a.note else "") + "Your turn in this round. " + REPLY_SHAPE
    out = []
    # In order, so each member sees the turns before it in the same round.
    for member in room.config["members"]:
        result = take_turn(room, member, task, a.timeout)
        for item in record_turn(room, *result):
            out.append(item)
            print(render(item), flush=True)
    return answered(out)


def cmd_propose(a):
    room = Room.find(a.room)
    body = Path(a.file).read_text(encoding="utf-8") if a.file != "-" else sys.stdin.read()
    if a.by != "moderator":
        room.member(a.by)
    print(render(add_proposal(room, a.kind, body.strip(), a.by)))
    return 0


def cmd_vote(a):
    room = Room.find(a.room)
    proposal = room.proposal(a.proposal)
    task = (f"Vote on proposal {proposal['id']} ({proposal['proposal']}):\n{proposal['text']}\n\n"
            + ("You still have your one veto in this room; spend it only on something you cannot accept. "
               if room.config["rule"] == "veto" else "")
            + VOTE_SHAPE)
    votes = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(room.config["members"])) as pool:
        results = list(pool.map(lambda m: take_turn(room, m, task, a.timeout), room.config["members"]))
    for name, answer, thread, error, _ in results:
        member = room.member(name)
        if thread:
            member["thread"] = thread
        if error:
            room.append({"kind": "error", "by": name, "text": error})
            continue
        choice, why = parse_vote(answer)
        if choice is None:
            room.append({"kind": "error", "by": name, "text": "no vote in the answer: " + why})
            continue
        if choice == "veto" and (room.config["rule"] != "veto" or member["veto_used"]):
            why = f"(a veto it could not use, counted as no) {why}"
            choice = "no"
        if choice == "veto":
            member["veto_used"] = True
        votes[name] = choice
        print(render(room.append({"kind": "vote", "by": name, "proposal": proposal["id"], "choice": choice,
                                  "why": why})))
    for member in room.config["members"]:
        member["seen"] = len(room.entries())
    room.save()
    if not votes:
        print("No member voted.", file=sys.stderr)
        return 4
    outcome, text = tally(room, proposal["id"], votes)
    print(render(room.append({"kind": "result", "proposal": proposal["id"], "outcome": outcome, "tally": text})))
    if outcome == "vetoed":
        print("Vetoed: back to discussion (room.py round), with the veto's reason as the note.", file=sys.stderr)
    return 0


def cmd_retry(a):
    room = Room.find(a.room)
    member = room.member(a.member)
    failed = [e for e in room.entries() if e.get("kind") == "error" and e.get("by") == a.member and e.get("task")]
    if not failed:
        raise SystemExit(f"{a.member} has no failed turn to take again.")
    task = failed[-1]["task"]
    out = record_turn(room, *take_turn(room, member, task, a.timeout))
    for item in out:
        print(render(item))
    return answered(out)


def cmd_note(a):
    room = Room.find(a.room)
    print(render(room.append({"kind": "note", "text": a.text})))
    return 0


def cmd_tail(a):
    room = Room.find(a.room)
    print(f"{room.path}  rule {room.config['rule']}  members "
          + ", ".join(m["name"] + (" (veto used)" if m.get("veto_used") else "") for m in room.config["members"]))
    for item in room.entries()[-a.n:]:
        print(render(item))
    return 0


def cmd_list(a):
    for path in sorted(rooms_dir().glob("*/room.json")):
        config = json.loads(path.read_text(encoding="utf-8"))
        print(f"{path.parent.name}  {config.get('rule')}  {', '.join(m['name'] for m in config.get('members', []))}"
              f"  {config.get('topic', '')[:60]}")
    return 0


def parser():
    p = argparse.ArgumentParser(prog="room.py", description="A superchat room: several agents in one transcript.")
    sub = p.add_subparsers(dest="command", required=True)
    n = sub.add_parser("new", help="open a room")
    n.add_argument("slug")
    n.add_argument("--member", action="append", default=[], help="name=<ask.py arguments> (repeatable, in turn order)")
    n.add_argument("--rule", choices=RULES, default="veto")
    n.add_argument("--arbiter", help="with --rule arbiter: the member who decides")
    n.add_argument("--max-rounds", type=int, default=6, help="rounds without the owner before it is a deadlock")
    n.add_argument("--topic", help="what the room is about")
    for name, helptext in (("say", "the owner's message; members answer at once"),
                           ("round", "members take turns in order"), ("vote", "every member votes on a proposal")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("room")
        if name == "say":
            s.add_argument("text")
            s.add_argument("--to", help="one member answers; the others only read")
            s.add_argument("--as", dest="as_", choices=("owner", "moderator"), default="owner",
                           help="whose words these are: the owner's (default) or the moderator's own question")
        if name == "round":
            s.add_argument("--note", help="the moderator's word for this round")
        if name == "vote":
            s.add_argument("proposal")
        s.add_argument("--timeout", type=int, default=1800, help="seconds for one member's turn")
    s = sub.add_parser("retry", help="take a member's last failed turn again")
    s.add_argument("room")
    s.add_argument("member")
    s.add_argument("--timeout", type=int, default=1800, help="seconds for one member's turn")
    s = sub.add_parser("propose", help="put a plan or a split of roles to the vote")
    s.add_argument("room")
    s.add_argument("kind", choices=KINDS)
    s.add_argument("--file", required=True, help="the proposal's text; - reads it from stdin")
    s.add_argument("--by", default="moderator")
    s = sub.add_parser("note", help="the moderator writes without asking anyone")
    s.add_argument("room")
    s.add_argument("text")
    s = sub.add_parser("tail", help="the transcript's last entries")
    s.add_argument("room")
    s.add_argument("-n", type=int, default=30)
    sub.add_parser("list", help="the rooms")
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    return {"new": cmd_new, "say": cmd_say, "round": cmd_round, "propose": cmd_propose, "vote": cmd_vote,
            "note": cmd_note, "retry": cmd_retry, "tail": cmd_tail, "list": cmd_list}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
