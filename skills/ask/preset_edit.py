"""Tune the presets in your ~/.config/ask/roles.json: the manual preset's order of each role's rows (a row switched off,
a model of this machine added) and the weighted presets' weights (quality, time, cost).

With no change it shows every preset and every role's rows, numbered. ask.py --preset edit runs this, and the
dashboard's presets page makes the same edits through the same code."""

import argparse
from collections import Counter
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import route

WEIGHTS = ("quality", "time", "cost")
WEIGHT_MAX = 5.0
# A weighted preset without its own quality weight takes quality as it is (score.rank: S = quality x Q - ...).
DEFAULT_QUALITY = 1.0
ROW_KEYS = ("prefer", "models", "names")


class EditError(ValueError):
    """An edit that cannot be applied: nothing is written."""


class Conflict(EditError):
    """The file changed since the page or the command read it."""


def canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def row_label(row, shared=None):
    """A role row as one line: 'gemini-4-argon* · agy · effort >=high', or 'any of: a | b · effort >=high'."""
    if isinstance(row, str):
        return row
    if not isinstance(row, dict):
        return json.dumps(row)
    if isinstance(row.get("any"), list):
        group = {k: v for k, v in row.items() if k != "any"}
        members = " | ".join(row_label(m) for m in row["any"])
        return "any of: " + members + "".join(f" · {k} {v}" if k == "effort" else f" · {v}" for k, v in group.items()
                                              if k in ("provider", "effort"))
    name = row.get("model") or row.get("family") or "?"
    parts = [name]
    if row.get("provider"):
        parts.append(row["provider"])
    if row.get("effort"):
        parts.append(f"effort {row['effort']}")
    return " · ".join(parts)


def _number(value, key):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise EditError(f"{key}: a number is needed, not {value!r}")
    if not 0 <= float(value) <= WEIGHT_MAX:
        raise EditError(f"{key}: between 0 and {WEIGHT_MAX:g}, not {value!r}")
    return round(float(value), 3)


def _rows(spec):
    return list(route.role_rows(spec))


def _off(spec):
    return list(spec.get("off") or []) if isinstance(spec, dict) else []


def write_json_atomic(path, value):
    """Through a link to the file it names (the owner's ~/.config/ask/roles.json links into a git checkout): a temporary
    file beside the target, then a rename over it, so a reader never sees half a file and the link stays a link."""
    target = os.path.realpath(path)
    folder = os.path.dirname(target)
    os.makedirs(folder, exist_ok=True)
    mode = os.stat(target).st_mode & 0o777 if os.path.exists(target) else 0o644
    fd, tmp = tempfile.mkstemp(prefix=".roles-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return target


class Editor:
    """The shipped roles.json and the user's file over it; reads, previews and saves edits."""

    def __init__(self, user_path=None, defaults_path=None, fake=None, context=None):
        self.user_path = user_path or route.USER_ROLES_PATH
        self.defaults_path = defaults_path or route.ROLES_PATH
        self.fake = fake
        self.context = context
        self._lock = threading.Lock()

    def read(self):
        """-> (defaults, overlay, version); version is a hash of the user's file as it is on disk ('' when absent)."""
        with open(self.defaults_path, encoding="utf-8") as f:
            defaults = json.load(f)
        try:
            raw = Path(self.user_path).read_bytes()
        except FileNotFoundError:
            return defaults, {}, ""
        overlay = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        if not isinstance(overlay, dict):
            raise EditError(f"{self.user_path}: not a JSON object")
        return defaults, overlay, hashlib.sha256(raw).hexdigest()[:16]

    def inventory(self, rules):
        """This machine's models, as rows a role can take: [{"provider", "model", "label"}]."""
        try:
            rows, _ = route.live_inventory(fake=self.fake, rules=rules)
        except Exception:
            return []
        seen, out = set(), []
        for r in rows:
            key = (r["provider"], r["id"])
            if key not in seen:
                seen.add(key)
                out.append({"provider": r["provider"], "model": r["id"], "label": f"{r['id']} · {r['provider']}"})
        return out

    def apply(self, defaults, overlay, edits, inventory=None):
        """The user's file with the edits made: [{"preset": name, "weights": {...}} | {"role": name, "rows": [...],
        "off": [...]}]. A role's rows may be reordered, switched off and on, and take models of this machine
        (inventory); nothing else in the role changes. A role or preset that ends equal to the shipped one leaves the
        user's file."""
        if not isinstance(edits, list) or not edits:
            raise EditError("no edits")
        new = copy.deepcopy(overlay)
        for edit in edits:
            rules = route.merge_roles(defaults, new)
            if not isinstance(edit, dict):
                raise EditError(f"an edit is an object, not {edit!r}")
            if "preset" in edit:
                self._apply_preset(defaults, rules, new, edit)
            elif "role" in edit:
                self._apply_role(defaults, rules, new, edit, inventory)
            else:
                raise EditError("an edit names a preset or a role")
        return new

    def _apply_preset(self, defaults, rules, new, edit):
        name = edit["preset"]
        presets = rules.get("presets") or {}
        if name == "manual":
            raise EditError("the manual preset has no weights: its order is each role's rows")
        if name not in presets:
            raise EditError(f"unknown preset {name!r}; known presets: {', '.join(presets)}")
        weights = edit.get("weights")
        if not isinstance(weights, dict) or not weights:
            raise EditError(f"{name}: weights are an object of {', '.join(WEIGHTS)}")
        unknown = sorted(set(weights) - set(WEIGHTS))
        if unknown:
            raise EditError(f"{name}: unknown weight {', '.join(unknown)}; known: {', '.join(WEIGHTS)}")
        value = dict(presets[name], **{k: _number(v, f"{name}.{k}") for k, v in weights.items()})
        shipped = (defaults.get("presets") or {}).get(name)
        user = dict(new.get("presets") or {})
        if shipped is not None and value == shipped:
            user.pop(name, None)
        else:
            user[name] = value
        if user:
            new["presets"] = user
        else:
            new.pop("presets", None)

    def _apply_role(self, defaults, rules, new, edit, inventory):
        name = edit["role"]
        spec = (rules.get("roles") or {}).get(name)
        if spec is None:
            raise EditError(f"unknown role {name!r}")
        rows, off = edit.get("rows"), edit.get("off", [])
        if not isinstance(rows, list) or not isinstance(off, list):
            raise EditError(f"{name}: rows and off are lists")
        if not rows:
            raise EditError(f"{name}: at least one row stays on")
        before = Counter(canon(r) for r in _rows(spec) + _off(spec))
        after = Counter(canon(r) for r in rows + off)
        if before - after:
            # Rows are switched off, never dropped: off keeps them for the next edit.
            raise EditError(f"{name}: a row went missing; switch it off instead")
        offered = {(m["provider"], m["model"]) for m in (inventory if inventory is not None else self.inventory(rules))}
        for added in (after - before).elements():
            row = json.loads(added)
            if not (isinstance(row, dict) and set(row) == {"provider", "model"}
                    and (row["provider"], row["model"]) in offered):
                raise EditError(f"{name}: only a model of this machine can be added ({{provider, model}}), not {added}")
        base = spec if isinstance(spec, dict) else {"prefer": spec}
        value = {}
        for k, v in base.items():
            # The rows where the role had them, so the user's file shows a small diff.
            if k in ROW_KEYS and "prefer" not in value:
                value["prefer"] = rows
                if off:
                    value["off"] = off
            elif k not in ROW_KEYS + ("off",):
                value[k] = v
        shipped = (defaults.get("roles") or {}).get(name)
        user = dict(new.get("roles") or {})
        if isinstance(shipped, dict) and canon(value) == canon(dict(
                {k: v for k, v in shipped.items() if k not in ROW_KEYS}, prefer=_rows(shipped))):
            user.pop(name, None)
        else:
            user[name] = value
        if user:
            new["roles"] = user
        else:
            new.pop("roles", None)

    def picks(self, rules):
        return picks_table(rules, fake=self.fake, context=self.context)

    def state(self, with_picks=True):
        defaults, overlay, version = self.read()
        rules = route.merge_roles(defaults, overlay)
        presets = []
        for name in ["manual"] + [n for n in (rules.get("presets") or {}) if n != "manual"]:
            weights = dict((rules.get("presets") or {}).get(name) or {})
            if name != "manual":
                weights.setdefault("quality", DEFAULT_QUALITY)
            presets.append({"name": name, "weighted": name != "manual",
                            "weights": {k: weights.get(k) for k in WEIGHTS} if name != "manual" else {},
                            "own": name in (overlay.get("presets") or {})})
        roles = []
        for name, spec in (rules.get("roles") or {}).items():
            roles.append({"name": name, "scored": isinstance(spec, dict) and "score" in spec,
                          "own": name in (overlay.get("roles") or {}),
                          "rows": [{"row": r, "label": row_label(r)} for r in _rows(spec)],
                          "off": [{"row": r, "label": row_label(r)} for r in _off(spec)]})
        try:
            active = route.resolve_preset(rules=rules, machine=route.load_machine(fake=self.fake))
        except ValueError as e:
            active = {"preset": "manual", "source": "default", "error": str(e)}
        return {
            "path": self.user_path, "target": os.path.realpath(self.user_path), "version": version,
            "ignored": (f"ASK_ROLES names {os.environ['ASK_ROLES']}: the router reads that file and nothing over it, "
                        f"so these edits do not change routing" if os.environ.get("ASK_ROLES") else None),
            "active": active, "presets": presets, "roles": roles, "models": self.inventory(rules),
            "picks": self.picks(rules) if with_picks else None,
        }

    def preview(self, edits):
        """Whom the router would pick now for every role under the edits, written nowhere."""
        defaults, overlay, _ = self.read()
        rules = route.merge_roles(defaults, overlay)
        new = self.apply(defaults, overlay, edits, self.inventory(rules))
        return {"picks": self.picks(route.merge_roles(defaults, new))}

    def save(self, edits, version=None):
        """Write the edits: the file as it was is kept beside the link as roles.json.bak. -> what was written."""
        with self._lock:
            defaults, overlay, current = self.read()
            if version is not None and version != current:
                raise Conflict(f"{self.user_path} changed since it was read; reload and edit again")
            rules = route.merge_roles(defaults, overlay)
            new = self.apply(defaults, overlay, edits, self.inventory(rules))
            if new == overlay:
                return {"written": False, "path": self.user_path, "backup": None}
            backup = None
            target = os.path.realpath(self.user_path)
            if os.path.exists(target):
                backup = self.user_path + ".bak"
                Path(backup).write_bytes(Path(target).read_bytes())
            write_json_atomic(self.user_path, new)
            return {"written": True, "path": self.user_path, "target": target, "backup": backup}


def picks_table(rules, fake=None, context=None):
    """{role: {"picks": {preset: model or None}, "note": why there is no pick}}: a dry run, no call, no reservation.
    A weighted preset only ranks a role that has a score; any other role repeats the manual pick."""
    names = ["manual"] + [n for n in (rules.get("presets") or {}) if n != "manual"]
    out = {}
    for role in rules.get("roles") or {}:
        try:
            decision = route.pick(role, rules=rules, fake=fake, context=context)
        except Exception as e:
            out[role] = {"picks": {n: None for n in names}, "note": str(e)}
            continue
        manual = decision.get("model")
        subscription = decision.get("provider") == "claude-subscription"
        cells = {}
        for name in names:
            model = manual if name == "manual" else (decision.get("preset_picks") or {}).get(name, manual)
            cells[name] = (("claude:" if subscription and model == manual else "") + model) if model else None
        note = None if decision.get("picked") or subscription else decision.get("message")
        out[role] = {"picks": cells, "note": note}
    return out


# --- The command line: ask.py --preset edit ---

def _show_preset(p, out):
    w = p["weights"]
    shown = ", ".join(f"{k} {w[k]:g}" if w.get(k) is not None else f"{k} (the role's own)" for k in WEIGHTS)
    out(f"  {p['name']}: {shown}" + ("  [yours]" if p["own"] else ""))


def _show_role(role, out, pick=None):
    out(f"  {role['name']}" + ("  [yours]" if role["own"] else "") + (f"  -> {pick}" if pick else ""))
    for i, r in enumerate(role["rows"], 1):
        out(f"    {i}. {r['label']}")
    for i, r in enumerate(role["off"], len(role["rows"]) + 1):
        out(f"    {i}. (off) {r['label']}")


def _role_edit(role, args, models):
    rows = [r["row"] for r in role["rows"]]
    off = [r["row"] for r in role["off"]]
    total = len(rows) + len(off)

    def index(n, on=True):
        if on and not 1 <= n <= len(rows):
            raise EditError(f"{role['name']}: row {n} is not on (on: 1-{len(rows)})")
        if not on and not len(rows) < n <= total:
            raise EditError(f"{role['name']}: row {n} is not off" + (f" (off: {len(rows) + 1}-{total})" if off else " (none is)"))
        return n - 1

    if args.move:
        a, b = index(args.move[0]), index(args.move[1])
        rows.insert(b, rows.pop(a))
    elif args.off is not None:
        off.append(rows.pop(index(args.off)))
    elif args.on is not None:
        rows.append(off.pop(index(args.on, on=False) - len(rows)))
    elif args.add:
        provider, sep, model = args.add.partition(":")
        if not sep or not provider or not model:
            raise EditError("--add takes PROVIDER:MODEL, as ask.py --list names them (ollama:qwen3:14b)")
        if not any(m["provider"] == provider and m["model"] == model for m in models):
            raise EditError(f"{args.add}: not a model of this machine (ask.py --list)")
        rows.append({"provider": provider, "model": model})
    else:
        return None
    return {"role": role["name"], "rows": rows, "off": off}


def main(argv=None, out=print, editor=None):
    p = argparse.ArgumentParser(prog="ask.py --preset edit", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("preset", nargs="?", help="the preset to edit: manual (with --role) or a weighted one")
    p.add_argument("--role", help="manual: the role whose rows change")
    for key in WEIGHTS:
        p.add_argument(f"--{key}", type=float, metavar="W", help=f"a weighted preset's {key} weight (0-{WEIGHT_MAX:g})")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--move", nargs=2, type=int, metavar=("FROM", "TO"), help="manual: move row FROM to place TO")
    g.add_argument("--off", type=int, metavar="N", help="manual: switch row N off (kept, not called)")
    g.add_argument("--on", type=int, metavar="N", help="manual: switch row N back on, at the end")
    g.add_argument("--add", metavar="PROVIDER:MODEL", help="manual: add a model of this machine at the end")
    args = p.parse_args(argv)
    editor = editor or Editor(context=route.resolve_context())
    try:
        state = editor.state(with_picks=False)
        if state["ignored"]:
            out(state["ignored"])
        presets = {x["name"]: x for x in state["presets"]}
        roles = {x["name"]: x for x in state["roles"]}
        weights = {k: getattr(args, k) for k in WEIGHTS if getattr(args, k) is not None}
        row_ops = args.move or args.off is not None or args.on is not None or args.add
        if args.preset is None:
            out(f"{state['path']}  (active preset: {state['active']['preset']}, from {state['active']['source']})")
            out("Weighted presets (shadow only for now):")
            for x in state["presets"]:
                if x["weighted"]:
                    _show_preset(x, out)
            out("Manual preset, each role's rows in order:")
            for role in state["roles"]:
                _show_role(role, out)
            return 0
        if args.preset not in presets:
            raise EditError(f"unknown preset {args.preset!r}; known presets: {', '.join(presets)}")
        if args.preset == "manual":
            if weights:
                raise EditError("the manual preset has no weights; --quality, --time and --cost are for a weighted one")
            if not args.role:
                if row_ops:
                    raise EditError("--role names the role whose rows change")
                for role in state["roles"]:
                    _show_role(role, out)
                return 0
            if args.role not in roles:
                raise EditError(f"unknown role {args.role!r}; roles: {', '.join(roles)}")
            edit = _role_edit(roles[args.role], args, state["models"])
            if edit is None:
                _show_role(roles[args.role], out)
                return 0
            affected = [args.role]
        else:
            if row_ops or args.role:
                raise EditError(f"{args.preset} is weighted: it takes --quality, --time and --cost, not rows")
            if not weights:
                _show_preset(presets[args.preset], out)
                return 0
            edit = {"preset": args.preset, "weights": weights}
            affected = [r for r in roles if roles[r]["scored"]]
        result = editor.save([edit], version=state["version"])
    except EditError as e:
        out(f"Nothing written: {e}")
        return 2
    except (OSError, ValueError) as e:
        out(f"Nothing written: {e}")
        return 2
    if not result["written"]:
        out("Nothing to change.")
        return 0
    after = editor.state(with_picks=False)
    if args.preset == "manual":
        role = next(r for r in after["roles"] if r["name"] == args.role)
        _show_role(role, out)
    else:
        _show_preset(next(x for x in after["presets"] if x["name"] == args.preset), out)
    defaults, overlay, _ = editor.read()
    picks = editor.picks(route.merge_roles(defaults, overlay))
    out("Would pick now:")
    for r in affected:
        row = picks.get(r) or {}
        model = (row.get("picks") or {}).get(args.preset)
        out(f"  {r}: {model}" if model else f"  {r}: - ({row.get('note') or 'no pick'})")
    out(f"Written: {result['path']}" + (f" (the file before: {result['backup']})" if result["backup"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
