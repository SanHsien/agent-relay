"""Per-machine settings: where the store lives, who I am, which doorbells ring.

    ~/.agent-relay/config.json      (AGENT_RELAY_HOME overrides the folder)

    {
      "store": "C:/Users/me/.agent-relay/store",
      "agent": "claude-code",
      "doorbells": [{"type": "toast"}, {"type": "handoff", "project_roots": ["C:/GitHub"]},
                    {"type": "command", "argv": ["bash", "notify.sh", "{summary}"]}]
    }

AGENT_RELAY_STORE and AGENT_RELAY_AGENT override the file, so an agent that only
has a shell can still be pointed at the right mailbox.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("AGENT_RELAY_HOME") or Path.home() / ".agent-relay")


def load() -> dict:
    path = home() / "config.json"
    cfg: dict = {}
    if path.is_file():
        cfg = json.loads(path.read_text(encoding="utf-8"))
    if os.environ.get("AGENT_RELAY_STORE"):
        cfg["store"] = os.environ["AGENT_RELAY_STORE"]
    if os.environ.get("AGENT_RELAY_AGENT"):
        cfg["agent"] = os.environ["AGENT_RELAY_AGENT"]
    cfg.setdefault("store", str(home() / "store"))
    cfg.setdefault("doorbells", [])
    return cfg
