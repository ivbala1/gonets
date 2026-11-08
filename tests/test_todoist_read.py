"""Недоверенный ран не получает Todoist (решение Ивана 04.11.2026: поимённого чтения без карточки больше нет)."""
import re
import unittest

from claude_tg_bot import config, untrusted

T = "mcp__claude_ai_Todoist__"


class TodoistReadTests(unittest.TestCase):
    def test_untrusted_still_denies_todoist(self) -> None:
        mcp_re = re.compile(config.DEFAULT_MCP_AUTO_ALLOW_REGEX)
        for name in ("find-tasks", "get-overview", "search", "add-tasks"):
            self.assertFalse(untrusted.tool_allowed(T + name, mcp_re), name)


if __name__ == "__main__":
    unittest.main()
