"""Setting up /ask's context on a machine: the folder marker and a first machine.json.

    ask.py --context-here work|personal write .switchboard-context in this folder: it and every folder under it
    ask.py --init look at this machine, propose ~/.config/ask/machine.json, write it on a yes
    ask.py --init --machine both --work-domain corp.example --yes the same without questions

Every call is either a work task or a personal one: a work task never goes to a personal account, a personal task never
spends a work account. Which one a call is: --context, else ASK_CONTEXT, else the nearest .switchboard-context up from
the folder, else context_paths in machine.json, else work (the safe side). An account is work by its label in
"accounts" or by its email's domain in "work_domains", else personal.
"""
import json
import os
import shutil
import sys

import route

# Mail services anyone can sign up to: their domain says nothing of an employer, so --init never offers them as work.
PUBLIC_MAIL = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "icloud.com", "me.com", "mac.com",
    "yahoo.com", "proton.me", "protonmail.com", "pm.me", "aol.com", "gmx.com", "gmx.net", "gmx.de", "mail.com",
    "yandex.com", "yandex.ru", "zoho.com", "fastmail.com", "tutanota.com", "ukr.net", "i.ua", "meta.ua",
}
# The first label of a mail service's domains in every country (yahoo.co.uk, hotmail.fr, gmx.at, ...).
PUBLIC_MAIL_NAMES = {
    "gmail", "googlemail", "outlook", "hotmail", "live", "msn", "icloud", "me", "mac", "yahoo", "ymail", "aol", "proton",
    "protonmail", "pm", "gmx", "web", "mail", "email", "yandex", "zoho", "fastmail", "tutanota", "tuta", "ukr", "i", "meta",
    "t-online", "orange", "free", "laposte", "libero", "virgilio", "seznam", "wp", "o2", "interia", "onet", "rambler",
    "qq", "163", "126", "naver", "daum", "rediffmail", "btinternet", "sky", "comcast", "verizon", "att", "sbcglobal",
}


def is_public_mail(domain):
    d = str(domain or "").lower()
    return d in PUBLIC_MAIL or d.split(".")[0] in PUBLIC_MAIL_NAMES


KINDS = {"p": "personal", "personal": "personal", "w": "work", "work": "work", "b": "both", "both": "both"}


def git_root(folder):
    """The top of the git repository holding folder, or None (by its .git, without running git)."""
    here = os.path.realpath(folder)
    while True:
        if os.path.exists(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def context_here(word, cwd=None, out=print):
    """Write the marker in cwd. Inside a git repository it says how to keep the marker out of the repo's history."""
    ctx = str(word or "").strip().lower()
    if ctx not in route.CONTEXTS:
        out(f"--context-here takes work or personal, not {word!r}")
        return 2
    folder = os.path.realpath(cwd or os.getcwd())
    path = os.path.join(folder, route.CONTEXT_MARKER)
    with open(path, "w", encoding="utf-8") as f:
        f.write(ctx + "\n")
    out(f"{folder} and every folder under it: {ctx} ({path})")
    repo = git_root(folder)
    if repo:
        out(f"This folder is inside the git repository {repo}: keep the marker out of its commits. Put it in the folder "
            f"above the repository instead, or add {route.CONTEXT_MARKER} to your global gitignore "
            f"(git config --global core.excludesfile).")
    return 0


def _accounts(fake=None):
    """[(provider, account id, email domain or None)] of the providers present here."""
    rows = []
    for prov in ("agy", "codex", "claude-subscription"):
        if not route.provider_present(prov, fake=fake)["present"]:
            continue
        for acc in route.provider_accounts(prov, fake=fake):
            email = acc.get("email") or ""
            rows.append((prov, acc["id"], email.rpartition("@")[2].lower() if "@" in email else None))
    return rows


def propose(machine, kind, work_domains=()):
    """machine.json as it would be after --init: existing keys kept; context_paths gets "~"; work_domains grows."""
    new = json.loads(json.dumps(machine or {}))
    paths = dict(new.get("context_paths") or {})
    if "~" not in paths:
        paths["~"] = "work" if kind == "work" else "personal"
    new["context_paths"] = paths
    domains = [d for d in (new.get("work_domains") or [])]
    for d in work_domains:
        d = str(d).strip().lower().lstrip("@")
        if d and d not in domains:
            domains.append(d)
    if domains:
        new["work_domains"] = domains
    return new


def init(kind=None, work_domains=(), yes=False, path=None, fake=None, out=print, ask=None):
    """Look, propose, write on a yes. Without a terminal and without --machine it only shows what it would do."""
    path = path or route.MACHINE_PATH
    interactive = ask is not None or (sys.stdin.isatty() and sys.stdout.isatty())
    ask = ask or input
    machine = route.load_machine(path=path) if os.path.exists(path) else {}
    if machine.get("_error"):
        out(f"{machine['_error']}: fix or move it first; --init does not overwrite a file it cannot read")
        return 2
    accounts = _accounts(fake=fake)
    out("Accounts on this machine:" if accounts else "No cloud account found on this machine (agy, Codex).")
    for prov, acc, domain in accounts:
        label = route.account_info(acc, machine, email=("x@" + domain) if domain else "")["label"]
        out(f"  {acc}" + (f"  @{domain}" if domain else "") + f"  [{label}]")
    if kind is None:
        if not interactive:
            out("\nRun it in a terminal, or say what the machine is for: --machine personal|work|both "
                "[--work-domain DOMAIN ...] [--yes]. Nothing was written.")
            return 0
        while kind is None:
            kind = KINDS.get(ask("\nThis machine is for [p]ersonal tasks, [w]ork tasks or [b]oth? ").strip().lower())
    domains = list(work_domains)
    if kind in ("both", "work") and interactive and not work_domains:
        for domain in sorted({d for _, _, d in accounts if d and not is_public_mail(d)}):
            if ask(f"Is @{domain} your employer's (its accounts are work accounts)? [y/N] ").strip().lower() in ("y", "yes"):
                domains.append(domain)
    new = propose(machine, kind, domains)
    if new == machine:
        out(f"\n{path} already says this: nothing to change.")
        return roles_step(yes=yes, fake=fake, out=out, ask=ask if interactive else None)
    out(f"\n{path} would be:\n" + json.dumps(new, indent=2, ensure_ascii=False))
    if kind == "both":
        out(f"\nYour home folder is personal; in each folder of work code run: ask.py --context-here work "
            f"(or put a {route.CONTEXT_MARKER} with the word work above those folders).")
    elif kind == "work":
        out("\nEvery task here is work: personal accounts are refused unless marked \"work_ok\": true in \"accounts\" "
            "(a conscious choice; your employer's rules decide).")
    if not yes:
        if not interactive:
            out("Add --yes to write it. Nothing was written.")
            return 0
        if ask("Write it? [y/N] ").strip().lower() not in ("y", "yes"):
            out("Nothing was written.")
            return 0
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
        out(f"The old file is kept as {path}.bak.")
    route._atomic_write_json(path, new)
    out(f"Written. Next: ask.py --list (what this context allows).")
    return roles_step(yes=yes, fake=fake, out=out, ask=ask if interactive else None)


def roles_step(yes=False, fake=None, out=print, ask=None):
    """--init's last step: the roles from the models here (asked on a terminal, done with --yes)."""
    if ask is not None and not yes:
        if ask("\nSet the roles from the models on this machine? [Y/n] ").strip().lower() in ("n", "no"):
            out("Roles left as switchboard ships them; ask.py --roles does it later.")
            return 0
    elif not yes:
        return 0
    out("\nRoles, what each would call now and after:")
    return refresh_roles(yes=yes, fake=fake, out=out, ask=ask)


# --- Roles from the models this machine has (ask.py --roles, and the last step of --init) ---
# The shipped roles.json names today's families; another machine has other models. So a role's rows are proposed from
# the live inventory, ranked by the role's own benchmark metrics (score.metrics in roles.json, benchmarks.json) and the
# journal's experience, and written to the user's ~/.config/ask/roles.json, over the shipped file: an update of switchboard
# keeps them. A model benchmarks.json does not know comes after the known ones.

# How many models a role should reach here before it is filled: cloud ones (one for a role that spares the quota, cost
# 1.0 in its score) and local ones (for local_only and local-first roles).
MIN_CLOUD, MIN_CLOUD_THRIFTY, MIN_LOCAL = 2, 1, 2


def _role_kind(spec):
    if spec.get("local_only"):
        return "local"
    rows = route.role_candidates(route.role_rows(spec))[0]
    first = rows[0] if rows and isinstance(rows[0], dict) else {}
    return "local_first" if first.get("provider") and route.is_local(first["provider"]) else "cloud"


def _here(row, inv, absent):
    """The (provider, model) a role row reaches on this machine: its wish's matches or its id, if offered here."""
    got, _ = route.expand_wishes([row], inv, absent)
    offered = {(r["provider"], r["id"]) for r in inv}
    pairs = []
    for m in route.role_candidates(got)[0]:
        if isinstance(m, dict) and (m.get("provider"), m.get("model")) in offered:
            pairs.append((m["provider"], m["model"]))
    return pairs


def propose_roles(defaults, fake=None, journal=None, bench=None):
    """{role: role spec} for the roles that should change on this machine. A role keeps every row of the shipped file in
    its order (its author's choices: a cheap model for a draft; a row for a provider not here costs nothing and serves
    once it comes); only a role that reaches too few models here is filled from this machine's other models, ranked by
    the role's metrics. A role whose author gave it no cloud or local row (a subagent only) is never filled."""
    import experience
    import score
    inv, absent = route.live_inventory(fake=fake, rules=defaults)
    bench = experience.load_benchmarks() if bench is None else bench
    journal = route._read_journal_rows(fake=fake) if journal is None else journal
    families = {}  # (provider, family or id) -> {"ids": [...], "efforts": [...] or None (unknown)}
    for r in inv:
        if r["provider"] == "claude-subscription":
            continue
        fam = r["family"] if r["provider"] == "agy" else r["id"]
        entry = families.setdefault((r["provider"], fam), {"ids": [], "efforts": []})
        entry["ids"].append(r["id"])
        # agy carries its effort in the id: an id without one has none (not "unknown"), so no bound is wished for it.
        effs = ([r["effort"]] if r.get("effort") else []) if r["provider"] == "agy" else r.get("efforts")
        entry["efforts"] = None if effs is None or entry["efforts"] is None else entry["efforts"] + list(effs)
    out = {}
    for name, spec in (defaults.get("roles") or {}).items():
        if not isinstance(spec, dict):
            continue
        kind = _role_kind(spec)
        agentic = spec.get("exit_code") == 6 or bool(spec.get("no_engine")) or name in ("writer", "code-writer", "analysis")
        wants_high = agentic or name == "review"
        own = [r for r in route.role_rows(spec) if not (isinstance(r, dict) and r.get("provider") == "claude-subscription")]
        subscription = [r for r in route.role_rows(spec) if isinstance(r, dict) and r.get("provider") == "claude-subscription"]
        rows, covered, n_local, n_cloud = list(own), set(), 0, 0
        thrifty = float((spec.get("score") or {}).get("cost", 0)) >= 1.0
        want_local = MIN_LOCAL if kind in ("local", "local_first") else 0
        want_cloud = 0 if kind == "local" else (MIN_CLOUD_THRIFTY if thrifty else MIN_CLOUD)
        for row in own:
            for prov, mid in _here(row, inv, absent):
                covered.add((prov, experience.family(mid) if prov == "agy" else mid))
                local = route.is_local(prov, fake=fake)
                n_local, n_cloud = n_local + local, n_cloud + (not local)
        if own:  # an author who gave no such row meant none: no filling
            ranked = []
            for i, ((prov, fam), entry) in enumerate(families.items()):
                local = route.is_local(prov, fake=fake)
                if ((prov, fam) in covered or (kind == "local" and not local) or (kind == "cloud" and local)
                        or (agentic and local)):
                    continue
                mid = entry["ids"][-1]
                if route.is_never(prov, mid, rules=defaults)[0] or route.lacking(prov, mid, spec.get("needs") or [], defaults):
                    continue
                known = experience.family(fam) in (bench.get("models") or {}) or bool(score.variant(prov, fam, bench))
                q = score.quality(name, prov, fam, journal, defaults, bench)
                ranked.append((0 if (kind != "local_first" or local) else 1, not known, -q, i, prov, fam, entry, local))
            ranked.sort(key=lambda t: t[:4])
            for _, _, _, _, prov, fam, entry, local in ranked:
                if (local and n_local >= want_local) or (not local and n_cloud >= want_cloud):
                    continue
                row = {"provider": prov, "family": fam}
                if wants_high and not local and (entry["efforts"] is None
                                                 or route.effort_choice((">=", "high"), entry["efforts"])):
                    row["effort"] = ">=high"
                rows.append(row)
                n_local, n_cloud = n_local + local, n_cloud + (not local)
        new_rows = rows + subscription
        if new_rows != route.role_rows(spec):
            out[name] = dict({k: v for k, v in spec.items() if k not in ("prefer", "models", "names")}, prefer=new_rows)
    return out


def _matched(rules, role, fake=None):
    try:
        matched, _ = route.with_wishes(rules, role, fake=fake)
    except ValueError:
        return []
    return [mid for _, mid in (route.resolve_model(m, rules=matched)
                               for m in route._role_spec(role, matched)["models"]) if mid]


def refresh_roles(yes=False, fake=None, out=print, ask=None, path=None, defaults=None):
    """Propose the roles from this machine's models, show what each role would call now and after, write the changed
    ones into the user's roles.json on a yes (the old file kept as .bak)."""
    path = path or route.USER_ROLES_PATH
    interactive = ask is not None or (sys.stdin.isatty() and sys.stdout.isatty())
    ask = ask or input
    try:
        user = route.load_user_roles(path)
    except (OSError, ValueError) as e:
        out(f"{path}: {e}: fix or move it first; nothing was written")
        return 2
    defaults = defaults if defaults is not None else route.load_roles(path=route.ROLES_PATH)
    current = route.merge_roles(defaults, user)
    # From the roles as the user has them (the shipped roles with the user's over them): a role the user removed stays
    # removed, and a role's own settings (local_only, needs, ...) stay.
    proposed = propose_roles(current, fake=fake)
    changed = {}
    for name, spec in proposed.items():
        now = _matched(current, name, fake=fake)
        after = _matched(dict(current, roles=dict(current["roles"], **{name: spec})), name, fake=fake)
        if now == after:
            out(f"  {name}: {', '.join(now) or '(nothing here)'}")
            continue
        changed[name] = spec
        out(f"  {name}: {', '.join(now) or '(nothing here)'}\n    -> {', '.join(after) or '(nothing here)'}")
    if not changed:
        out("The roles already fit the models here: nothing to change.")
        return 0
    if not yes:
        if not interactive:
            out(f"Add --yes to write {len(changed)} role(s) into {path}. Nothing was written.")
            return 0
        if ask(f"Write {len(changed)} role(s) into {path}? [y/N] ").strip().lower() not in ("y", "yes"):
            out("Nothing was written.")
            return 0
    new = dict(user, roles=dict(user.get("roles") or {}, **changed))
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    route._atomic_write_json(path, new)
    out(f"Written: {path} (over the shipped roles.json; ask.py --roles again after new models arrive).")
    return 0
