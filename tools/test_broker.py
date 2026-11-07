"""Offline protocol and writer integration tests; no real CLIs, accounts, network or engine."""
import contextlib
import io
import json
import os

# The tests read a frozen roles file, not the live roles.json.
os.environ["ASK_ROLES"] = os.path.join(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "ask"), "test_roles.json")
os.environ["ASK_LANG"] = "uk"  # the tests read the Ukrainian words; test_limits checks the English ones too
import tempfile as _tempfile
# A machine's own machine.json, roles.json and journal never decide a test.
os.environ["ASK_CONFIG_DIR"] = _tempfile.mkdtemp(prefix="ask-test-config-")
os.environ["ASK_CACHE_DIR"] = _tempfile.mkdtemp(prefix="ask-test-cache-")
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import broker
import writer


ONCE = [{"optionId": "yes-once", "name": "Allow once", "kind": "allow_once"},
        {"optionId": "no-once", "name": "Reject once", "kind": "reject_once"},
        {"optionId": "forever", "name": "Always", "kind": "allow_always"}]


def acp(command=None, path=None, **extra):
    tool = ({"name": "run_command", "kind": "execute", "rawInput": {"CommandLine": command}}
            if command else {"name": "write_file", "kind": "edit", "locations": [{"path": path}]})
    tool.update(extra)
    return {"method": "session/request_permission", "params": {"toolCall": tool, "options": ONCE}}


def codex(command=None, changes=None, decisions=None):
    if command is not None:
        return {"method": "item/commandExecution/requestApproval", "params": {
            "command": command, "availableDecisions": decisions or ["accept", "decline", "cancel"]}}
    return {"method": "item/fileChange/requestApproval", "params": {"itemId": "patch"},
            "item": {"id": "patch", "type": "fileChange", "changes": changes}}


def wait_for(predicate, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError("timed out waiting for fixture")


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(ROOT))
        self.addCleanup(self.tmp.cleanup)
        self.cwd = self.tmp.name
        self.base = os.path.join(self.cwd, "run")
        self.events, self.notes = [], []
        self.real_popen = subprocess.Popen
        real_run = subprocess.run

        def stops_a_fake(command):
            # stop_tree on Windows: taskkill of a fake server's tree
            return os.name == "nt" and list(command[:4]) == ["taskkill", "/T", "/F", "/PID"] and len(command) == 5

        def only_fakes(command, *args, **kwargs):
            if stops_a_fake(command):
                return self.real_popen(command, *args, **kwargs)
            if command[0] != sys.executable or not any(str(p).endswith(("fake_agents.py", "writer.py")) for p in command[:4]):
                raise AssertionError("only local fake servers and --answer may run: " + repr(command))
            return self.real_popen(command, *args, **kwargs)

        def only_stops(command, *args, **kwargs):
            if stops_a_fake(command):
                return real_run(command, *args, **kwargs)
            raise AssertionError("no external commands")

        for target, value in (("subprocess.Popen", only_fakes),
                              ("subprocess.run", mock.Mock(side_effect=only_stops)),
                              ("urllib.request.urlopen", mock.Mock(side_effect=AssertionError("no network")))):
            patcher = mock.patch(target, new=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(sys, "stderr", io.StringIO()))
        self.stack.enter_context(mock.patch.object(sys, "stdout", io.StringIO()))

    def driver(self, provider, script=None, **kwargs):
        cls = broker.AcpDriver if provider == "agy" else broker.CodexAppDriver
        command = [sys.executable, "-B", str(HERE / "fake_agents.py"), "acp" if provider == "agy" else "codex",
                   "--script", json.dumps(script or {})]
        driver = cls(self.cwd, "model", self.base, self.notes.append,
                     lambda direction, msg: self.events.append({"dir": direction, "msg": msg}),
                     writer.stop_tree, command=command, home=str(ROOT), **kwargs)
        self.addCleanup(driver.close)
        return driver

    def run_driver(self, provider, script=None, conversation=None, **kwargs):
        driver = self.driver(provider, script, **kwargs)
        try:
            result = driver.run("test prompt", conversation)
        finally:
            driver.close()
        return driver, result

    def test_acp_reasoning_sink_and_defensive_content(self):
        pieces = []
        steps = [{"notification": "session/update", "params": {"update": {
            "sessionUpdate": "agent_thought_chunk", "content": content}}}
            for content in (None, "bad", {"text": "First "}, {"text": "thought"})]
        driver, result = self.run_driver("agy", {"turns": [{"steps": steps, "answer": "Answer"}]}, reasoning_sink=pieces.append)
        self.assertEqual(result["response"], "Answer")
        self.assertEqual(result["reasoning"], "First thought")
        self.assertEqual(result["reasoning_kind"], "summary")
        self.assertEqual(pieces, ["First ", "thought"])
        self.assertNotIn("First", "".join(self.notes))

    def test_codex_reasoning_parts_fallback_and_summary_option(self):
        def note(method, **params):
            return {"notification": method, "params": params}
        steps = [note("item/reasoning/summaryTextDelta", itemId="one", summaryIndex=0, delta="First"),
                 note("item/reasoning/summaryPartAdded", itemId="one", summaryIndex=1),
                 note("item/reasoning/summaryTextDelta", itemId="one", summaryIndex=1, delta="Second"),
                 note("item/completed", item={"id": "one", "type": "reasoning", "summary": ["First", "Second"]}),
                 note("item/completed", item={"id": "empty", "type": "reasoning", "summary": []}),
                 note("item/completed", item={"id": "two", "type": "reasoning", "summary": ["Fallback", "Part"]}),
                 note("item/completed", item={"id": "two", "type": "reasoning", "summary": ["Fallback", "Part"]})]
        for summary in (None, "detailed"):
            pieces = []
            driver, result = self.run_driver("codex", {"echoTurnParams": True, "turns": [{"steps": steps}]},
                                             reasoning_summary=summary, reasoning_sink=pieces.append)
            self.assertEqual(result["reasoning"], "First\n\nSecond\n\nFallback\n\nPart")
            self.assertEqual("".join(pieces), result["reasoning"])
            self.assertEqual(result["reasoning_kind"], "summary")
            params = [e["msg"]["params"] for e in self.events if e["msg"].get("method") == "fake/turnParams"][-1]
            self.assertEqual(params.get("summary"), summary)
            self.assertEqual("summary" in params, summary is not None)

    def test_codex_full_reasoning_wins(self):
        steps = [{"notification": "item/reasoning/textDelta", "params": {"itemId": "raw", "delta": "Raw"}},
                 {"notification": "item/completed", "params": {"item": {
                     "id": "raw", "type": "reasoning", "content": ["Raw"]}}},
                 {"notification": "item/completed", "params": {"item": {
                     "id": "fallback", "type": "reasoning", "content": ["Full"]}}},
                 {"notification": "item/reasoning/summaryTextDelta", "params": {"itemId": "sum", "delta": "Summary"}}]
        _, result = self.run_driver("codex", {"turns": [{"steps": steps}]})
        self.assertEqual(result["reasoning"], "Raw\n\nFull\n\nSummary")
        self.assertEqual(result["reasoning_kind"], "full")

    def test_reasoning_log_long_line_and_review_cases(self):
        # Review: a long line cut mid-email, a token pattern eating an email, a fake turn header.
        log = broker.ReasoningLog(self.cwd, "long", "agy", "model")
        log.write("word " * 799 + "person@exam")
        log.write("ple.invalid end\n")
        log.write("## Turn 7 is what the model wrote\n")
        log.write("bearer user123@secret.invalid and sk-1234567890123456@secret.invalid\n")
        log.close("summary")
        text = Path(log.path).read_text()
        self.assertNotIn("person@", text)
        self.assertNotIn("exam", text.replace("example", ""))
        self.assertNotIn("secret.invalid", text)
        self.assertEqual(broker.ReasoningLog(self.cwd, "long", "agy", "model").turn, 2)

    def test_reasoning_log_turns_redaction_and_sanitizing(self):
        log = broker.ReasoningLog(self.cwd, "../unsafe/id", "agy", "model")
        self.assertEqual(Path(log.path).parent, Path(self.cwd))
        self.assertEqual(Path(log.path).name, ".._unsafe_id.md")
        log.write("person@")
        log.write("example.invalid sk-" + "a" * 10)
        log.write("b" * 10 + "\n")
        live = Path(log.path).read_text()
        self.assertIn("<email> <redacted>", live)
        self.assertNotIn("person@", live)
        log.close("summary")
        log = broker.ReasoningLog(self.cwd, "../unsafe/id", "agy", "model")
        self.assertEqual(log.turn, 2)
        log.close(None)
        text = Path(log.path).read_text()
        self.assertEqual(text.count("# Reasoning:"), 1)
        self.assertIn("## Turn 2,", text)
        self.assertIn("(no reasoning from the provider this turn)", text)
        self.assertIn("_kind: none_", text)
        for token in ("ghp_" + "a" * 25, "gho_" + "a" * 25, "github_pat_" + "a" * 25,
                      "AIza" + "a" * 35, "xoxb-123-abcdef", "Bearer abc123"):
            self.assertEqual(broker.redact(token), "<redacted>")

    def replies(self):
        return [e["msg"]["result"] for e in self.events if e["dir"] == "out" and "result" in e["msg"]]

    def pending(self):
        try:
            with open(self.base + ".pending.json") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def answer_cli(self, option, text=None, base=None):
        args = [sys.executable, "-B", str(HERE / "writer.py"), "--answer", base or self.base, option]
        if text is not None:
            args.append(text)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        out, err = process.communicate(timeout=5)
        return process.returncode, out, err

    def in_thread(self, function):
        values, errors = [], []

        def run():
            try:
                values.append(function())
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(thread.join, 5)
        return thread, values, errors

    def test_read_only_policy_and_never(self):
        policy = broker.Policy(self.cwd, read_only=True)
        self.assertEqual(policy.decide("agy", "file", "", ["a.txt"], write=False)[0], "allow")
        self.assertEqual(policy.decide("agy", "file", "", ["a.txt"], write=True)[0], "human")
        self.assertEqual(policy.decide("agy", "command", "", command=policy.command)[0], "human")
        human = broker.HumanRequests(self.base, "agy", policy, self.notes.append, ask="never")
        self.assertEqual(human.choose(1, "file", "", [], "yes", "no", ["a.txt"], write=True)[0], "no")
        self.assertEqual(policy.decide("agy", "file", "", ["~/.ssh/key"], write=True)[0], "deny")

    def test_codex_options_and_environment(self):
        self.run_driver("codex", {"echoEnv": "BROKER_TEST_VALUE"}, sandbox="read-only", effort=None,
                        env=dict(os.environ, BROKER_TEST_VALUE="fixture-value"))
        start = next(e["msg"]["params"] for e in self.events if e["msg"].get("method") == "thread/start")
        turn = next(e["msg"]["params"] for e in self.events if e["msg"].get("method") == "turn/start")
        self.assertEqual(start["sandbox"], "read-only")
        self.assertEqual(start["approvalPolicy"], "on-request")
        self.assertNotIn("effort", turn)
        self.assertTrue(any(e["msg"].get("params", {}).get("value") == "fixture-value" for e in self.events))

    def test_deadline_polite_cancel_for_both_providers(self):
        for provider in ("agy", "codex"):
            with self.subTest(provider=provider):
                self.events.clear()
                start = time.monotonic()
                driver, result = self.run_driver(provider, {"honourCancel": True,
                    "turns": [{"steps": [{"hang": True}]}]}, deadline=.5, deadline_grace=.5)
                self.assertLess(time.monotonic() - start, 2)
                self.assertIsNotNone(driver.timed_out)
                self.assertEqual(result["deadline"], driver.timed_out)
                self.assertFalse(result["failed"])
                method = "session/cancel" if provider == "agy" else "turn/interrupt"
                message = next(e["msg"] for e in self.events if e["dir"] == "out" and e["msg"].get("method") == method)
                self.assertEqual(message["params"], {"sessionId": "fake-session"} if provider == "agy" else
                                 {"threadId": "fake-thread", "turnId": "fake-turn"})
                if provider == "agy":
                    self.assertNotIn("id", message)
                    self.assertTrue(any(e["msg"].get("result", {}).get("stopReason") == "cancelled" for e in self.events))
                else:
                    self.assertTrue(any(e["msg"].get("params", {}).get("turn", {}).get("status") == "interrupted"
                                        for e in self.events))

    def test_deadline_ignored_cancel_kills_after_grace(self):
        for provider in ("agy", "codex"):
            with self.subTest(provider=provider):
                driver = self.driver(provider, {"turns": [{"steps": [{"hang": True}]}]},
                                     deadline=.5, deadline_grace=.5)
                start = time.monotonic()
                result = driver.run("prompt")
                driver.transport.process.wait(timeout=1)
                self.assertGreaterEqual(time.monotonic() - start, 1)
                self.assertLess(time.monotonic() - start, 2.5)
                self.assertIsNotNone(driver.transport.process.poll())
                self.assertIsNotNone(result["deadline"])
                self.assertFalse(result["failed"])
                driver.close()

    def test_deadline_excludes_live_human_wait_for_both_providers(self):
        for provider in ("agy", "codex"):
            with self.subTest(provider=provider):
                self.events.clear()
                step = acp(command="inspect") if provider == "agy" else codex(command="inspect")
                driver = self.driver(provider, {"turns": [{"steps": [step]}]}, deadline=.8)
                thread, values, errors = self.in_thread(lambda: driver.run("prompt"))
                wait_for(self.pending)
                time.sleep(1.6)
                self.assertIsNone(driver.timed_out)
                self.assertGreater(driver.human.waited_seconds(), 1.5)
                option = "yes-once" if provider == "agy" else "accept"
                self.assertEqual(self.answer_cli(option)[0], 0)
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertIsNone(values[0]["deadline"])
                self.assertEqual(self.replies(), [{"outcome": {"outcome": "selected", "optionId": option}}]
                                 if provider == "agy" else [{"decision": option}])
                driver.close()

    def test_deadline_zero_and_negative(self):
        for provider in ("agy", "codex"):
            driver, result = self.run_driver(provider, {"turns": [{"steps": [{"sleep": .7}]}]}, deadline=0)
            self.assertIsNone(driver.watchdog)
            self.assertIsNone(result["deadline"])
        for legacy in (False, True):
            self.assertEqual(writer.parser(legacy).parse_args(["--cwd", self.cwd]).deadline, 3600)
            with self.assertRaises(SystemExit) as raised:
                writer.main(["--cwd", self.cwd, "--deadline", "-1"], legacy_agy=legacy)
            self.assertEqual(raised.exception.code, 2)

    def test_writer_broker_deadline_journal_and_reservation(self):
        import route
        for provider in ("agy", "codex"):
            with self.subTest(provider=provider), self.writer_patches(), \
                 mock.patch.object(route, "check_name", return_value={"ok": True, "provider": provider, "model": "model"}), \
                 mock.patch.object(route, "codex_path", return_value="fake-codex"), \
                 mock.patch.object(route, "release_reservation", wraps=route.release_reservation) as release:
                def fake_start(driver):
                    driver.command = [sys.executable, "-B", str(HERE / "fake_agents.py"),
                                      "acp" if provider == "agy" else "codex", "--script",
                                      json.dumps({"honourCancel": True, "turns": [{"steps": [{"hang": True}]}]})]
                    broker.Driver.start(driver)
                cls = broker.AcpDriver if provider == "agy" else broker.CodexAppDriver
                with mock.patch.object(cls, "start", fake_start):
                    code = writer.main(["--cwd", self.cwd, "--model", "model", "--" + provider,
                                        "--brief", "brief.md", "--log", self.base + ".json", "--deadline", ".5"])
                self.assertEqual(code, 9)
                release.assert_called_once()
                with open(route.JOURNAL_PATH) as handle:
                    self.assertEqual(json.loads(handle.readlines()[-1])["status"], "deadline")
                with open(self.base + ".progress.txt") as handle:
                    self.assertIn("ERROR: deadline", handle.read())
                self.assertIn("--continue", sys.stderr.getvalue())
                self.assertIn("[deadline:", sys.stdout.getvalue())

    def test_writer_legacy_deadline_journal_and_reservation(self):
        import route
        for legacy_agy in (False, True):
            with self.subTest(legacy_agy=legacy_agy), self.writer_patches(), \
                 mock.patch.object(route, "release_reservation", wraps=route.release_reservation) as release:
                ended = threading.Event()
                proc = mock.Mock(returncode=None)
                proc.poll.side_effect = lambda: proc.returncode
                def lines():
                    ended.wait(3)
                    return
                    yield
                proc.stdout = lines()
                def stop(process):
                    self.assertIs(process, proc)
                    proc.returncode = -9
                    ended.set()
                with mock.patch.object(subprocess, "Popen", return_value=proc), \
                     mock.patch.object(writer, "stop_tree", side_effect=stop) as kill:
                    args = ["--cwd", self.cwd, "--model", "model", "--continue", "old",
                            "--log", self.base + ".json", "--deadline", ".5"]
                    if not legacy_agy:
                        args += ["--agy", "--legacy-cli"]
                    start = time.monotonic()
                    code = writer.main(args, legacy_agy=legacy_agy)
                self.assertLess(time.monotonic() - start, 2)
                self.assertEqual(code, 9)
                kill.assert_called_once_with(proc)
                release.assert_called_once()
                with open(route.JOURNAL_PATH) as handle:
                    self.assertEqual(json.loads(handle.readlines()[-1])["status"], "deadline")
                with open(self.base + ".progress.txt") as handle:
                    self.assertIn("ERROR: deadline", handle.read())
                self.assertIn("--continue old", sys.stderr.getvalue())

    def test_policy_for_both_providers_and_canonical_paths(self):
        policy = broker.Policy(self.cwd, python="chosen-python", home=str(ROOT), engine_markers=("big-engine",))
        for provider in ("agy", "codex"):
            with self.subTest(provider=provider):
                self.assertEqual(policy.decide(provider, "file", "write", ["one.py"])[0], "allow")
                for target in ("git push origin", "git commit -am x", "taskkill /f", "kill 42", "pkill x",
                               "Big-Engine -run", "~/.codex/a", ".gemini/a", ".ssh/key", "security find", "rm -rf /"):
                    self.assertEqual(policy.decide(provider, "command", target, command=target)[0], "deny", target)
                self.assertEqual(policy.decide(provider, "file", "read", ["/outside/home"])[0], "deny")
                self.assertEqual(policy.decide(provider, "file", "read", [str(ROOT / "elsewhere")])[0], "human")
                self.assertEqual(policy.decide(provider, "question", "git push?")[0], "human")
        self.assertEqual(policy.decide("agy", "command", "", command="chosen-python -I agy_run.py")[0], "allow")
        self.assertEqual(policy.decide("codex", "command", "", command="chosen-python -I agy_run.py")[0], "human")
        self.assertEqual(policy.decide("agy", "command", "", command="chosen-python -I agy_run.py", cwd=str(ROOT))[0], "human")
        try:
            os.symlink("/outside/home", os.path.join(self.cwd, "escape"))
        except (OSError, NotImplementedError):  # Windows without the right to make links
            self.skipTest("this account may not create symbolic links")
        self.assertEqual(policy.decide("agy", "file", "", ["escape/file"])[0], "deny")

    def test_acp_rules_and_carry_on_sum_usage(self):
        steps = [acp(command="python -I agy_run.py"), acp(path="one.txt"), acp(command="git push origin")]
        _, result = self.run_driver("agy", {"turns": [{"steps": steps, "answer": ""}, {"answer": "Rest done"}]}, ask="never")
        self.assertEqual([r["outcome"]["optionId"] for r in self.replies()], ["yes-once", "yes-once", "no-once"])
        prompts = [e["msg"]["params"]["prompt"][0]["text"] for e in self.events
                   if e["msg"].get("method") == "session/prompt"]
        self.assertEqual(len(prompts), 2)
        self.assertIn("was refused", prompts[1])
        self.assertEqual(result["usage"], {"input_tokens": 200, "output_tokens": 40, "thinking_tokens": 6,
                                           "cache_read_tokens": 20, "total_tokens": 240})
        self.assertEqual(result["response"], "Rest done")
        self.assertNotIn("DO NOT ECHO", result["response"])

    def test_acp_limits_recovery_to_three_extra_turns(self):
        _, result = self.run_driver("agy", {"turns": [{"steps": [acp(command="git commit")]}]}, ask="never")
        self.assertEqual(len(self.replies()), 4)
        self.assertEqual(result["usage"]["total_tokens"], 480)

    def prompts(self, method):
        key = "prompt" if method == "session/prompt" else "input"
        return [e["msg"]["params"][key][0]["text"] for e in self.events if e["dir"] == "out" and e["msg"].get("method") == method]

    @staticmethod
    def acp_plan(*entries):
        return {"notification": "session/update", "params": {"update": {"sessionUpdate": "plan", "entries": [
            {"content": text, "priority": "medium", "status": status} for text, status in entries]}}}

    @staticmethod
    def codex_plan(*entries):
        return {"notification": "turn/plan/updated", "params": {"threadId": "fake-thread", "turnId": "fake-turn", "plan": [
            {"step": text, "status": status} for text, status in entries]}}

    def test_acp_open_plan_items_send_the_agent_back(self):
        turns = [{"steps": [self.acp_plan(("Read the brief", "completed"), ("Write notes.md", "pending"))], "answer": ""},
                 {"steps": [self.acp_plan(("Read the brief", "completed"), ("Write notes.md", "completed"))], "answer": "Written"}]
        driver, result = self.run_driver("agy", {"turns": turns})
        prompts = self.prompts("session/prompt")
        self.assertEqual(len(prompts), 2)
        self.assertIn("- Write notes.md", prompts[1])
        self.assertNotIn("Read the brief", prompts[1])
        self.assertEqual((result["open_todos"], result["todo_turns"], result["response"]), ([], 1, "Written"))
        self.assertEqual(result["usage"]["total_tokens"], 240)
        self.assertIn("plan 2/2 done", self.notes)

    def test_acp_todo_turns_limit_and_no_progress(self):
        def plan(*open_items):
            return {"steps": [self.acp_plan(*((t, "in_progress") for t in open_items))]}
        # Each carry-on closes an item, so only --todo-turns stops it.
        _, result = self.run_driver("agy", {"turns": [plan("a", "b", "c", "d"), plan("b", "c", "d"), plan("c", "d")]},
                                    todo_turns=2)
        self.assertEqual(len(self.prompts("session/prompt")), 3)
        self.assertEqual((result["open_todos"], result["todo_turns"]), (["c", "d"], 2))
        # A carry-on that closed nothing ends the run, however many turns are left.
        self.events.clear()
        _, result = self.run_driver("agy", {"turns": [plan("a", "b")]}, todo_turns=5)
        self.assertEqual(len(self.prompts("session/prompt")), 2)
        self.assertEqual((result["open_todos"], result["todo_turns"]), (["a", "b"], 1))
        self.assertTrue(any("closed none" in n for n in self.notes))
        # 0 turns it off; a plan with no open items never carries on.
        for script, kwargs in (({"turns": [plan("a")]}, {"todo_turns": 0}),
                               ({"turns": [{"steps": [{"notification": "session/update", "params": {"update": {"sessionUpdate": "plan", "entries": [
                                   {"content": "a", "status": "completed"}, {"content": " ", "status": "pending"}, "bad",
                                   {"status": "pending"}]}}}]}]}, {})):
            self.events.clear()
            _, result = self.run_driver("agy", script, **kwargs)
            self.assertEqual((len(self.prompts("session/prompt")), result["todo_turns"]), (1, 0))

    def test_acp_refusal_and_open_plan_carry_ons_count_apart(self):
        turns = [{"steps": [acp(command="git commit"), self.acp_plan(("commit", "pending"), ("notes", "pending"))]},
                 {"steps": [self.acp_plan(("commit", "cancelled"), ("notes", "pending"))]},
                 {"steps": [self.acp_plan(("commit", "cancelled"), ("notes", "completed"))]}]
        _, result = self.run_driver("agy", {"turns": turns}, ask="never")
        prompts = self.prompts("session/prompt")
        self.assertEqual(len(prompts), 3)
        self.assertIn("was refused", prompts[1])
        self.assertIn("- notes", prompts[2])
        self.assertNotIn("- commit", prompts[2])
        self.assertEqual(result["open_todos"], [])

    def test_codex_open_plan_items_start_another_turn(self):
        turns = [{"steps": [self.codex_plan(("tests", "completed"), ("README", "inProgress"))], "answer": "Half"},
                 {"steps": [self.codex_plan(("tests", "completed"), ("README", "completed"))], "answer": "All done"}]
        driver, result = self.run_driver("codex", {"turns": turns})
        inputs = self.prompts("turn/start")
        self.assertEqual(len(inputs), 2)
        self.assertIn("- README", inputs[1])
        self.assertEqual((result["open_todos"], result["todo_turns"]), ([], 1))
        self.assertIn("All done", result["response"])
        self.assertTrue(driver.turn_ended.is_set())

    def test_codex_no_carry_on_after_quota_or_failure(self):
        open_plan = self.codex_plan(("README", "pending"))
        quota = {"notification": "account/rateLimits/updated", "params": {"rateLimits": {
            "primary": {"usedPercent": 100}, "rateLimitReachedType": "primary"}}}
        for turn in ({"steps": [open_plan, quota]}, {"steps": [open_plan], "status": "failed", "error": {"message": "x"}}):
            self.events.clear()
            _, result = self.run_driver("codex", {"turns": [turn, {"answer": "never asked"}]})
            self.assertEqual(len(self.prompts("turn/start")), 1)
            self.assertEqual((result["open_todos"], result["todo_turns"]), (["README"], 0))

    def test_acp_resume_and_cap_after_turn(self):
        driver, result = self.run_driver("agy", conversation="old-session", max_tokens=100)
        self.assertEqual(result["conversation_id"], "old-session")
        self.assertEqual(driver.capped, 120)
        self.assertIn("session/resume", [e["msg"].get("method") for e in self.events])

    def test_acp_cap_during_turn(self):
        with mock.patch.object(broker, "AGY_BRAIN", os.path.join(self.cwd, "brain")):
            path = broker.agy_transcript_path("fake-session")
            rows = [{"step_index": 1, "source": "MODEL", "input_tokens": 20, "output_tokens": 5},
                    {"step_index": 1, "source": "MODEL", "input_tokens": 100, "output_tokens": 20,
                     "cache_read_tokens": 9000},
                    {"step_index": 2, "source": "MODEL", "input_tokens": 60, "output_tokens": 10}]
            text = "not json\n[]\n" + "".join(json.dumps(row) + "\n" for row in rows)
            driver = self.driver("agy", {"turns": [{"steps": [
                {"append": {"path": path, "text": text}}, {"sleep": 10}]}]}, max_tokens=150)
            started = time.monotonic()
            try:
                with self.assertRaises(RuntimeError):
                    driver.run("test prompt")
                self.assertEqual(driver.capped, 190)
                self.assertIsNotNone(driver.transport.process.poll())
            finally:
                driver.close()
            self.assertLess(time.monotonic() - started, 4)
            self.assertEqual(driver.live_tokens, {"in": 160, "out": 30, "total": 190, "cached_in": 9000})
            self.assertFalse(driver.usage_watchdog.is_alive())
            # No turn result came: the run's usage is the transcript's, for the journal.
            self.assertEqual(driver.result()["usage"], {"input_tokens": 160, "output_tokens": 30, "thinking_tokens": 0,
                                                        "cache_read_tokens": 9000, "total_tokens": 9190})

    def test_acp_transcript_resume_baseline(self):
        with mock.patch.object(broker, "AGY_BRAIN", os.path.join(self.cwd, "brain")):
            path = Path(broker.agy_transcript_path("old-session"))
            path.parent.mkdir(parents=True)
            old = {"step_index": 1, "source": "MODEL", "input_tokens": 9000, "output_tokens": 1000}
            path.write_text(json.dumps(old) + "\n", encoding="utf-8")
            rows = [old, {"step_index": 2, "source": "MODEL", "input_tokens": 30, "output_tokens": 10},
                    {"step_index": 3, "source": "MODEL", "input_tokens": 20, "output_tokens": 5}]
            steps = [{"append": {"path": str(path), "text": "".join(json.dumps(row) + "\n" for row in rows)}},
                     {"sleep": 2}]
            driver, _ = self.run_driver("agy", {"turns": [{"steps": steps}]},
                                        conversation="old-session", max_tokens=200)
            self.assertIsNone(driver.capped)
            self.assertEqual(driver.live_tokens["total"], 65)

    def test_acp_transcript_partial_line(self):
        with mock.patch.object(broker, "AGY_BRAIN", os.path.join(self.cwd, "brain")):
            path = broker.agy_transcript_path("fake-session")
            row = json.dumps({"step_index": 1, "source": "MODEL", "input_tokens": 160, "output_tokens": 20})
            steps = [{"append": {"path": path, "text": row}}, {"sleep": 1.5},
                     {"append": {"path": path, "text": "\n" + row + "\n"}}, {"sleep": 2}]
            driver = self.driver("agy", {"turns": [{"steps": steps}]}, max_tokens=200)
            thread, values, errors = self.in_thread(lambda: driver.run("test prompt"))
            try:
                wait_for(lambda: getattr(driver, "transcript_pending", b""))
                self.assertEqual(driver.live_tokens["total"], 0)
                self.assertIsNone(driver.capped)
                wait_for(lambda: driver.live_tokens["total"] == 180)
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(len(values), 1)
                self.assertIsNone(driver.capped)
                self.assertEqual(driver.live_tokens["total"], 180)
            finally:
                driver.close()

    def test_live_cap_after_the_turn_ended_stops_nothing(self):
        driver = self.driver("agy", max_tokens=10)
        driver.turn_ended.set()
        driver.check_cap({"in": 500, "out": 0, "total": 500, "cached_in": 0}, live=True)
        self.assertIsNone(driver.capped)

    def test_acp_resume_baseline_drops_an_unfinished_line(self):
        with mock.patch.object(broker, "AGY_BRAIN", os.path.join(self.cwd, "brain")):
            path = Path(broker.agy_transcript_path("old-session"))
            path.parent.mkdir(parents=True)
            # An earlier process stopped mid-line: its step is not this run's.
            path.write_text('{"step_index": 1, "source": "MODEL", "input_tokens": 9000, "output_tokens": 1000', encoding="utf-8")
            new = {"step_index": 2, "source": "MODEL", "input_tokens": 30, "output_tokens": 10}
            steps = [{"append": {"path": str(path), "text": "}\n" + json.dumps(new) + "\n"}}, {"sleep": 2}]
            driver, _ = self.run_driver("agy", {"turns": [{"steps": steps}]}, conversation="old-session", max_tokens=200)
            self.assertIsNone(driver.capped)
            self.assertEqual(driver.live_tokens["total"], 40)

    def test_codex_file_allow_and_command_refusal_choices(self):
        steps = [codex(changes=[{"path": "one.txt", "kind": "add", "diff": "+one"}]),
                 codex(command="git push", decisions=["accept", "cancel"]),
                 codex(command="git commit", decisions=["accept", "decline"])]
        self.run_driver("codex", {"turns": [{"steps": steps}]})
        self.assertEqual(self.replies(), [{"decision": "accept"}, {"decision": "cancel"}, {"decision": "decline"}])

    def test_unknown_request_and_elicitation(self):
        steps = [{"method": "unknown"}, {"method": "mcpServer/elicitation/request"}]
        self.run_driver("codex", {"turns": [{"steps": steps}]})
        errors = [e["msg"]["error"] for e in self.events if e["dir"] == "out" and "error" in e["msg"]]
        self.assertEqual(errors[0]["code"], -32601)
        self.assertIn({"action": "decline"}, self.replies())

    def test_human_cli_answers_escalation_and_bad_option(self):
        driver = self.driver("codex", {"turns": [{"steps": [codex(command="inspect something")]}]})
        thread, values, errors = self.in_thread(lambda: driver.run("prompt"))
        pending = wait_for(self.pending)
        self.assertEqual(pending["kind"], "command")
        self.assertEqual([o["id"] for o in pending["options"]], ["accept", "decline"])
        self.assertEqual(self.answer_cli("bad")[0], 2)
        broker.atomic_json(self.base + ".answer.json", {"request": pending["request"], "option": "bogus"})
        wait_for(lambda: "ignored invalid answer" in self.notes)
        code, out, _ = self.answer_cli("accept", base=self.base + ".json")
        self.assertEqual(code, 0)
        self.assertIn("Answered command accept", out)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(values[0]["response"], "Done")
        self.assertFalse(os.path.exists(self.base + ".pending.json"))
        self.assertIn("ANSWERED command accept by human", self.notes)

    def test_timeout_denies(self):
        self.run_driver("codex", {"turns": [{"steps": [codex(command="inspect")]}]}, ask_timeout=.05)
        self.assertEqual(self.replies(), [{"decision": "decline"}])
        self.assertIn("ANSWERED command decline by timeout", self.notes)
        self.assertFalse(self.pending())

    def test_never_denies_and_no_human_question(self):
        steps = [codex(command="inspect"), {"method": "item/tool/requestUserInput", "params": {"questions": [
            {"id": "q", "question": "Pick one", "options": [{"label": "alpha"}], "isOther": True}]}}]
        self.run_driver("codex", {"turns": [{"steps": steps}]}, ask="never")
        self.assertEqual(self.replies()[0], {"decision": "decline"})
        self.assertEqual(self.replies()[1], {"answers": {"q": {"answers": [broker.NO_HUMAN]}}})
        self.assertFalse(any(note.startswith("WAITING") for note in self.notes))

    def test_question_other_and_multiple_questions(self):
        question = {"method": "item/tool/requestUserInput", "params": {"questions": [
            {"id": "one", "question": "Which word?", "isOther": True, "options": [{"label": "alpha"}]},
            {"id": "two", "question": "Another word?", "options": [{"label": "beta"}]}]}}
        driver = self.driver("codex", {"turns": [{"steps": [question]}]})
        thread, _, errors = self.in_thread(lambda: driver.run("prompt"))
        pending = wait_for(self.pending)
        self.assertEqual(pending["options"], [{"id": "alpha", "label": "alpha"}, {"id": "other", "label": "other"}])
        self.assertEqual(self.answer_cli("other")[0], 2)
        self.assertEqual(self.answer_cli("other", "custom word")[0], 0)
        wait_for(lambda: (self.pending() or {}).get("request") != pending["request"] and self.pending())
        self.assertEqual(self.answer_cli("beta")[0], 0)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.replies(), [{"answers": {"one": {"answers": ["custom word"]}, "two": {"answers": ["beta"]}}}])

    def test_missing_pending_and_stale_answer(self):
        self.assertEqual(self.answer_cli("accept")[0], 2)
        human = broker.HumanRequests(self.base, "codex", broker.Policy(self.cwd, home=str(ROOT)), self.notes.append, timeout=.1)
        broker.atomic_json(self.base + ".answer.json", {"request": "old", "option": "accept"})
        choice, _ = human.choose(0, "command", "inspect", [{"id": "accept", "label": "accept"},
                                                          {"id": "cancel", "label": "cancel"}], "accept", "cancel")
        self.assertEqual(choice, "cancel")
        self.assertIn("ignored invalid answer", self.notes)

    def test_permissions_profile_deny_and_human_allow(self):
        params = {"permissions": {"network": {"enabled": True}, "fileSystem": {"write": [self.cwd]}}}
        driver = self.driver("codex", ask="never")
        reply = driver.handle("item/permissions/requestApproval", params, 0)
        self.assertEqual(reply, {"permissions": {}, "scope": "turn"})
        driver.human.ask = "human"
        thread, values, errors = self.in_thread(lambda: driver.handle("item/permissions/requestApproval", params, 1))
        wait_for(self.pending)
        self.assertEqual(self.answer_cli("allow")[0], 0)
        thread.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(values, [{"permissions": params["permissions"], "scope": "turn"}])

    def test_file_summary_uses_prior_item_and_only_40_diff_lines(self):
        changes = [{"path": str(ROOT / "other.txt"), "kind": {"type": "update"},
                    "diff": "\n".join("line " + str(i) for i in range(60))}]
        driver = self.driver("codex", {"turns": [{"steps": [codex(changes=changes)]}]})
        thread, _, errors = self.in_thread(lambda: driver.run("prompt"))
        pending = wait_for(self.pending)
        self.assertEqual(pending["summary"].splitlines()[1:], ["line " + str(i) for i in range(40)])
        self.assertEqual(self.answer_cli("decline")[0], 0)
        thread.join(5)
        self.assertEqual(errors, [])

    def test_codex_resume_usage_is_differenced(self):
        before = {"inputTokens": 1000, "outputTokens": 100, "cachedInputTokens": 400, "reasoningOutputTokens": 10}
        after = {"inputTokens": 1200, "outputTokens": 130, "cachedInputTokens": 450, "reasoningOutputTokens": 15}
        _, result = self.run_driver("codex", {"resumeUsage": before, "turns": [{"usage": after}]}, conversation="old")
        self.assertEqual(result["usage"], {"in": 200, "out": 30, "total": 230, "cached_in": 50})  # reasoning is inside output

    def test_resume_errors_are_explicit(self):
        for script in ({"resumeError": True}, {}):
            driver = self.driver("codex", script)
            with self.assertRaises(broker.ResumeError):
                driver.run("continue", "old")
            driver.close()

    def test_codex_live_cap_interrupts_sleep_and_keeps_usage(self):
        total = {"inputTokens": 1000, "cachedInputTokens": 10, "outputTokens": 20}
        steps = [{"notification": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": total}}}, {"sleep": 10}]
        driver = self.driver("codex", {"turns": [{"steps": steps}]}, max_tokens=500)
        start = time.monotonic()
        try:
            driver.run("prompt")
        except RuntimeError:
            pass
        finally:
            driver.close()
        self.assertLess(time.monotonic() - start, 4)
        self.assertEqual(driver.capped, 1020)
        self.assertEqual(driver.tokens["total"], 1020)
        self.assertIsNotNone(driver.transport.process.poll())

    @unittest.skipIf(os.name == "nt", "POSIX group assertion")
    def test_process_group_stopped_at_end_including_child(self):
        driver, _ = self.run_driver("codex", {"child": True})
        process = driver.transport.process
        self.assertIsNotNone(process.poll())
        child = next(e["msg"]["params"]["pid"] for e in self.events if e["msg"].get("method") == "fake/child")
        wait_for(lambda: self.group_gone(process.pid))
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    @staticmethod
    def group_gone(pid):
        try:
            os.killpg(pid, 0)
            return False
        except ProcessLookupError:
            return True

    def test_redaction_for_both_directions_and_numeric_counts(self):
        secret = "do-not-log-secret"
        jwt = "eyJhbGciOiJIUzI1NiJ9.payload.signature"
        steps = [{"notification": "sample", "params": {"authorization": secret, "inputTokens": 42,
                 "nested": {"refresh_token": secret}, "text": "person@example.invalid " + jwt}}]
        self.run_driver("codex", {"turns": [{"steps": steps, "stderr": ['{"cookie":"' + secret + '"}'],
                                               "answer": "person@example.invalid"}]})
        logged = json.dumps(self.events)
        self.assertNotIn(secret, logged)
        self.assertNotIn("person@example.invalid", logged)
        self.assertNotIn(jwt, logged)
        self.assertIn('"inputTokens": 42', logged)
        self.assertEqual(broker.redact({"total_tokens": 123, "token": secret})["total_tokens"], 123)

    def test_rate_limits_only_report_changed_percentages(self):
        driver = self.driver("codex")
        for percent, reset in ((10, 1), (10, 2), (20, 2)):
            driver.notification("account/rateLimits/updated", {"rateLimits": {"primary": {"usedPercent": percent, "resetsAt": reset}}})
        self.assertEqual([n for n in self.notes if n.startswith("rate limits")], ["rate limits primary 10% used", "rate limits primary 20% used"])

    def writer_patches(self):
        import route
        stack = contextlib.ExitStack()
        for name, value in {"CACHE_DIR": self.cwd, "LOG_DIR": os.path.join(self.cwd, "logs"),
                            "RESERVATIONS_PATH": os.path.join(self.cwd, "reservations.json"),
                            "RESERVATIONS_LOCK": os.path.join(self.cwd, "reservations.lock"),
                            "AGY_CONVERSATIONS_PATH": os.path.join(self.cwd, "conversations.json"),
                            "JOURNAL_PATH": os.path.join(self.cwd, "journal.jsonl")}.items():
            stack.enter_context(mock.patch.object(route, name, value))
        stack.enter_context(mock.patch.object(route, "cloud_state", return_value={"pools": []}))
        stack.enter_context(mock.patch.object(route, "pool_fractions_for_model", return_value={}))
        stack.enter_context(mock.patch.object(route, "check_name", return_value={"ok": True, "provider": "agy", "model": "model", "pool": "agy"}))
        return stack

    def test_writer_default_broker_journals_agy_resumed_run_totals(self):
        import route
        with self.writer_patches():

            def fake_start(driver):
                driver.command = [sys.executable, "-B", str(HERE / "fake_agents.py"), "acp"]
                broker.Driver.start(driver)
            # autospec on the function supplies self without replacing the driver itself.
            with mock.patch.object(broker.AcpDriver, "start", fake_start), \
                 mock.patch.object(route, "record_conversation_usage", wraps=route.record_conversation_usage) as record:
                broker.atomic_json(route.AGY_CONVERSATIONS_PATH, {"old-session": {"in": 500, "out": 100, "total": 600, "cached": 30}})
                code = writer.main(["--cwd", self.cwd, "--model", "model", "--agy", "--continue", "old-session",
                                    "--log", self.base + ".json", "--ask", "never"])
            self.assertEqual(code, 0)
            self.assertEqual(record.call_args.args[1]["total"], 720)
            with open(route.JOURNAL_PATH) as handle:
                row = json.loads(handle.readline())
            self.assertEqual(row["tokens"]["total"], 120)
            self.assertEqual(row["conversation_tokens"]["total"], 720)
            with open(self.base + ".events.jsonl") as handle:
                events = [json.loads(line) for line in handle]
            self.assertTrue(any(e["dir"] == "out" for e in events))
            self.assertTrue(any(e["dir"] == "in" for e in events))
            prompt = next(e["msg"]["params"]["prompt"][0]["text"] for e in events if e["msg"].get("method") == "session/prompt")
            self.assertIn("Prefer the exact command", prompt)
            self.assertNotIn("The ONLY command", prompt)

    def test_codex_sample_total_does_not_double_count_reasoning(self):
        before = {"inputTokens": 13776, "outputTokens": 103, "totalTokens": 13879,
                  "cachedInputTokens": 6784, "reasoningOutputTokens": 28}
        after = {"inputTokens": 89307, "outputTokens": 703, "totalTokens": 90010,
                 "cachedInputTokens": 78464, "reasoningOutputTokens": 208}
        _, result = self.run_driver("codex", {"resumeUsage": before, "turns": [{"usage": after}]}, conversation="old")
        self.assertEqual(result["usage"], {"in": 75531, "out": 600, "total": 76131, "cached_in": 71680})

    def test_usage_notifications_arrive_while_human_is_pending(self):
        step = codex(command="inspect")
        step["whileWaiting"] = [{"notification": "thread/tokenUsage/updated", "params": {
            "tokenUsage": {"total": {"inputTokens": 90, "outputTokens": 10, "totalTokens": 100}}}}]
        driver = self.driver("codex", {"turns": [{"steps": [step]}]})
        thread, _, errors = self.in_thread(lambda: driver.run("prompt"))
        wait_for(self.pending)
        wait_for(lambda: driver.tokens["total"] == 100)
        self.assertEqual(self.answer_cli("decline")[0], 0)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_writer_codex_cap_and_quota_exit_codes_and_redacted_files(self):
        import route
        scripts = [({"turns": [{"steps": [{"notification": "thread/tokenUsage/updated", "params": {
                        "tokenUsage": {"total": {"inputTokens": 1000, "outputTokens": 20}}}}, {"sleep": 10}]}]}, 5),
                   ({"turns": [{"status": "failed", "answer": "", "error": {"message": "quota exceeded"}}]}, 6),
                   ({"turns": [{"steps": [{"notification": "sample", "params": {
                        "apiToken": "hidden-secret", "text": "person@example.invalid"}}],
                        "answer": "person@example.invalid"}]}, 0)]
        for script, expected in scripts:
            with self.subTest(expected=expected), self.writer_patches(), \
                 mock.patch.object(route, "check_name", return_value={"ok": True, "provider": "codex", "model": "model"}), \
                 mock.patch.object(route, "codex_path", return_value="fake-codex"):
                def fake_start(driver):
                    driver.command = [sys.executable, "-B", str(HERE / "fake_agents.py"), "codex", "--script", json.dumps(script)]
                    broker.Driver.start(driver)
                with mock.patch.object(broker.CodexAppDriver, "start", fake_start):
                    code = writer.main(["--cwd", self.cwd, "--model", "model", "--codex", "--brief", "brief.md",
                                        "--log", self.base + ".json", "--max-tokens", "500"])
                self.assertEqual(code, expected)
                for suffix in (".json", ".events.jsonl", ".progress.txt"):
                    with open(self.base + suffix) as handle:
                        text = handle.read()
                    self.assertNotIn("hidden-secret", text)
                    self.assertNotIn("person@example.invalid", text)
                with open(route.JOURNAL_PATH) as handle:
                    rows = [json.loads(line) for line in handle]
                self.assertEqual(rows[-1]["status"], {5: "capped", 6: "quota", 0: "ok"}[expected])

    def test_writer_todo_turns_flag_and_open_items_said(self):
        import route
        script = {"turns": [{"steps": [self.codex_plan(("README", "pending"))]}]}
        for flag, turns in (([], 1), (["--todo-turns", "0"], 0)):
            with self.subTest(flag=flag), self.writer_patches(), \
                 mock.patch.object(route, "check_name", return_value={"ok": True, "provider": "codex", "model": "model"}), \
                 mock.patch.object(route, "codex_path", return_value="fake-codex"):
                def fake_start(driver):
                    driver.command = [sys.executable, "-B", str(HERE / "fake_agents.py"), "codex", "--script", json.dumps(script)]
                    broker.Driver.start(driver)
                sys.stdout.seek(0)
                sys.stdout.truncate()
                with mock.patch.object(broker.CodexAppDriver, "start", fake_start):
                    code = writer.main(["--cwd", self.cwd, "--model", "model", "--codex", "--brief", "brief.md",
                                        "--log", self.base + ".json"] + flag)
                self.assertEqual(code, 0)
                with open(self.base + ".json") as handle:
                    result = json.load(handle)
                self.assertEqual((result["open_todos"], result["todo_turns"]), (["README"], turns))
                self.assertIn(f"open plan item(s) after {turns} carry-on turn(s): README", sys.stdout.getvalue())

    def test_writer_resume_failure_falls_back_once_to_legacy(self):
        import route
        with self.writer_patches(), \
             mock.patch.object(route, "check_name", return_value={"ok": True, "provider": "codex", "model": "model"}), \
             mock.patch.object(route, "codex_path", return_value="fake-codex"), \
             mock.patch.object(writer.CodexMeter, "read"), \
             mock.patch.object(route, "release_reservation", wraps=route.release_reservation) as release:
            proc = mock.Mock(returncode=0, stdout=io.StringIO(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}})))
            original_popen = subprocess.Popen
            def launch(command, **kwargs):
                if command[0] == "fake-codex":
                    self.assertEqual(command[1:4], ["exec", "resume", "old"])
                    return proc
                return original_popen(command, **kwargs)
            def fake_start(driver):
                driver.command = [sys.executable, "-B", str(HERE / "fake_agents.py"), "codex", "--script", '{"resumeError":true}']
                broker.Driver.start(driver)
            with mock.patch.object(broker.CodexAppDriver, "start", fake_start), mock.patch.object(subprocess, "Popen", side_effect=launch):
                code = writer.main(["--cwd", self.cwd, "--model", "model", "--codex", "--continue", "old", "Continue"])
            self.assertEqual(code, 0)
            self.assertEqual(release.call_count, 2)

    def test_rename_destination_is_checked_and_contents_are_not_paths(self):
        paths, summary = broker.file_details({"changes": [{"path": "old.txt", "kind": {
            "type": "update", "movePath": "/outside/home/new.txt"}, "diff": "a line"}]})
        self.assertEqual(paths, ["old.txt", "/outside/home/new.txt"])
        policy = broker.Policy(self.cwd, home=str(ROOT))
        self.assertEqual(policy.decide("codex", "file", summary, paths)[0], "deny")
        paths, _ = broker.file_details({"rawInput": {"Path": "good.txt", "Content": "/outside/home"}})
        self.assertEqual(paths, ["good.txt"])

    def test_run_folder_outside_home_is_still_the_run_folder(self):
        # Live run: a scratch folder under /private/tmp had its own file write refused.
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as work:
            policy = broker.Policy(work, home=home)
            self.assertEqual(policy.decide("agy", "file", "", [os.path.join(work, "hello.txt")])[0], "allow")
            self.assertEqual(policy.decide("agy", "file", "", ["/etc/hosts"])[0], "deny")
            # Review: an option between git and push slipped past the rule.
            self.assertEqual(policy.decide("agy", "command", "", command="git -C . push")[0], "deny")
            self.assertEqual(policy.decide("codex", "command", "", command="cat ../../../../../../etc/passwd", cwd=work)[0], "deny")

    def test_acp_tool_output_is_redacted_in_progress(self):
        driver = self.driver("agy")
        driver.notification("session/update", {"update": {"sessionUpdate": "tool_call_update", "toolCallId": "x",
            "status": "completed", "rawOutput": {"apiToken": "never-log-this", "text": "person@example.invalid"}}})
        self.assertNotIn("never-log-this", "\n".join(self.notes))
        self.assertNotIn("person@example.invalid", "\n".join(self.notes))

    def test_transport_matches_overlapping_out_of_order_responses(self):
        driver = self.driver("codex")
        driver.start()
        first, one, errors_one = self.in_thread(lambda: driver.transport.request(
            "fake/delayedEcho", {"delay": .15, "value": "first"}))
        second, two, errors_two = self.in_thread(lambda: driver.transport.request(
            "fake/delayedEcho", {"delay": 0, "value": "second"}))
        first.join(5)
        second.join(5)
        self.assertEqual(errors_one + errors_two, [])
        self.assertEqual(one, [{"value": "first"}])
        self.assertEqual(two, [{"value": "second"}])
        self.assertEqual(driver.transport.pending, {})


class ReviewRound2Tests(unittest.TestCase):
    """Fixes from the broker review of."""

    def test_protected_folders_beyond_the_first_three(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as cwd:
            policy = broker.Policy(cwd, home=str(ROOT))
            for target in (".claude/settings.json", ".aws/credentials", ".config/ask/machine.json", ".netrc", ".kube/config"):
                self.assertEqual(policy.decide("agy", "file", "edit", [str(ROOT / target)], write=True)[0], "deny", target)
                self.assertEqual(policy.decide("codex", "command", "", command="cat ~/" + target)[0], "deny", target)
            self.assertEqual(policy.decide("agy", "file", "edit", [os.path.join(cwd, "aws-notes.md")], write=True)[0], "allow")
            self.assertEqual(policy.decide("agy", "file", "read", ["file://" + os.path.join(cwd, "a.txt")])[0], "allow")
            self.assertEqual(policy.decide("agy", "file", "read", ["file:///outside/home"])[0], "deny")

    def test_redaction_of_more_token_shapes_and_assignments(self):
        for secret in ("ya29.a0AfH6SMBxxxxxxxxxxxxxxxxxxxxxx", "ghs_abcdefghijklmnopqrstuvwxyz01", "AKIAIOSFODNN7EXAMPLE"):
            self.assertNotIn(secret, broker.redact("value " + secret + " end"), secret)
        self.assertEqual(broker.redact({"password": "p", "X-Api-Key": "k", "count": 3}), {"password": "<redacted>", "X-Api-Key": "<redacted>", "count": 3})
        self.assertNotIn("hunter2", broker.redact("OPENAI_API_KEY=hunter2hunter2 and {'refresh_token': 'hunter2hunter2'}"))
        self.assertIn("<redacted>", broker.redact("export GITHUB_TOKEN='hunter2hunter2'"))
        self.assertEqual(broker.redact("tokens=42 total, reasoningOutputTokens=3"), "tokens=42 total, reasoningOutputTokens=3")
        self.assertEqual(broker.redact("token_count=42"), "token_count=42")  # a bare number is a count, not a secret
        for text in ("password=hunter2", "PIN_SECRET=123456", "api_key=a1b2c3", 'PASSWORD="abcd\\"supersecret"', "secret='a\\'b' end"):
            out = broker.redact(text)
            self.assertNotIn(text.split("=", 1)[1].strip("\"'")[:4], out.split("=", 1)[1], (text, out))
        self.assertEqual(broker.redact('PASSWORD="abcd\\"supersecret" rest'), "PASSWORD=<redacted> rest")

    def test_reasoning_masked_inside_a_result(self):
        logged = []
        value = {"id": 1, "result": {"thread": {"turns": [{"items": [{"type": "reasoning", "summary": ["secret thought"]},
                                                                    {"type": "agentMessage", "text": "hello"}]}]}}}
        broker.mask_reasoning(value)
        self.assertEqual(value["result"]["thread"]["turns"][0]["items"][0]["summary"], "<reasoning>")
        self.assertEqual(value["result"]["thread"]["turns"][0]["items"][1]["text"], "hello")

    def test_answer_pending_without_kind(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as cwd:
            base = os.path.join(cwd, "run")
            broker.atomic_json(base + ".pending.json", {"expires": time.time() + 60, "options": [{"id": "allow"}], "request": 1})
            self.assertEqual(broker.answer_pending(base, "allow")["request"], 1)

    def test_codex_resume_without_a_new_update_bills_nothing(self):
        before = {"inputTokens": 1000, "outputTokens": 100, "cachedInputTokens": 400, "reasoningOutputTokens": 10}
        case = BrokerTests("test_codex_resume_usage_is_differenced")
        case.setUp()
        try:
            _, result = case.run_driver("codex", {"resumeUsage": before, "turns": [{"usage": "none"}]}, conversation="old")
        finally:
            case.tmp.cleanup()
        self.assertEqual(result["usage"]["total"], 0)



if __name__ == "__main__":
    unittest.main()
