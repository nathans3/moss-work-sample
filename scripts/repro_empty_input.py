"""Repro: an empty input to moss/fastlane-1 returns 200 with an unrelated, invented answer."""
import time
from common import Log, call, short

log = Log("repro_empty_input")
cases = [
    ("empty string", {"input": ""}),
    ("empty string, cache off", {"input": ""}, {"X-Moss-Cache": "off"}),
    ("whitespace", {"input": "   "}),
    ("newline", {"input": "\n"}),
    ("empty list", {"input": []}),
    ("empty user message", {"input": [{"role": "user", "content": ""}]}),
    ("empty + instructions", {"input": "", "instructions": "You are a support agent for Acme."}),
    ("empty on chat/completions", None),
    ("empty on haiku", {"input": "", "model": "anthropic/claude-haiku-5.5"}),
]
for c in cases:
    name, body = c[0], c[1]
    hdr = c[2] if len(c) > 2 else {"X-Moss-Cache": "off"}
    for i in range(3):
        if body is None:
            s, b, h, ms = call("POST", "/chat/completions", log=log, label=name, headers=hdr,
                               json={"model": "moss/fastlane-1", "messages": [{"role": "user", "content": ""}]})
            txt = b["choices"][0]["message"]["content"][:140] if s == 200 else short(b)
        else:
            s, b, h, ms = call("POST", "/responses", log=log, label=name, headers=hdr,
                               json={"model": "moss/fastlane-1", **body})
            txt = short(b, 140)
        u = b.get("usage") if isinstance(b, dict) else None
        print(f"{name:28s} run{i} {s} in={u and u.get('input_tokens', u.get('prompt_tokens'))} | {txt}")
        time.sleep(0.7)
print("log:", log.path)
