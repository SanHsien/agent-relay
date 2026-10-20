"""Claude Code SessionStart hook: start every session with the repo's hand-over bundle.

Add to a settings file (user, project, or a --settings file):

    {"hooks": {"SessionStart": [{"matcher": "startup|resume|clear|compact",
        "hooks": [{"type": "command", "command": "python C:/GitHub/agent-relay/hooks/session_start.py"}]}]}}

It reads the hook payload on stdin, prints {"hookSpecificOutput": {... "additionalContext": ...}}
when there is something to hand over, and stays silent (exit 0) on any error, so a
session never fails to start because of it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_relay.context import main_hook  # noqa: E402

sys.exit(main_hook())
