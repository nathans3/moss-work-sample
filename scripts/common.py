import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

BASE = "https://inference.moss.dev/v1"
PROJECT_ID = os.environ["MOSS_PROJECT_ID"]
PROJECT_KEY = os.environ["MOSS_PROJECT_KEY"]
AUTH = {"Authorization": f"Bearer {PROJECT_ID}:{PROJECT_KEY}"}

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

INTERESTING_HEADERS = [
    "x-moss-request-id",
    "x-moss-retrieve-ms",
    "retry-after",
    "x-should-retry",
    "idempotent-replayed",
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
]


class Log:
    """Appends one JSON line per call so every run can be replayed/inspected later."""

    def __init__(self, name):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = LOG_DIR / f"{name}_{stamp}.jsonl"
        self.f = self.path.open("w")

    def write(self, **rec):
        rec.setdefault("ts", datetime.now(timezone.utc).isoformat())
        self.f.write(json.dumps(rec, default=str) + "\n")
        self.f.flush()


def call(method, path, log=None, label=None, headers=None, timeout=120, retry_429=True, **kw):
    h = {**AUTH, **(headers or {})}
    for attempt in range(4):
        t0 = time.perf_counter()
        r = requests.request(method, BASE + path, headers=h, timeout=timeout, **kw)
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code != 429 or not retry_429:
            break
        if log:
            log.write(label=label, note="429, backing off", attempt=attempt,
                      retry_after=r.headers.get("retry-after"))
        time.sleep(float(r.headers.get("retry-after", 1)) + 0.5)
    try:
        body = r.json()
    except ValueError:
        body = r.text[:2000]
    hdrs = {k: r.headers[k] for k in INTERESTING_HEADERS if k in r.headers}
    if log:
        log.write(label=label, method=method, path=path, status=r.status_code,
                  ms=round(ms, 1), headers=hdrs, body=body)
    return r.status_code, body, hdrs, ms


def short(body, n=160):
    if isinstance(body, dict) and body.get("error"):
        e = body["error"]
        return f"{e.get('type')}/{e.get('code')}: {str(e.get('message'))[:n]}"
    if isinstance(body, dict) and body.get("output"):
        txt = "".join(
            c.get("text", "")
            for item in body["output"] if item.get("type") == "message"
            for c in item.get("content", [])
        )
        return txt[:n].replace("\n", " ")
    return str(body)[:n].replace("\n", " ")
