"""Optional machine hooks of /ask: what one machine knows beyond the plain GPU and VRAM check.

A machine with a build engine, games or a quiet-start script names a Python file in ~/.config/ask/machine.json:

    "hooks": {"module": "~/path/to/my_hooks.py", ...settings the module reads}

Without that section the router uses its plain checks only: free VRAM against what the model needs, the GPU lock. A hook module may define any of these; each gets the "hooks" section as `settings`:

    local_blockers(state, settings) -> [reason] may a local model load now? state: model, processes,
                                                     gpu_processes, fake (a test's local state), rules
    claim_blockers(model, settings) -> [reason] the last look just after the GPU lock is taken
    build_blockers(settings) -> [reason] what blocks a build now, besides a local model's lock
    writer_rules(engine, settings) -> {"engine": text, "protected": text} a writer's standing rules about it
    engine_markers(settings) -> [text] words of a command that starts the engine (the broker
                                                     refuses such a command to a writer without --engine)

A module that fails to load, or a blocker hook that raises, blocks the local model or the build (fail closed): the hooks
are there to keep a model off a GPU the machine needs. engine_markers failing makes the broker refuse every command but
its exact one. writer_rules falls back to the defaults with a warning.

    python3 hooks.py build-blockers exit 0 when nothing blocks a build, 1 with the reasons on stdout
"""
import importlib.util
import os
import sys

_loaded = {}


def section(machine):
    conf = (machine or {}).get("hooks")
    return conf if isinstance(conf, dict) and conf.get("module") else None


def load(machine):
    """(module or None, settings, error or None) for the machine's "hooks" section; the module is loaded once per path
    and modification time."""
    conf = section(machine)
    if conf is None:
        return None, {}, None
    path = os.path.expanduser(str(conf["module"]))
    try:
        key = (path, os.path.getmtime(path))
        if key not in _loaded:
            spec = importlib.util.spec_from_file_location("ask_machine_hooks", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _loaded[key] = module
        return _loaded[key], conf, None
    except Exception as e:  # a broken file is said, never silently skipped
        return None, conf, f"hooks module {path} failed to load: {e}"


def blockers(name, *args, machine=None):
    """The reasons a blocker hook gives ([] without a hook); a load error or a raising hook is itself a reason."""
    module, conf, error = load(machine)
    if error:
        return [error]
    fn = getattr(module, name, None) if module else None
    if fn is None:
        return []
    try:
        return [str(r) for r in (fn(*args, settings=conf) or [])]
    except Exception as e:
        return [f"hooks {name} failed: {e}"]


def value(name, *args, machine=None, default=None):
    """What an advisory hook returns, else the default; a failure warns on stderr and keeps the default."""
    module, conf, error = load(machine)
    fn = getattr(module, name, None) if module else None
    if error or fn is None:
        if error:
            print(f"[{error}]", file=sys.stderr)
        return default
    try:
        out = fn(*args, settings=conf)
        return default if out is None else out
    except Exception as e:
        print(f"[hooks {name} failed: {e}; the default is used]", file=sys.stderr)
        return default


def engine_markers(machine):
    """The words of an engine command, lowered; [] without hooks; None when the module or the hook failed (the broker then
    refuses every command but its exact one, fail closed)."""
    module, conf, error = load(machine)
    fn = getattr(module, "engine_markers", None) if module else None
    if error:
        print(f"[{error}]", file=sys.stderr)
        return None
    if fn is None:
        return []
    try:
        return [str(m).lower() for m in fn(settings=conf) or []]
    except Exception as e:
        print(f"[hooks engine_markers failed: {e}]", file=sys.stderr)
        return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ["build-blockers"]:
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import route
    reasons = route.build_blockers()
    for r in reasons:
        print(r)
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
