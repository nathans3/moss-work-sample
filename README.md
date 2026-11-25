# Moss Inference work sample

What I actually ran against Moss Inference on Nov 25, 2026 (portal, Inference API, Tasks API), with the scripts and raw logs. The write-up is the one-pager; this repo is the evidence behind it.

Total spend for everything here: **$0.20** of the $10 sign-up credit (12 browser tasks ≈ $0.17, ~140 inference calls ≈ $0.02).

## Layout

```
scripts/
  common.py              shared HTTP helper; logs every call (status, latency, Moss headers, body) to JSONL
  inference_tests.py     Inference API: latency, model comparison, retrieval, cache, OpenAI SDK compat,
                         tool calling, ~25 edge cases, 25-request burst
  repro_empty_input.py   repro for the empty-input bug (below)
  retrieval_latency.py   retrieval overhead with the reply cache off
  tasks_tests.py         Tasks API: `validate` (26 bad requests), `cancel` (idempotency + cancel), `run` (8 real tasks)
  sdk_tasks_check.py     same thing through the Python SDK, plus the SDK-only tasks.trace
fixtures/injection.html  page with hidden instructions, used as a prompt-injection target
logs/                    raw JSONL for every call + console output of each run + task traces
logs/first_attempt/      my first inference run, before I fixed a parsing bug in my own script (kept for honesty)
```

## Run it

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv moss openai requests python-dotenv
printf 'MOSS_PROJECT_ID=...\nMOSS_PROJECT_KEY=...\n' > .env
cd scripts
../.venv/bin/python inference_tests.py
../.venv/bin/python repro_empty_input.py
../.venv/bin/python tasks_tests.py validate     # free
../.venv/bin/python tasks_tests.py run          # ~$0.17
```

Note: the `moss` package needs Python ≥ 3.10. On the macOS system Python (3.9) `pip install moss` just says "No matching distribution found", which is a confusing first-run error.

## Inference API results

**Latency** (client side, from my laptop in the US, cache off, streamed). Server-side first token in Logs & Traces was ~55 ms for fastlane.

| model | median TTFT | median total |
|---|---|---|
| moss/fastlane-1 (n=20) | 144 ms (p95 183) | 212 ms |
| deepseek/deepseek-v4.1-flash | 480 ms | 507 ms |
| google/gemini-3.5-flash-lite | 552 ms | 553 ms |
| anthropic/claude-haiku-5.5 | 420 ms | 515 ms |
| openai/gpt-6-luna | 803 ms | 909 ms |

**Retrieval** on the onboarding sample index (100 products): `X-Moss-Retrieve-Ms` median 7.8 ms, max 10 ms. Answers were grounded and correct, and when I asked for prices (which the sample data doesn't have) it said so instead of inventing them.

**Things that worked well:** OpenAI Python SDK works unchanged (Responses + Chat Completions streaming), function tool calls come back in the right shape, multi-turn via `input` list works, and the error messages are some of the best I've seen: they tell you what to send instead (`previous_response_id is not supported. Send the whole conversation in input.`). 4 MB body → 413, 405k-token prompt → `context_length_exceeded` with the actual count.

**Where it broke**

1. **Empty input is accepted and billed, and fastlane makes something up.** `input: ""`, `"   "`, `"\n"` or an empty user message → `200`, 87 input tokens billed, and a random reply: a description of a Kia Picanto, a weather forecast for Chengdu, a story continuation, replies in Chinese. In 3 of 18 such runs it said it is **"Qwen, developed by Alibaba"** instead of Moss fastlane-1. Same input on claude-haiku-5.5 → `400 model_error` with Anthropic's raw message passed through. `input: []` is correctly rejected. Repro: `scripts/repro_empty_input.py`, log in `logs/repro_empty_input_*.jsonl`.
2. **Wrong error for unknown / too many indexes.** An index name that doesn't exist returns `403 permission_error: names an index outside this project`, which sends you looking at permissions. 33 index names (limit is 32) returns the same 403 rather than the documented 400, because the existence check runs before the count check.
3. **Internal parameter names leak into errors.** `max_output_tokens: -1` → "max_tokens must be at least 1"; an image sent to fastlane → "At most 0 image(s) may be provided in one prompt" (docs say text-only; the message should just say that).
4. **Rate limit is easy to hit sequentially.** Plain sequential calls at ~5 req/s get 429 after ~15 requests (120/min sustained + 10 burst, as documented). A 25-way concurrent burst → 10 OK / 15 × 429 with `Retry-After: 1`. Works as designed, but a first-time user's simple loop will hit it.
5. Reply-cache hits report `input_tokens: 0` and `X-Moss-Retrieve-Ms: 0.0` even when the original call retrieved. Fine for billing, but it made my first retrieval measurement look like retrieval never ran.

## Tasks API results

`validate`: all 26 malformed requests were rejected with a clear 400/413 (bad URLs incl. `javascript:` and `file://`, out-of-range limits, schema not an object, oversized goal/metadata/body). Unknown/malformed task ids → 404. Nothing slipped through. Idempotency works, including reordered JSON keys; same key + different body → 422. Cancel of a running task took ~4 s and cost $0.0003. Path traversal on the files route → 404.

| scenario | model | status | time | cost | notes |
|---|---|---|---|---|---|
| HN top story, with schema | moss-fast | completed, schema passed | 18 s | $0.005 | correct |
| Wikipedia Moss → Bryophyta species count | moss-balanced | completed, schema passed | 59 s | $0.096 | **did not do the task** (see below) |
| CEO phone on example.com (impossible) | moss-fast | completed | 16 s | $0.006 | correctly said there's none |
| schema the page can't satisfy | moss-fast | completed, `passed: false` | 42 s | $0.008 | portal shows it as "Succeeded" |
| `max_cost_usd: 0.01` | moss-fast | completed | 34 s | $0.007 | 5 random pages for under a cent, never hit the cap |
| `max_runtime_seconds: 60`, huge goal | moss-fast | failed `time_limit` | 62 s | $0.013 | limit enforced |
| hidden prompt injection on page | moss-fast | completed | 40 s | $0.008 | ignored the injected instruction |
| example.com heading | deepseek-v4.1-flash | completed | 17 s | $0.010 | correctly noticed the page has no `<h1>` any more; moss-fast just returned the title |

**Where it broke / what I didn't like**

1. **"Completed + verification passed" doesn't mean the goal was done.** In the Bryophyta task the agent's first step says it will follow the Bryophyta link, but the trace shows it never left `/wiki/Moss`; it returned that page's number with `source_url: .../wiki/Moss`. Verification only checks the schema, so it passed. Trace: `logs/trace_task_7d755b727c624a3ca03b5d3f7d056ea9.json`.
2. **Python SDK usage doesn't match the docs.** Docs say `usage.cost.total_usd`; the SDK's `TaskUsage` has no `cost`, and its `cost_usd` is the model part only ($0.0956 vs $0.0959 total), and steps/browser time are only in `.raw`.
3. Undocumented surface: `GET /v1/tasks` (list) works, `tasks.trace` exists in the SDK but not in the REST reference, `live_url` was always null, and the `model_not_found` error lists `moss-fast-beta` and `moss-balanced-handoff`, which aren't in the docs.
4. No queued state observed and no live view: the portal says "screenshots appear when the task ends", and Steps stays at 0 while running.
5. My first injection run hit a GitHub CDN 404 and the task still reported "completed" with "The supplied URL displays 404: Not Found". That's honest, but a failed page load probably shouldn't count toward a 91% success rate.

## Portal notes (from clicking through)

- Onboarding says "we created product-search context with 100 sample documents. Ask it anything", but the Playground then defaults to **No indexes**, so my first question got "I do not have any waterproof jackets to sell."
- The sample prompt chip "wireless headphones under $200" implies prices, but the sample docs contain none.
- The index page showed "Queries, 24h: 0" after a dozen inference-time retrievals.
- The Tasks list re-orders live as new tasks arrive; I clicked one row and opened a different task.
- Models, Logs & Traces, Analytics and Usage drop out of the project URL (`/dashboard/inference/...`) while Configure/Playground/Tasks stay inside it, so the breadcrumb/project context jumps around.
- Good: request ids are everywhere and copyable, Logs & Traces showed every call (including cache hits and errors) within seconds, per-call TTFT/total/tokens/retrievals in one table, and auto-named tasks are a really nice touch.
