"""Claude Code SessionStart hook: start every session with the repo's hand-over bundle.

Add to a settings file (user, project, or a --settings file):

    {"hooks": {"SessionStart": [{"matcher": "startup|clear",
        "hooks": [{"type": "command", "timeout": 15,
                   "command": "python C:/GitHub/agent-relay/hooks/session_start.py"}]}]}}

It reads the hook payload on stdin (UTF-8), prints
{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ...}}
when there is something to hand over, and always exits 0, so a session never
fails to start because of it.
"""

import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent_relay.context import main_hook
except Exception:  # noqa: BLE001 - a broken install must not block a session start
    sys.exit(0)

sys.exit(main_hook())
