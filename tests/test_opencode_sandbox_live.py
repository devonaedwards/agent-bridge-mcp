#!/usr/bin/env python3
"""End-to-end: an opencode subagent launched by the bridge can reach STATE_DIR.

This is the regression test for the failure that motivated the whole change - an
opencode child auto-rejected on ~/.agent-bridge, losing check_notes / ask_parent /
raise_concern while still reporting returncode 0. Run from the repo root.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root
import agent_bridge_mcp as ab  # noqa: E402

# cwd deliberately OUTSIDE STATE_DIR - that is the whole point.
CWD = Path(__file__).resolve().parent / "sandbox_probe"
CWD.mkdir(exist_ok=True)

probe = ab.STATE_DIR / "sandbox_probe.txt"
probe.write_text("STATEDIR_REACHABLE_OK\n")

result = ab.launch_opencode({
    "prompt": (
        f"Read the file {probe} and reply with ONLY its exact contents. "
        "Do not explain, do not use any other tool."
    ),
    "cwd": str(CWD),
    "model": "opencode/deepseek-v4-flash-free",
    "timeout_seconds": 180,
    "multi_phase": False,
})

payload = ab.json.loads(result["content"][0]["text"])
job_id = payload["job_id"]
print(f"launched {job_id}")
print(f"sandbox note: {ab.json.dumps(payload.get('sandbox'), indent=2)}")

job = ab.get_job(job_id)
deadline = time.time() + 180
while job.returncode is None and time.time() < deadline:
    time.sleep(2)

print(f"\nreturncode: {job.returncode}")
print(f"--- stderr ---\n{job.stderr.strip() or '(empty)'}")
print(f"--- stdout tail ---\n{job.stdout[-600:]}")

rejected = "auto-reject" in job.stderr.lower()
reached = "STATEDIR_REACHABLE_OK" in job.stdout

print("\n=== RESULT ===")
print(f"auto-rejection in stderr : {rejected}   (want False)")
print(f"STATE_DIR content reached: {reached}   (want True)")
probe.unlink(missing_ok=True)
sys.exit(0 if (reached and not rejected) else 1)
