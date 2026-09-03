"""tools/reminders.py — the real macOS Reminders app (syncs via iCloud).

macOS-only (osascript backend). On other platforms the tool registers as
unavailable and returns a capability error rather than crashing startup.
"""

from __future__ import annotations

from tools.base import PermissionLevel, Tool
from tools.platform import PLATFORM_NAME, macos_backend
from tools.registry import register

_MACOS = macos_backend()

_DESCRIPTION = (
    "Create, list, complete, or delete reminders in the real macOS Reminders app "
    "(Apple's Reminders.app — syncs to iPhone/iPad/Apple Watch via iCloud). Use this "
    "for ANY request to 'remind me', 'set a reminder', 'add a reminder' or similar — "
    "NOT the `todo` tool, which is a separate local list Reminders.app never sees.\n"
    "Actions:\n"
    "  - add      : create a reminder. `text` is required. If the user gives a time "
    "('at 8pm', 'tomorrow morning', 'in an hour'), compute the absolute due_date "
    "yourself from the current date/time given in your system context and pass it "
    "as due_date='YYYY-MM-DD HH:MM' (24-hour). Omit due_date for an undated reminder.\n"
    "  - list     : list upcoming (incomplete) reminders. Optional `list_name`.\n"
    "  - complete : mark a reminder done. `text` is a (sub)string matching its name.\n"
    "  - delete   : remove a reminder. `text` is a (sub)string matching its name.\n"
    "If `text` matches more than one reminder for complete/delete, the tool returns "
    "the candidates instead of guessing — ask the user which one."
)


class ReminderTool(Tool):
    name = "reminder"
    execution_scope = "device"
    description = _DESCRIPTION
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "list", "complete", "delete"],
                "description": "The reminder operation.",
            },
            "text": {"type": "string", "description": "Reminder title (for add), or a name substring to match (for complete/delete)."},
            "due_date": {"type": "string", "description": "Absolute due date/time as 'YYYY-MM-DD HH:MM' (24-hour), for action=add."},
            "list_name": {"type": "string", "description": "Name of the Reminders list to use (default: the user's default list)."},
        },
        "required": ["action"],
    }
    permission = PermissionLevel.LOW_RISK

    available = _MACOS is not None
    unavailable_reason = (
        None if _MACOS is not None
        else f"the Reminders app is macOS-only (running on {PLATFORM_NAME})"
    )

    def assess(self, arguments: dict):
        if arguments.get("action") == "delete":
            return PermissionLevel.WRITE
        return PermissionLevel.LOW_RISK

    def execute(self, action: str, text: str = None, due_date: str = None, list_name: str = None) -> dict:
        if _MACOS is None:
            return {"error": self.unavailable_reason}
        return _MACOS.run_reminder(action, text, due_date, list_name)


register(ReminderTool())
