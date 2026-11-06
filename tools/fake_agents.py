"""Offline ACP/app-server fixtures. Scripts are JSON data, never code.

    python -B tools/fake_agents.py acp --script '{"turns": [...]}'
Or set BROKER_FAKE_SCRIPT to the same JSON object. No real agents or network.
Each turn has steps (method/params for requests, notification/params, or sleep), answer and usage.
"""
import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time


class FakeServer:
    def __init__(self, protocol, script):
        self.protocol, self.script = protocol, script
        self.lock, self.waiters_lock = threading.Lock(), threading.Lock()
        self.waiters, self.request_id, self.turn = {}, -1, 0
        self.cancelled = threading.Event()

    def send(self, message):
        with self.lock:
            print(json.dumps(message), flush=True)

    def reply(self, request, result):
        self.send({"jsonrpc": "2.0", "id": request["id"], "result": result})

    def notify(self, method, params):
        self.send({"method": method, "params": params})

    def request(self, method, params, while_waiting=()):
        with self.waiters_lock:
            self.request_id += 1
            request_id = self.request_id
            waiter = self.waiters[request_id] = queue.Queue()
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        for note in while_waiting:
            self.notify(note["notification"], note.get("params") or {})
        return waiter.get(timeout=15)

    def run_turn(self, request, turn_index):
        try:
            turns = self.script.get("turns") or [{}]
            turn = turns[min(turn_index, len(turns) - 1)]
            if self.script.get("child") and turn_index == 0:
                child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(60)"])
                self.notify("fake/child", {"pid": child.pid})
            for line in turn.get("stderr", []):
                print(line, file=sys.stderr, flush=True)
            for step in turn.get("steps", []):
                if step.get("hang"):
                    self.cancelled.wait()
                    if self.protocol == "acp":
                        self.reply(request, {"stopReason": "cancelled"})
                    else:
                        self.notify("turn/completed", {"threadId": "fake-thread", "turn": {
                            "id": "fake-turn", "status": "interrupted"}})
                    return
                elif "sleep" in step:
                    time.sleep(step["sleep"])
                elif "notification" in step:
                    self.notify(step["notification"], step.get("params") or {})
                elif "method" in step:
                    if step.get("item"):
                        self.notify("item/started", {"item": step["item"]})
                    response = self.request(step["method"], step.get("params") or {}, step.get("whileWaiting") or [])
                    if "expect" in step and response.get("result") != step["expect"]:
                        raise AssertionError("unexpected reply: " + json.dumps(response))
            answer = turn.get("answer", "Done")
            usage = turn.get("usage") or {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120,
                                          "thoughtTokens": 3, "cachedReadTokens": 10,
                                          "cachedInputTokens": 10, "reasoningOutputTokens": 3}
            if self.protocol == "acp":
                self.notify("session/update", {"sessionId": "fake-session", "update": {
                    "sessionUpdate": "user_message_chunk", "content": {"text": "DO NOT ECHO"}}})
                self.notify("session/update", {"sessionId": "fake-session", "update": {
                    "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": answer}}})
                self.reply(request, {"stopReason": "end_turn", "usage": usage})
            else:
                if turn.get("usage") != "none":  # "none": a turn that ends before any token update reaches the client
                    self.notify("thread/tokenUsage/updated", {"threadId": "fake-thread", "tokenUsage": {"total": usage}})
                self.notify("item/completed", {"threadId": "fake-thread", "item": {
                    "id": "answer", "type": "agentMessage", "text": answer}})
                self.notify("turn/completed", {"threadId": "fake-thread", "turn": {
                    "id": "fake-turn", "status": turn.get("status", "completed"), "error": turn.get("error")}})
        except Exception as error:
            self.send({"id": request["id"], "error": {"code": -32603, "message": str(error)}})
            if self.protocol == "codex":
                self.notify("turn/completed", {"turn": {"status": "failed", "error": {"message": str(error)}}})

    def handle(self, message):
        method = message.get("method")
        if method is None:
            with self.waiters_lock:
                waiter = self.waiters.pop(message.get("id"), None)
            if waiter:
                waiter.put(message)
            return
        if method in ("session/cancel", "turn/interrupt"):
            if method == "turn/interrupt":
                self.reply(message, {})
            if self.script.get("honourCancel"):
                self.cancelled.set()
            return
        if method == "initialized":
            return
        if method == "initialize":
            if self.script.get("echoEnv"):
                self.notify("fake/env", {"value": os.environ.get(self.script["echoEnv"])})
            self.reply(message, {"protocolVersion": 1} if self.protocol == "acp" else {"userAgent": "fake/1.0"})
        elif method == "fake/delayedEcho":
            def echo():
                time.sleep(message["params"].get("delay", 0))
                self.reply(message, {"value": message["params"]["value"]})
            threading.Thread(target=echo, daemon=True).start()
        elif method in ("session/new", "session/resume"):
            self.reply(message, {"sessionId": message["params"].get("sessionId", "fake-session")})
        elif method == "session/set_model":
            self.reply(message, {})
        elif method in ("thread/start", "thread/resume"):
            if method == "thread/resume" and self.script.get("resumeError"):
                self.send({"id": message["id"], "error": {"code": -32602, "message": "resume unavailable"}})
                return
            thread = {"id": message["params"].get("threadId", "fake-thread")}
            if method == "thread/resume" and "resumeUsage" in self.script:
                self.notify("thread/tokenUsage/updated", {"threadId": thread["id"], "tokenUsage": {
                    "total": self.script["resumeUsage"]}})
            self.reply(message, {"thread": thread})
        elif method in ("session/prompt", "turn/start"):
            if method == "turn/start":
                if self.script.get("echoTurnParams"):
                    self.notify("fake/turnParams", message["params"])
                self.notify("turn/started", {"turn": {"id": "fake-turn", "status": "inProgress"}})
                self.reply(message, {"turn": {"id": "fake-turn", "status": "inProgress"}})
            self.cancelled.clear()
            turn_index = self.turn
            self.turn += 1
            threading.Thread(target=self.run_turn, args=(message, turn_index), daemon=True).start()
        else:
            self.send({"id": message["id"], "error": {"code": -32601, "message": "unsupported method"}})

    def run(self):
        for line in sys.stdin:
            self.handle(json.loads(line))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("protocol", choices=("acp", "codex"))
    parser.add_argument("--script", default=os.environ.get("BROKER_FAKE_SCRIPT", "{}"))
    args = parser.parse_args()
    FakeServer(args.protocol, json.loads(args.script)).run()


if __name__ == "__main__":
    main()
