"""
Poke at the Moss Inference API: happy path, latency, retrieval, OpenAI compat,
and a pile of edge cases. Every call lands in logs/inference_*.jsonl.

    python scripts/inference_tests.py
"""
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from openai import OpenAI

from common import AUTH, BASE, PROJECT_ID, PROJECT_KEY, Log, call, short

log = Log("inference")
INDEX = "product-search-context"  # the sample index onboarding created
results = []


def record(name, ok, detail):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'NOTE'}] {name}: {detail}")


def resp(body, label, headers=None, **kw):
    return call("POST", "/responses", log=log, label=label, json=body, headers=headers, **kw)


# ---------- 1. basics ----------
def test_models_list():
    s, b, _, _ = call("GET", "/models", log=log, label="models")
    ids = [m["id"] for m in b.get("data", [])]
    portal = {  # copied from the portal's Models page
        "moss/fastlane-1", "z-ai/glm-5.3", "moonshotai/kimi-k3", "deepseek/deepseek-v4.1-flash",
        "qwen/qwen3.8-flash", "deepseek/deepseek-v4.1-flash-1017", "z-ai/glm-5.3-flash",
        "minimax/minimax-m3.1-flash", "openai/gpt-6-luna", "openai/gpt-6.1-terra",
        "openai/gpt-6.1-sol", "openai/gpt-6-astra", "anthropic/claude-haiku-5.5",
        "anthropic/claude-sonnet-5.5", "anthropic/claude-opus-5.5", "anthropic/claude-fable-5.1",
        "google/gemini-3.8-flash", "google/gemini-3.5-flash-lite", "google/gemini-4-argon",
    }
    missing = sorted(portal - set(ids))
    extra = sorted(set(ids) - portal)
    record("GET /models", s == 200,
           f"{len(ids)} models from API vs {len(portal)} on portal; missing from API={missing} extra={extra}")


def test_basic():
    s, b, h, ms = resp({"model": "moss/fastlane-1", "input": "Say hi in five words."}, "basic")
    record("basic response", s == 200, f"{ms:.0f}ms total, usage={b.get('usage')} -> {short(b)}")


# ---------- 2. latency ----------
def stream_ttft(model, prompt, headers=None, label="ttft"):
    body = {"model": model, "input": prompt, "stream": True}
    t0 = time.perf_counter()
    first = None
    events = 0
    text = []
    with requests.post(BASE + "/responses", headers={**AUTH, **(headers or {})},
                       json=body, stream=True, timeout=120) as r:
        rid = r.headers.get("x-moss-request-id")
        for line in r.iter_lines():
            if not line or not line.startswith(b"data:"):
                continue
            ev = json.loads(line[5:])
            events += 1
            if ev.get("type") == "response.output_text.delta":
                if first is None:
                    first = time.perf_counter()
                text.append(ev.get("delta", ""))
        status = r.status_code
    end = time.perf_counter()
    rec = dict(label=label, model=model, status=status, request_id=rid,
               ttft_ms=round((first - t0) * 1000, 1) if first else None,
               total_ms=round((end - t0) * 1000, 1), events=events, text="".join(text)[:300])
    log.write(**rec)
    return rec


def pct(xs, p):
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, round(p / 100 * (len(xs) - 1))))
    return xs[k]


def test_latency(n=20):
    ttfts, totals = [], []
    for i in range(n):
        # vary the prompt so the reply cache can't help
        r = stream_ttft("moss/fastlane-1", f"In one sentence, what is {i * 7 + 3} squared?",
                        headers={"X-Moss-Cache": "off"}, label="latency_fastlane")
        time.sleep(0.6)
        if r["ttft_ms"]:
            ttfts.append(r["ttft_ms"])
            totals.append(r["total_ms"])
    record("fastlane TTFT (client side, from my laptop)", True,
           f"n={len(ttfts)} p50={statistics.median(ttfts):.0f}ms p95={pct(ttfts, 95):.0f}ms "
           f"min={min(ttfts):.0f}ms max={max(ttfts):.0f}ms | total p50={statistics.median(totals):.0f}ms")


def test_model_compare():
    for model in ["moss/fastlane-1", "anthropic/claude-haiku-5.5", "openai/gpt-6-luna",
                  "google/gemini-3.5-flash-lite", "deepseek/deepseek-v4.1-flash"]:
        runs = [stream_ttft(model, f"Name {k} fruits, comma separated.", headers={"X-Moss-Cache": "off"},
                            label="model_compare") for k in (3, 4, 5) if not time.sleep(0.6)]
        ok = [r for r in runs if r["ttft_ms"]]
        if ok:
            record(f"TTFT {model}", True,
                   f"median ttft={statistics.median(r['ttft_ms'] for r in ok):.0f}ms "
                   f"total={statistics.median(r['total_ms'] for r in ok):.0f}ms")
        else:
            record(f"TTFT {model}", False, f"no text streamed, status={runs[0]['status']}")


# ---------- 3. retrieval ----------
def test_retrieval():
    q = "Which waterproof jackets do you sell and how much are they?"
    s1, b1, h1, ms1 = resp({"model": "moss/fastlane-1", "input": q}, "retrieval_off")
    s2, b2, h2, ms2 = resp({"model": "moss/fastlane-1", "input": q}, "retrieval_on",
                           headers={"X-Moss-Indexes": INDEX})
    record("no index named", s1 == 200, short(b1, 200))
    record("with X-Moss-Indexes", s2 == 200,
           f"retrieve_ms={h2.get('x-moss-retrieve-ms')} total={ms2:.0f}ms -> {short(b2, 300)}")
    # retrieval on but switched off
    s3, b3, h3, _ = resp({"model": "moss/fastlane-1", "input": q}, "retrieval_forced_off",
                         headers={"X-Moss-Indexes": INDEX, "X-Moss-Retrieve": "off"})
    record("X-Moss-Retrieve: off", s3 == 200, f"retrieve_ms={h3.get('x-moss-retrieve-ms')} -> {short(b3)}")
    # an index that doesn't exist
    s4, b4, _, _ = resp({"model": "moss/fastlane-1", "input": q}, "retrieval_missing_index",
                        headers={"X-Moss-Indexes": "does-not-exist"})
    record("unknown index name", s4 >= 400, f"{s4} {short(b4)}")
    # 33 indexes, docs say max is 32
    many = ",".join(f"idx{i}" for i in range(33))
    s5, b5, _, _ = resp({"model": "moss/fastlane-1", "input": q}, "retrieval_33_indexes",
                        headers={"X-Moss-Indexes": many})
    record("33 indexes (limit 32)", s5 == 400, f"{s5} {short(b5)}")
    # a prompt-injection-ish doc question: does it stay grounded?
    s6, b6, _, _ = resp({"model": "moss/fastlane-1", "input": "Do you sell any jackets under $20? Only answer from the catalog."},
                        "retrieval_grounding", headers={"X-Moss-Indexes": INDEX})
    record("grounding: jackets under $20", s6 == 200, short(b6, 300))


def test_cache():
    body = {"model": "moss/fastlane-1", "input": "What is the capital of Portugal? One word."}
    _, b1, _, ms1 = resp(body, "cache_1")
    _, b2, _, ms2 = resp(body, "cache_2")
    record("reply cache (same request twice)", True,
           f"first={ms1:.0f}ms second={ms2:.0f}ms; same text={short(b1) == short(b2)}")


# ---------- 4. compat ----------
def test_chat_completions():
    s, b, h, ms = call("POST", "/chat/completions", log=log, label="chat_completions", json={
        "model": "moss/fastlane-1",
        "messages": [{"role": "system", "content": "Answer in one word."},
                     {"role": "user", "content": "Opposite of hot?"}]})
    txt = b.get("choices", [{}])[0].get("message", {}).get("content") if s == 200 else short(b)
    record("chat/completions", s == 200, f"{ms:.0f}ms -> {txt}")


def test_openai_sdk():
    client = OpenAI(base_url=BASE, api_key=f"{PROJECT_ID}:{PROJECT_KEY}",
                    default_headers={"X-Moss-Indexes": INDEX})
    try:
        r = client.responses.create(model="moss/fastlane-1", input="Recommend one hiking product, one line.")
        record("OpenAI python SDK (responses)", True, r.output_text[:200])
        log.write(label="openai_sdk", text=r.output_text)
    except Exception as e:
        record("OpenAI python SDK (responses)", False, repr(e)[:200])
    try:
        stream = client.chat.completions.create(model="moss/fastlane-1", stream=True,
                                                messages=[{"role": "user", "content": "Count to 5."}])
        chunks = [c for c in stream]
        last = chunks[-1]
        record("OpenAI SDK chat stream", True,
               f"{len(chunks)} chunks, final usage={getattr(last, 'usage', None)}")
    except Exception as e:
        record("OpenAI SDK chat stream", False, repr(e)[:200])


def test_tool_calling():
    tools = [{"type": "function", "name": "get_weather", "description": "Weather for a city",
              "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                             "required": ["city"]}}]
    s, b, _, _ = resp({"model": "moss/fastlane-1", "input": "What's the weather in Oslo?", "tools": tools},
                      "tool_call")
    calls = [o for o in b.get("output", []) if o.get("type") == "function_call"] if s == 200 else []
    record("function tool call", bool(calls), f"{s} {calls[:1] or short(b)}")


def test_multiturn():
    convo = [{"role": "user", "content": "My name is Priya."},
             {"role": "assistant", "content": "Nice to meet you, Priya."},
             {"role": "user", "content": "What's my name?"}]
    s, b, _, _ = resp({"model": "moss/fastlane-1", "input": convo}, "multiturn")
    record("multi-turn via input list", s == 200 and "Priya" in short(b, 500), short(b))


# ---------- 5. trying to break it ----------
def test_edges():
    cases = [
        ("no auth", dict(headers={"Authorization": ""}), {"model": "moss/fastlane-1", "input": "hi"}),
        ("bad key", dict(headers={"Authorization": f"Bearer {PROJECT_ID}:moss_nope"}), {"model": "moss/fastlane-1", "input": "hi"}),
        ("key without project id", dict(headers={"Authorization": f"Bearer {PROJECT_KEY}"}), {"model": "moss/fastlane-1", "input": "hi"}),
        ("unknown model", {}, {"model": "moss/fastlane-9", "input": "hi"}),
        ("vendor name w/o prefix", {}, {"model": "gpt-6-luna", "input": "hi"}),
        ("previous_response_id", {}, {"model": "moss/fastlane-1", "input": "hi", "previous_response_id": "resp_123"}),
        ("unknown field", {}, {"model": "moss/fastlane-1", "input": "hi", "banana": True}),
        ("empty input", {}, {"model": "moss/fastlane-1", "input": ""}),
        ("missing input", {}, {"model": "moss/fastlane-1"}),
        ("no model (should default)", {}, {"input": "hi"}),
        ("temperature on anthropic", {}, {"model": "anthropic/claude-haiku-5.5", "input": "hi", "temperature": 0.2}),
        ("temperature=5 on fastlane", {}, {"model": "moss/fastlane-1", "input": "hi", "temperature": 5}),
        ("max_output_tokens=1", {}, {"model": "moss/fastlane-1", "input": "Write a poem", "max_output_tokens": 1}),
        ("max_output_tokens=-1", {}, {"model": "moss/fastlane-1", "input": "hi", "max_output_tokens": -1}),
        ("built-in web_search tool", {}, {"model": "moss/fastlane-1", "input": "hi", "tools": [{"type": "web_search"}]}),
        ("image to fastlane", {}, {"model": "moss/fastlane-1", "input": [{"role": "user", "content": [
            {"type": "input_image", "image_url": "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png"}]}]}),
        ("background:true", {}, {"model": "moss/fastlane-1", "input": "hi", "background": True}),
        ("unicode/emoji input", {}, {"model": "moss/fastlane-1", "input": "Réponds en un mot: 🍕 是什么?"}),
        ("input is a number", {}, {"model": "moss/fastlane-1", "input": 42}),
        ("null bytes", {}, {"model": "moss/fastlane-1", "input": "hi\u0000there"}),
    ]
    for name, extra, body in cases:
        s, b, h, ms = resp(body, f"edge:{name}", **extra)
        record(f"edge: {name}", True, f"{s} ({ms:.0f}ms) {short(b)}")

    # malformed JSON
    r = requests.post(BASE + "/responses", headers={**AUTH, "Content-Type": "application/json"},
                      data="{not json", timeout=30)
    log.write(label="edge:malformed json", status=r.status_code, body=r.text[:500])
    record("edge: malformed json", r.status_code == 400, f"{r.status_code} {r.text[:160]}")

    # body just over 4 MB
    big = "a " * (2 * 1024 * 1024 + 10)
    s, b, _, _ = resp({"model": "moss/fastlane-1", "input": big}, "edge:4MB+ body")
    record("edge: >4MB body", s == 413, f"{s} {short(b)}")

    # ~300k tokens, over fastlane's 262k context, under 4MB
    long = "lorem ipsum dolor sit amet " * 60000
    s, b, _, ms = resp({"model": "moss/fastlane-1", "input": long + "\nHow many words?"}, "edge:context overflow")
    record("edge: context overflow", s == 400, f"{s} ({ms:.0f}ms) {short(b)}")


def test_burst():
    # docs: fastlane burst = 10. Fire 25 at once and see what happens.
    def one(i):
        return resp({"model": "moss/fastlane-1", "input": f"Reply with the number {i}."}, "burst",
                    headers={"X-Moss-Cache": "off"}, retry_429=False)
    with ThreadPoolExecutor(25) as ex:
        out = list(ex.map(one, range(25)))
    codes = {}
    for s, *_ in out:
        codes[s] = codes.get(s, 0) + 1
    retry = [h.get("retry-after") for s, b, h, _ in out if s == 429]
    record("burst of 25 concurrent (limit 10 burst)", True, f"status counts={codes} retry-after={retry[:3]}")


def test_stream_abort():
    time.sleep(10)  # let the burst bucket refill first
    # open a long stream and drop it after a few deltas; then check it doesn't break the next call
    with requests.post(BASE + "/responses", headers=AUTH, stream=True, timeout=60,
                       json={"model": "moss/fastlane-1", "stream": True, "input": "Write 500 words about rivers."}) as r:
        n = 0
        for line in r.iter_lines():
            if line.startswith(b"data:"):
                n += 1
            if n > 5:
                break
    s, b, _, _ = resp({"model": "moss/fastlane-1", "input": "ok?"}, "after_abort")
    record("client aborts a stream mid-way", s == 200, f"next call status {s}")


if __name__ == "__main__":
    for t in [test_models_list, test_basic, test_latency, test_model_compare, test_retrieval, test_cache,
              test_chat_completions, test_openai_sdk, test_tool_calling, test_multiturn,
              test_edges, test_burst, test_stream_abort]:
        try:
            t()
        except Exception as e:
            record(t.__name__, False, f"crashed: {e!r}"[:300])
    print(f"\nlog: {log.path}")
