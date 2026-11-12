"""Room chat: talk directly to a room in a terminal.

    python3 tools/chat.py [ROOM] [--new SLUG --member NAME=ARGS ...]
                          [--topic TEXT] [--rule RULE] [--arbiter NAME]
                          [--timeout SECONDS] [--no-color] [-n N]

Commands: /help, /round [note], /tail [n], /members, /claude [MODEL [NAME]], /add NAME=ARGS, /rooms, /quit, /exit.
"""
import argparse
import os
import shutil
import sys
import textwrap

try:
    import readline  # noqa: F401
except ImportError:
    pass

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import room  # noqa: E402

PALETTE = (36, 33, 35, 32, 34, 31)
HELP = ("/help  list commands", "/round [note]  take one council round", "/tail [n]  show the transcript (default 20)",
        "/members  show members", "/claude  list the Claude models",
        "/claude MODEL [NAME]  add a Claude model (opus, sonnet, haiku, fable or an id) to the room; then @NAME",
        "/add NAME=ARGS  add any member by its ask.py arguments, e.g. /add codex=--codex gpt-5.5", "/rooms  list rooms", "/quit  leave room chat", "/exit  leave room chat")
INTERRUPTED = "[interrupted: the answers still running are recorded as they end]"


def style(text, colors, name=None, weight=None):
    if colors is None:
        return text
    codes = ([str(weight)] if weight else []) + ([str(colors[name])] if name in colors else [])
    return f"\033[{';'.join(codes)}m{text}\033[0m" if codes else text


def show(item, colors):
    """Render a transcript entry, preserving its line breaks and wrapping message text."""
    kind, name = item.get("kind"), item.get("by", "")
    if kind in ("say", "owner"):
        label = name if kind == "say" else "you" + (" → " + item["to"] if item.get("to") else "")
        head = style(label, colors, name if kind == "say" else None, 1)
        if kind == "say":
            meta = []
            if item.get("model"):
                meta.append(str(item["model"]))
            if item.get("seconds") is not None:
                meta.append(f"{item['seconds']} s")
            if meta:
                head += style(" " + " · ".join(meta), colors, weight=2)
        width = max(3, min(shutil.get_terminal_size().columns, 100))
        lines = []
        for line in item.get("text", "").split("\n"):
            # Keep a line's own indent, and never cut a URL or a long word in two.
            indent = "  " + line[:len(line) - len(line.lstrip(" "))]
            lines.extend(textwrap.wrap(line.lstrip(" "), width, initial_indent=indent, subsequent_indent=indent,
                                       break_long_words=False, break_on_hyphens=False) or [indent])
        return head + "\n" + "\n".join(lines)
    if kind == "error":
        return style(f"{name} did not answer: {item.get('text', '')}", colors, name, 2)
    if kind == "ask_owner":
        return style(f"{name} asks you: {item.get('text', '')}", colors, name, 1)
    return style(room.render(item), colors, weight=2)


def show_members(current, out, colors):
    for member in current.config["members"]:
        print(style(member["name"], colors, member["name"]) + "  " + (member.get("model") or member["ask"])
              + (" (veto used)" if member.get("veto_used") else ""), file=out)


def run(current, lines, out, colors, timeout):
    """Read owner lines and share the room's existing transcript and turn functions."""
    def emit(item):
        print(show(item, colors) + "\n", file=out, flush=True)

    iterator = iter(lines)
    while True:
        try:
            line = next(iterator).rstrip("\r\n")
        except (StopIteration, EOFError):
            return 0
        except KeyboardInterrupt:
            print(file=out)
            continue
        if not line.strip():
            continue
        try:
            if line.startswith("/"):
                command, _, arg = line.partition(" ")
                arg = arg.strip()
                if command in ("/quit", "/exit"):
                    return 0
                if command == "/help":
                    print("\n".join(HELP), file=out)
                elif command == "/members":
                    show_members(current, out, colors)
                elif command in ("/claude", "/add"):
                    if command == "/claude":
                        words = arg.split()
                        if not words or len(words) > 2:
                            models = room.claude_models()
                            print("Claude models: " + ", ".join(models) + "\nUsage: /claude MODEL [NAME], e.g. /claude opus",
                                  file=out)
                            continue
                        model = room.claude_model(words[0])
                        name = words[1] if len(words) > 1 else room.claude_name(model)
                        ask = "--claude " + model
                    else:
                        name, sep, ask = arg.partition("=")
                        if not sep:
                            print("Usage: /add NAME=ARGS, e.g. /add codex=--codex gpt-5.5", file=out)
                            continue
                    try:
                        emit(current.add_member(name, ask))
                    except ValueError as error:
                        print(str(error), file=out)
                        continue
                    if colors is not None:
                        colors[name.strip()] = PALETTE[(len(current.config["members"]) - 1) % len(PALETTE)]
                    print(f"Talk to it with @{name.strip()}, or to everyone as before.", file=out)
                elif command == "/rooms":
                    for path in room.list_rooms():
                        print(("* " if path.resolve() == current.path.resolve() else "  ") + path.name, file=out)
                elif command == "/tail":
                    try:
                        count = int(arg) if arg else 20
                        if count < 0:
                            raise ValueError
                    except ValueError:
                        print("Usage: /tail [non-negative number]", file=out)
                        continue
                    for item in current.entries()[-count:] if count else []:
                        emit(item)
                elif command == "/round":
                    if current.rounds_since_owner() >= current.config["max_rounds"]:
                        print(f"{current.config['max_rounds']} rounds since the owner last spoke: a deadlock is the owner's call. "
                              "Summarise the positions for the owner and wait for their word (room.py say).", file=out)
                        continue
                    current.append({"kind": "round", "text": arg})
                    task = (arg + "\n\n" if arg else "") + "Your turn in this round. " + room.REPLY_SHAPE
                    room.round_turns(current, task, timeout, on_item=emit)
                else:
                    print(f"Unknown command {command}; /help lists them.", file=out)
                continue
            to = None
            text = line
            if line.startswith("@"):
                word, _, text = line[1:].partition(" ")
                names = [m["name"] for m in current.config["members"]]
                if word not in names:
                    print(f"No member '{word}'; members: {', '.join(names)}", file=out)
                    continue
                if not text.strip():
                    print(f"Write the message after @{word}.", file=out)
                    continue
                to = word
            current.append({"kind": "owner", "text": text, **({"to": to} if to else {})})
            members = room.members_for(current, to)
            print(style("… waiting for " + ", ".join(m["name"] for m in members), colors, weight=2),
                  file=out, flush=True)
            room.parallel_turns(current, members, "Answer the owner's last message. " + room.REPLY_SHAPE,
                                timeout, on_item=emit,
                                on_interrupt=lambda: print(INTERRUPTED, file=out, flush=True))
        except KeyboardInterrupt:
            continue


def prompt_lines():
    while True:
        try:
            yield input("you> ")
        except EOFError:
            return
        except KeyboardInterrupt:
            print()


def parser():
    p = argparse.ArgumentParser(prog="chat.py", description="Room chat: talk directly to a room in a terminal.")
    p.add_argument("room", nargs="?", metavar="ROOM", help="room slug or folder; default: the room last talked in")
    p.add_argument("--new", metavar="SLUG", help="open a new room")
    p.add_argument("--member", action="append", default=[], metavar="NAME=ARGS", help="member ask.py arguments (repeatable)")
    p.add_argument("--topic", default="", help="what the room is about")
    p.add_argument("--rule", choices=room.RULES, default="veto")
    p.add_argument("--arbiter", help="member who decides with the arbiter rule")
    p.add_argument("--timeout", type=int, default=1800, metavar="SECONDS")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("-n", type=int, default=10, help="transcript entries shown on start (default: 10)")
    return p


def main(argv=None, stdin=None, out=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if stdin is None and not sys.stdin.isatty() and hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    out = sys.stdout if out is None else out
    p = parser()
    try:
        args = p.parse_args(argv)
        if args.room and args.new:
            p.error("ROOM and --new cannot be used together")
        if args.n < 0:
            p.error("-n must be non-negative")
    except SystemExit as error:
        return int(error.code)
    try:
        if args.new:
            current = room.open_room(args.new, args.member, args.rule, args.arbiter, topic=args.topic)
        elif args.room:
            current = room.Room.find(args.room)
        else:
            paths = room.list_rooms()
            if not paths:
                print('Open a room: chat.py --new <slug> --member "codex=--role review" '
                      '--member "agy=--role second-opinion"', file=out)
                return 2
            # The room last talked in, not the last by name: two rooms of one day sort by slug.
            current = room.Room(max(paths, key=lambda p: ((p / "transcript.jsonl").stat().st_mtime
                                                          if (p / "transcript.jsonl").exists() else 0, p.name)))
    except (ValueError, SystemExit) as error:
        print(str(error), file=sys.stderr)
        return 2
    colors = None
    if not args.no_color and not os.environ.get("NO_COLOR") and sys.stdout.isatty():
        colors = {m["name"]: PALETTE[i % len(PALETTE)] for i, m in enumerate(current.config["members"])}
        if os.name == "nt":
            os.system("")
    print(f"{current.path.name}\nTopic: {current.config.get('topic', '')}\nRule: {current.config['rule']}", file=out)
    show_members(current, out, colors)
    for item in current.entries()[-args.n:] if args.n else []:
        print(show(item, colors) + "\n", file=out)
    print("Type to talk to everyone, @name to talk to one; /help for commands.", file=out)
    lines = prompt_lines() if stdin is None and sys.stdin.isatty() else (sys.stdin if stdin is None else stdin)
    return run(current, lines, out, colors, args.timeout)


if __name__ == '__main__':
    sys.exit(main())
