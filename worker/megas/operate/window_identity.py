"""Verify native X11 window identity in an installed operator's MCP listing."""

import hashlib
import json
import math


def window_ref(session, window):
    if not isinstance(session, dict) or not isinstance(window, dict):
        return None
    xid, pid, created = (window.get(key) for key in ("window_id", "process_id", "process_created"))
    if (type(xid) is not int or xid <= 0 or type(pid) is not int or pid <= 0
            or type(created) not in (int, float) or not math.isfinite(created) or created <= 0):
        return None
    fields = [session.get(key) for key in ("session_id", "display", "session_type")]
    if not all(isinstance(value, str) and value for value in fields):
        return None
    data = json.dumps([*fields, xid, pid, float(created)], separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def matches_window_inventory(body, arguments):
    """A list handle's indices must address exactly the rows returned to the client."""
    result = body.get("result")
    rows = result.get("windows") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return False
    seen = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("index") != index:
            return False
        ref = window_ref(body.get("desktop_session"), row)
        if "window_ref" not in row or row["window_ref"] != ref:
            return False
        if ref is not None:
            if ref in seen:
                return False
            seen.add(ref)
        if any(row.get(key) != arguments[key] for key in ("window_ref", "agent_window_id") if key in arguments):
            return False
    return True
