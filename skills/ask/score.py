"""Role score, using only supplied data.

Cold time prior: AA minutes * 60 * the role's aa_time_scale (first guess).
Successful role journal medians calibrate it via base * minutes / reference.
Model-role timeouts are censored: each contributes max(2 * seconds, prior or 0)
to the same median/count blend as successful model-role seconds.
Cost is weekly share / available quota * time left in week * max(1, pace).
Thus quota expiring soon is cheaper; expiry enters exactly once.
"""
import math
import statistics

import experience
import route


def config(role, rules):
    spec = rules.get('roles', {}).get(role, {})
    return spec.get('score', {}) if isinstance(spec, dict) else {}


def variant(provider, model, bench, effort=None):
    if effort:
        model = experience.family(model) + '-' + effort
    elif experience.family(model) == model:
        default = bench.get('default_effort', {}).get(provider)
        if default:
            model += '-' + default
    return bench.get('variants', {}).get(model, {})


def quality(role, provider, model, rows, rules, bench, effort=None):
    entry = variant(provider, model, bench, effort)
    family = bench.get('models', {}).get(experience.family(model), {})
    values = []
    for metric in config(role, rules).get('metrics', []):
        value = entry.get(metric, family.get(metric))
        top = bench.get('sources', {}).get(metric, {}).get('top')
        if value is not None and top and math.isfinite(float(value)) and float(top) > 0:
            values.append(max(0., min(1., float(value) / float(top))))
    prior = statistics.mean(values) if values else experience.prior(model, role, rules, bench)
    evidence = experience.evidence(role, model, provider, rows, rules)
    weight = rules.get('experience', {}).get('prior_weight', 2)
    return ((weight * prior + evidence['sum']) / (weight + evidence['n'])
            if weight + evidence['n'] else prior)


def _runs(role, rows):
    return [r for r in rows if r.get('role') == role and r.get('status') == 'ok'
            and 'type' not in r]


def _median(values):
    values = [float(v) for v in values if v is not None and float(v) > 0 and math.isfinite(float(v))]
    return statistics.median(values) if values else None


def _reference(role, metric, rules, bench):
    spec = rules.get('roles', {}).get(role, {})
    raw = spec if isinstance(spec, list) else spec.get('models', spec.get('names', []))
    return _median(variant(*route.resolve_model(m, rules=rules), bench).get(metric)
                   for m in route.role_candidates(raw)[0])


def _blend(prior, samples, rules):
    own = _median(samples)
    if own is None:
        return prior
    if prior is None:
        return own
    k = rules.get('score_defaults', {}).get('prior_runs', 2)
    return (k * prior + len(samples) * own) / (k + len(samples))


def expected_seconds(role, provider, model, rows, rules, bench, size_factor=1., effort=None):
    runs = _runs(role, rows)
    minutes = variant(provider, model, bench, effort).get('minutes_per_task')
    base = _median(r.get('seconds') for r in runs)
    ref = _reference(role, 'minutes_per_task', rules, bench)
    prior = None
    if minutes is not None:
        cold = minutes * 60 * config(role, rules).get('aa_time_scale', 1.)
        prior = base * minutes / ref if base and ref else cold
    samples = [r['seconds'] for r in runs if r.get('provider') == provider and r.get('model') == model
               and r.get('seconds', 0) > 0]
    samples.extend(max(2 * r['seconds'], prior or 0) for r in rows
                   if r.get('role') == role and r.get('provider') == provider
                   and r.get('model') == model and r.get('status') == 'timeout'
                   and 'type' not in r and r.get('seconds', 0) > 0)
    result = _blend(prior, samples, rules)
    return result * size_factor if result is not None else None


def expected_tokens(role, provider, model, rows, rules, bench, size_factor=1., effort=None):
    base = route.estimate_tokens(role, rules=rules, fake={'journal': rows})
    output = variant(provider, model, bench, effort).get('output_tokens_per_task')
    ref = _reference(role, 'output_tokens_per_task', rules, bench)
    prior = base * output / ref if output is not None and ref else base
    samples = [route.billed_tokens(r, provider, rules=rules)['billed'] for r in _runs(role, rows)
               if r.get('provider') == provider and r.get('model') == model]
    return _blend(prior, [s for s in samples if s > 0], rules) * size_factor


def cost_share(role, provider, model, rows, rules, bench, cs, size_factor=1., effort=None):
    if provider == 'ollama':
        return 0.
    if provider == 'claude-subscription':
        return None
    tokens = expected_tokens(role, provider, model, rows, rules, bench, size_factor, effort)
    costs = []
    for pool, window in route.covering_pools_and_windows(provider, model, cs.get('pools', [])):
        wtype = str(window.get('window') or '')
        if 'week' not in wtype.lower():  # the router's own test for a weekly window ("weekly", "week", "7d" labels)
            continue
        rate = route.pool_window_rate(pool.get('label'), wtype, rules=rules, fake={'journal': rows})
        available = max(1e-6, float(window.get('available_fraction', window.get('remaining_fraction', 1))))
        left = window.get('resets_in_s')
        fraction = min(1., max(0., left / (window.get('length_s') or 604800))) if left is not None else 1.
        costs.append(tokens / 1e6 * rate / available * fraction * max(1., window.get('pace') or 0))
    # Unknown pools still have a default rate; absence of telemetry is not free quota.
    return max(costs) if costs else tokens / 1e6 * route.pool_window_rate(
        provider, rules=rules, fake={'journal': rows})


def size_factor(role, prompt_tokens, rows, rules=None):
    median = _median((r.get('tokens') or {}).get('in') for r in _runs(role, rows))
    if prompt_tokens is None or prompt_tokens <= 0 or not median:
        return 1.
    lo, hi = (rules or {}).get('score_defaults', {}).get('size_clamp', [.5, 3.])
    return max(lo, min(hi, .5 + .5 * prompt_tokens / median))


def rank(role, evaluated, rules, rows, bench, cs, prompt_tokens=None, weights=None):
    cfg = dict(config(role, rules))
    cfg.update({k: v for k, v in (weights or {}).items() if k in ("time", "cost")})
    budget = cfg['time_budget_s']
    spec = rules['roles'][role]
    raw = spec if isinstance(spec, list) else spec.get('models', spec.get('names', []))
    owners = [route.resolve_model(m, rules=rules) for m in route.role_candidates(raw)[0]]
    defaults = rules.get('score_defaults', {})
    factor = size_factor(role, prompt_tokens, rows, rules)
    candidates = []
    for e in evaluated:
        if not e.get('ok') or e['provider'] == 'claude-subscription':
            continue
        p, m = e['provider'], e['model']
        # The effort picks the benchmark variant only; the journal knows the model by its plain id.
        effort = e.get('effort')
        q = quality(role, p, m, rows, rules, bench, effort)
        t = expected_seconds(role, p, m, rows, rules, bench, factor, effort)
        c = cost_share(role, p, m, rows, rules, bench, e.get('_cs') or cs, factor, effort)  # another account's own pool
        index = owners.index((p, m)) if (p, m) in owners else len(owners)
        bonus = min(defaults.get('owner_cap', .10), max(0, len(owners) - index - 1) * defaults.get('owner_step', .03))
        candidates.append(dict(e, score={'Q': q, 'seconds': t, 'budget': budget, 'C': c, 'bonus': bonus}, _owner=index))
    # Filter before cost normalization: a disallowed slow model must not make others look cheap.
    maximum = max((e['score']['C'] for e in candidates
                   if e['score']['seconds'] is None or e['score']['seconds'] <= budget), default=0)
    filtered, survivors = [], []
    for e in candidates:
        s = e['score']
        s['S'] = s['Q'] - cfg.get('time', 0) * (s['seconds'] or 0) / budget - cfg.get('cost', 0) * (s['C'] / maximum if maximum else 0) + s['bonus']
        if s['seconds'] is not None and s['seconds'] > budget:
            e.update(ok=False, reason=f"{e['model']}: would time out (~{s['seconds']/60:.0f} min > {budget/60:g} min budget)")
            filtered.append(e)
        else:
            survivors.append(e)
    survivors.sort(key=lambda e: (-e['score']['S'], e['_owner']))
    notes = []
    for e in survivors + filtered:
        s = e['score']
        t = '?' if s['seconds'] is None else f"~{s['seconds']/60:.1f}"
        notes.append(f"{e['model']} {s['S']:.2f} (Q {s['Q']:.2f}, T {t}/{budget/60:g} min, C {s['C']*100:.2f}% wk)" + (f"; {e['reason']}" if not e['ok'] else ''))
    return survivors, filtered, notes
