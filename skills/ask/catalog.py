"""The model catalog: each provider's live list of models, kept fresh on its own, and what it means for the roles.

The router already reads the providers' live lists when it picks (agy models, Codex's model/list, Ollama's tags, a local
server's /models; cached a day by each). What it did not do is look at them as a whole: a role wish such as
"gemini-4-neon*" that matches no id agy lists was only said in the header of a call to that role, and a model a provider
started to offer was only seen by someone who ran --list and read the yellow rows. This module keeps one catalog of the
lists and compares it with the roles:

    missing  a role row names an id, or wishes for a family, that the provider's live list does not have (the router
             passes over it, so the role falls back to its next row)
    new      an id a provider lists now that it did not list at the last look, and the roles that take it (none: whether
             it joins a role is the user's call, as before)
    gone     an id a provider listed at the last look and lists no more

The catalog lives in ~/.cache/ask/model_catalog.json and is read again when older than CHECK_TTL_S, so the limits band
(once a minute) and --list keep it current without anyone asking; `ask.py --models` prints it, and `--models --refresh`
reads every list past its cache. Nothing here edits roles.json.

The Claude subscription has no list of its own: its ids are roles.json's subscription_models. When ANTHROPIC_API_KEY is
set, Anthropic's model list (GET /v1/models, cached a day) is read beside it, so that a subscription id Anthropic does
not list and a new Claude model are said too.

    python3 catalog.py [--refresh] [--json]
"""
import argparse
import json
import os
import sys
import time
import urllib.request

import route

CHECK_TTL_S = 3600  # how old the catalog may be before a check reads the lists again (each list keeps its own cache)
ANTHROPIC_TTL_S = 86400
ANTHROPIC_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC = "anthropic-api"  # the catalog's name for Anthropic's list (a reference, not a provider the router calls)


def _providers():
    import providers
    return providers


def catalog_path():
    # Read at call time: the tests move route.CACHE_DIR.
    return os.path.join(route.CACHE_DIR, "model_catalog.json")


def _anthropic_cache():
    return os.path.join(route.CACHE_DIR, "anthropic_models.json")


def fetch_anthropic_models(fake=None, force_refresh=False, ttl=ANTHROPIC_TTL_S):
    """Anthropic's model list: (ok, [{"id", "label"}]). Without ANTHROPIC_API_KEY it is not read: (False, [])."""
    fk = route.resolve_fake(fake)
    if fk is not None:
        items = fk.get("anthropic_models")
        if not items:
            return False, []
        return True, [{"id": str(x.get("id") if isinstance(x, dict) else x),
                       "label": str((x.get("display_name") if isinstance(x, dict) else None) or x)} for x in items]
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return False, []
    path = _anthropic_cache()
    if not force_refresh:
        try:
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - float(cached.get("fetched_at", 0)) <= ttl:
                return True, list(cached["models"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    out, after = [], None
    try:
        for _ in range(20):  # pages of 1000; a guard, not a limit anyone meets
            url = ANTHROPIC_URL + "?limit=1000" + (f"&after_id={after}" if after else "")
            req = urllib.request.Request(url, headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
            out += [{"id": m["id"], "label": m.get("display_name") or m["id"]} for m in data.get("data") or [] if m.get("id")]
            if not data.get("has_more") or not data.get("last_id"):
                break
            after = data["last_id"]
    except Exception:
        return False, []
    try:
        route._atomic_write_json(path, {"fetched_at": time.time(), "models": out})
    except OSError:
        pass
    return True, out


def read_lists(fake=None, force_refresh=False, rules=None):
    """{provider: {"present", "live", "note", "models": [{"id", "family", "effort" | "efforts"}]}} for every provider
    the router knows, plus Anthropic's list when it can be read. live: the list came from the provider itself (not from
    roles.json, which is what the router falls back to when a list cannot be read)."""
    rules = rules or route.load_roles()
    pv = _providers()
    out = {}
    for ad in pv.all_adapters(fake=fake):
        det = ad.detect()
        listed = out[ad.name] = {"present": bool(det["present"]), "live": False, "note": det.get("note"), "models": []}
        if not det["present"]:
            continue
        if ad.name == "claude-subscription":
            # No list to read: the ids are the ones roles.json gives the subscription, and the router uses exactly those.
            listed["live"], listed["note"] = True, "subscription_models (roles.json)"
            listed["models"] = [{"id": m, "family": m, "efforts": list(pv.CLAUDE_EFFORTS)}
                               for m in rules.get("subscription_models") or route.DEFAULT_SUBSCRIPTION_MODELS]
            continue
        if ad.name == "ollama":
            # The tags alone: the shared-weights look (/api/show per model) is --list's, not the catalog's.
            ok, items = route.fetch_ollama_models(ollama_url=ad.url(), fake=fake, include_show=False)
            listed["live"] = ok
            listed["models"] = [{"id": it["id"], "family": it["id"], "efforts": []} for it in items] if ok else []
            continue
        seen = set()
        for acc in ad.accounts():
            ok, rows = ad.models(acc if not acc.get("active") else None, force_refresh=force_refresh)
            if not ok:
                continue
            listed["live"] = True
            for r in rows:
                if r.get("ids"):  # agy: one id per effort
                    for eff, mid in r["ids"].items():
                        if mid not in seen:
                            seen.add(mid)
                            listed["models"].append(dict({"id": mid, "family": r["family"], "effort": eff or None},
                                                         **({"label": r["label"]} if r.get("label") not in (None, mid) else {})))
                elif r["id"] not in seen:
                    seen.add(r["id"])
                    listed["models"].append({"id": r["id"], "family": r.get("family") or r["id"],
                                            "efforts": list(r.get("efforts") or []) or None})
        if not listed["live"]:
            listed["note"] = route.agy_unavailable() if ad.name == "agy" else None
            listed["note"] = listed["note"] or "live list not read"
    ok, items = fetch_anthropic_models(fake=fake, force_refresh=force_refresh)
    out[ANTHROPIC] = {"present": ok, "live": ok, "note": None if ok else "ANTHROPIC_API_KEY not set or the list not read",
                      "models": [{"id": it["id"], "family": it["id"]} for it in items]}
    return out


def _load(path=None):
    try:
        with open(path or catalog_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def merge(previous, lists, now):
    """The new catalog from the last one and the lists just read. A provider read for the first time is the baseline:
    its ids are not new. A list that was not read keeps what the last look saw, so a provider that is down for an hour
    does not make all its ids gone and then new again."""
    prev = (previous or {}).get("providers") or {}
    providers = {}
    for name, listed in lists.items():
        old = prev.get(name) or {}
        first_seen = dict(old.get("first_seen") or {})
        gone = dict(old.get("gone") or {})
        since = old.get("since")
        if listed["live"]:
            ids = [m["id"] for m in listed["models"]]
            if since is None:
                since = now
            for mid in ids:
                first_seen.setdefault(mid, now)
                gone.pop(mid, None)
            for mid in list(first_seen):
                if mid not in ids:
                    gone.setdefault(mid, now)
                    first_seen.pop(mid)
            models, read_at = listed["models"], now
        else:
            models, read_at = old.get("models") or [], old.get("read_at")
        providers[name] = {"present": listed["present"], "live": listed["live"], "note": listed.get("note"),
                           "models": models, "read_at": read_at, "since": since,
                           "first_seen": first_seen, "gone": gone}
    return {"checked_at": now, "seen_at": (previous or {}).get("seen_at"),
            "seen_missing": (previous or {}).get("seen_missing") or [], "providers": providers}


def _row_list(rules, role):
    return route.role_candidates(route.role_rows((rules.get("roles") or {}).get(role)))[0]


def audit(catalog, rules=None):
    """{"missing": [...], "new": [...], "gone": [...]} for this catalog and these roles (see the module's docstring)."""
    rules = rules or route.load_roles()
    provs = catalog.get("providers") or {}
    live = {p for p, e in provs.items() if e.get("live") and p != ANTHROPIC}
    inventory = []
    for p in live:
        inventory += [dict(m, provider=p) for m in provs[p].get("models") or []]
    ids = {p: {m["id"].lower() for m in provs[p].get("models") or []} for p in provs}
    missing, said = [], set()

    def add(item):
        key = (item["role"], item["what"], item.get("provider"))
        if key not in said:
            said.add(key)
            missing.append(item)

    for role in rules.get("roles") or {}:
        for row in _row_list(rules, role):
            if not isinstance(row, dict):
                continue
            if route.is_wish(row):
                prov = row.get("provider")
                if prov and prov not in live:
                    continue  # not on this machine, or its list not read: nothing to compare with
                read = [prov] if prov else sorted(p for p in live if p != "claude-subscription")
                if not read:
                    continue
                try:
                    got, _ = route.match_wish(row, inventory, {})
                except ValueError:
                    got = []
                if got:
                    continue
                loose = route.match_wish(dict(row, effort=None), inventory, {})[0] if row.get("effort") else []
                not_read = [] if prov else sorted(p for p, e in provs.items()
                                                  if e.get("present") and not e.get("live") and p != ANTHROPIC)
                at = f" at effort {row['effort']}" if row.get("effort") else ""
                add({"role": role, "kind": "wish", "what": row["family"] + at, "provider": prov,
                     "why": (f"listed only at other efforts ({', '.join(sorted({g['model'] for g in loose}))})" if loose else
                             f"no id in the live list{'s' if len(read) > 1 else ''} of {', '.join(read)} matches")
                            + (f" (not read: {', '.join(not_read)})" if not_read else "")})
            elif row.get("model"):
                prov = row.get("provider") or route.resolve_model(row, rules=rules)[0]
                if prov not in live or row["model"].lower() in ids.get(prov, set()):
                    continue
                why = ("not in subscription_models (roles.json)" if prov == "claude-subscription"
                       else f"{prov}'s live list does not have it")
                add({"role": role, "kind": "id", "what": row["model"], "provider": prov, "why": why})
    # Subscription ids against Anthropic's own list, when it was read.
    if provs.get(ANTHROPIC, {}).get("live") and "claude-subscription" in live:
        for m in provs["claude-subscription"].get("models") or []:
            if m["id"].lower() not in ids[ANTHROPIC]:
                add({"role": "subscription_models", "kind": "id", "what": m["id"], "provider": "claude-subscription",
                     "why": "Anthropic's model list does not have it"})

    used = route.role_usage(rules)
    new, gone = [], []
    for p, e in provs.items():
        since = e.get("since")
        for mid, at in (e.get("first_seen") or {}).items():
            if since is not None and at > since:
                if p == ANTHROPIC:
                    sub = ids.get("claude-subscription", set())
                    roles = ["subscription_models"] if mid.lower() in sub else []
                else:
                    roles = list(used.get((p, mid), []))
                    roles += [r for r in route.wish_roles(p, mid, rules) if r not in roles]
                new.append({"provider": p, "id": mid, "first_seen": at, "roles": roles})
        for mid, at in (e.get("gone") or {}).items():
            gone.append({"provider": p, "id": mid, "gone_at": at})
    new.sort(key=lambda r: (-r["first_seen"], r["provider"], r["id"]))
    gone.sort(key=lambda r: (-r["gone_at"], r["provider"], r["id"]))
    return {"missing": missing, "new": new, "gone": gone}


def check(fake=None, force_refresh=False, max_age=CHECK_TTL_S, rules=None, now=None, write=True):
    """The catalog, read again when older than max_age (or force_refresh), with its audit against the roles as they are now:
    {"checked_at", "seen_at", "providers", "missing", "new", "gone", "unseen", "unseen_missing"}. unseen and unseen_missing:
    what the user has not looked at yet (ask.py --models), which the band says."""
    rules = rules or route.load_roles()
    now = time.time() if now is None else now
    cat = _load()
    if force_refresh or not cat or now - float(cat.get("checked_at") or 0) > max_age:
        cat = merge(cat, read_lists(fake=fake, force_refresh=force_refresh, rules=rules), now)
        if write:
            try:
                os.makedirs(route.CACHE_DIR, exist_ok=True)
                route._atomic_write_json(catalog_path(), cat)
            except OSError:
                pass
    report = dict(cat, **audit(cat, rules))
    seen = float(cat.get("seen_at") or 0)
    report["unseen"] = [n for n in report["new"] if n["first_seen"] > seen]
    known = set(cat.get("seen_missing") or [])
    report["unseen_missing"] = [m for m in report["missing"] if _missing_key(m) not in known]
    return report


def _missing_key(m):
    return f"{m['role']}|{m.get('provider') or ''}|{m['what']}"


def mark_seen(report, now=None):
    """The user has looked: the band stops saying what this report holds (a role row still missing stays in --models and
    --list, and a row that goes missing later is said again)."""
    cat = _load()
    if not cat:
        return
    cat["seen_at"] = time.time() if now is None else now
    cat["seen_missing"] = sorted(_missing_key(m) for m in report.get("missing") or [])
    try:
        route._atomic_write_json(catalog_path(), cat)
    except OSError:
        pass


def grouped(missing):
    """The missing rows by what is missing: [(what, provider, why, [roles])], in the order first said."""
    out = {}
    for m in missing:
        key = (m["what"], m.get("provider"), m["why"])
        out.setdefault(key, []).append(m["role"])
    return [(w, p, why, roles) for (w, p, why), roles in out.items()]


def _more(items, show):
    return show + (f" (+{len(items) - 1})" if len(items) > 1 else "")


def band_notes(report):
    """At most two short lines for the limits band: a role row the live lists do not have and new ids, each until the user
    has looked (ask.py --models)."""
    import limits
    out = []
    groups = grouped(report.get("unseen_missing") or [])
    if groups:
        out.append(limits.text("models_missing", what=_more(groups, groups[0][0])))
    if report.get("unseen"):
        n = report["unseen"][0]
        out.append(limits.text("models_new", what=_more(report["unseen"], n["id"])))
    return out


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"


def print_report(report, color=False, out=None):
    out = out or sys.stdout
    say = lambda s="": print(s, file=out)  # noqa: E731
    say(route.bold(f"Model catalog, checked {_when(report.get('checked_at'))}", color))
    for p, e in sorted((report.get("providers") or {}).items()):
        if not e.get("present"):
            if p != ANTHROPIC:
                say(route.dim(f"  {p}: not on this machine", color))
            elif e.get("note"):
                say(route.dim(f"  {p}: {e['note']}", color))
            continue
        state = (f"{len(e.get('models') or [])} model(s)" + (f", {e['note']}" if e.get("note") and e.get("live") else "")
                 if e.get("live") else route.yellow(f"{e.get('note') or 'live list not read'}"
                                                    + (f"; last read {_when(e.get('read_at'))}" if e.get("read_at") else ""),
                                                    color))
        say(f"  {p}: {state}")
    say()
    if report.get("missing"):
        say(route.bold("Role rows the live lists do not have (the router passes over them):", color))
        for what, prov, why, roles in grouped(report["missing"]):
            where = f" ({prov})" if prov else ""
            say(route.red(f"  {what}{where}: {why}", color) + route.dim(f"  [{', '.join(roles)}]", color))
    else:
        say(route.green("Every role row matches a live list (or its provider's list was not read).", color))
    if report.get("new"):
        say()
        say(route.bold("New since the provider's first look:", color))
        unseen = {(n["provider"], n["id"]) for n in report.get("unseen") or []}
        for n in report["new"]:
            roles = ", ".join(n["roles"]) if n["roles"] else route.yellow("in no role: whether it joins one is your call", color)
            mark = " *" if (n["provider"], n["id"]) in unseen else ""
            say(f"  {n['id']} ({n['provider']}, {_when(n['first_seen'])}){mark}: {roles}")
        if unseen:
            say(route.dim("  (* not seen before this look)", color))
    if report.get("gone"):
        say()
        say(route.bold("No longer listed:", color))
        for g in report["gone"]:
            say(route.dim(f"  {g['id']} ({g['provider']}, since {_when(g['gone_at'])})", color))


def main(argv=None, fake=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--refresh", action="store_true", help="read every provider's live list again, past its cache")
    ap.add_argument("--json", action="store_true", help="print the catalog and its audit as JSON")
    a = ap.parse_args(argv)
    report = check(fake=route.resolve_fake(fake), force_refresh=a.refresh, max_age=0 if a.refresh else CHECK_TTL_S)
    if a.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_report(report, color=route.use_color(sys.stdout))
    mark_seen(report)
    return 1 if report["missing"] else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
