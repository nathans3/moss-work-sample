"""Retrieval overhead with the reply cache off: same questions, index on vs off."""
import statistics, time
from common import Log, call, short

log = Log("retrieval_latency")
qs = ["Do you have a jacket for rainy hikes?", "Something warm for winter?", "Shorts for running?",
      "What's good for yoga?", "A shirt for hot weather?", "Anything for skiing?",
      "Do you sell headphones?", "A hat for cold mornings?"]
rows = {"off": [], "on": []}
rms = []
for q in qs:
    for mode in ("off", "on"):
        h = {"X-Moss-Cache": "off"}
        if mode == "on":
            h["X-Moss-Indexes"] = "product-search-context"
        s, b, hd, ms = call("POST", "/responses", log=log, label=f"retr_{mode}", headers=h,
                            json={"model": "moss/fastlane-1", "input": q, "max_output_tokens": 60})
        rows[mode].append(ms)
        if mode == "on":
            rms.append(float(hd.get("x-moss-retrieve-ms", "nan")))
            print(f"{q:40s} retrieve={hd.get('x-moss-retrieve-ms')}ms total={ms:.0f}ms | {short(b, 110)}")
        time.sleep(0.7)
print(f"\nretrieve_ms header: median={statistics.median(rms):.1f} max={max(rms):.1f}")
print(f"total ms (60 tok cap) median: index off={statistics.median(rows['off']):.0f} on={statistics.median(rows['on']):.0f}")
print("log:", log.path)
