"""
Same Tasks API through the Python SDK (moss 1.26): tasks.run, then the
`tasks.trace` call that exists in the SDK but not in the REST docs.

    python scripts/sdk_tasks_check.py [task_id_for_trace ...]
"""
import asyncio
import json
import os
import sys

from common import Log, ROOT
from moss import MossInference

log = Log("sdk_tasks")


async def main(trace_ids):
    client = MossInference(os.environ["MOSS_PROJECT_ID"], os.environ["MOSS_PROJECT_KEY"])
    task = await client.tasks.run(
        goal="Open https://example.com and return the page heading.",
        environment={"type": "browser", "start_url": "https://example.com"},
        model="moss-fast",
        output_schema={"type": "object", "properties": {"heading": {"type": "string"}}, "required": ["heading"]},
        limits={"max_runtime_seconds": 180, "max_cost_usd": 0.3},
    )
    log.write(label="sdk_run", id=task.id, status=task.status, result=repr(task.result),
              verification=repr(task.verification), cost=repr(task.usage.cost))
    print("sdk run:", task.id, task.status, task.result, task.verification, task.usage.cost)

    for tid in [task.id, *trace_ids]:
        try:
            tr = await client.tasks.trace(tid)
            out = ROOT / "logs" / f"trace_{tid}.json"
            out.write_text(json.dumps(tr, indent=2, default=str))
            steps = tr.get("steps") if isinstance(tr, dict) else None
            print(f"trace {tid}: saved {out.name}, keys={list(tr)[:8] if isinstance(tr, dict) else type(tr)}"
                  f" steps={len(steps) if steps else '?'}")
        except Exception as e:
            print(f"trace {tid}: {e!r}"[:200])
    await client.close()


asyncio.run(main(sys.argv[1:]))
print("log:", log.path)
