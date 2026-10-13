"""Mechanical evidence provenance checks, separate from model causal judgement."""

import hashlib
import json


def content_hash(output) -> str:
    return hashlib.sha256(json.dumps(output, sort_keys=True, default=str).encode()).hexdigest()


def valid_evidence(evidence, calls, run_id):
    successful = {call["tool_call_id"]: call for call in calls if call.get("status") == "SUCCEEDED"}
    valid = []
    seen = set()
    for item in evidence:
        call = successful.get(item.get("tool_call_id"))
        output = item.get("result")
        if (call is None or item.get("run_id") != run_id or not output
                or output != call.get("output") or item.get("hash") != content_hash(output)
                or output.get("error") or output.get("partial") or output.get("truncated")):
            continue
        digest = content_hash(output)
        if digest not in seen:
            valid.append(item)
            seen.add(digest)
    return valid
