"""One operator-state authority for native, stdio and Worker-relayed clients."""
from __future__ import annotations

import os
from pathlib import Path


def operator_home() -> Path:
    # The Worker pins MEGAS_HOME to its connection/configuration directory.
    # Presentation and all MCP transports must keep the operator's existing
    # account, grants, Actions and project registry instead of opening an empty
    # second store there. MEGAS_CHATGPT_HOME is the historical shell-provided
    # operator path, retained for existing installed environments.
    configured = (os.environ.get("MEGAS_OPERATOR_HOME")
                  or os.environ.get("MEGAS_CHATGPT_HOME")
                  or os.environ.get("MEGAS_HOME"))
    return Path(configured).expanduser() if configured else Path.home() / ".megas"
