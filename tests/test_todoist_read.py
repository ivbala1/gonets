"""Чтение Todoist без карточки поимённо, запись и чужой текст на карточку или отказ."""
import re
import unittest

from claude_tg_bot import approval, config, untrusted

T = "mcp__claude_ai_Todoist__"


class TodoistReadTests(unittest.TestCase):
    def test_reads_pass(self) -> None:
        for name in ("find-tasks", "find-tasks-by-date", "find-projects", "find-sections", "get-overview",
                     "search", "fetch", "user-info"):
            self.assertTrue(approval._todoist_read(T + name), name)

    def test_writes_and_unknown_need_card(self) -> None:
        for name in ("add-tasks", "update-tasks", "complete-tasks", "uncomplete-tasks", "delete-object",
                     "analyze-project-health", "get-project-health", "export-project-template",
                     "import-project-template", "reschedule-tasks", "project-move", "find-tasks-x"):
            self.assertFalse(approval._todoist_read(T + name), name)

    def test_other_servers_not_affected(self) -> None:
        for name in ("mcp__telegram__find-tasks", "mcp__claude_ai_Todoist", "mcp__x__claude_ai_Todoist__search",
                     "find-tasks", ""):
            self.assertFalse(approval._todoist_read(name), name)

    def test_untrusted_still_denies_todoist(self) -> None:
        mcp_re = re.compile(config.DEFAULT_MCP_AUTO_ALLOW_REGEX)
        for name in ("find-tasks", "get-overview", "search", "add-tasks"):
            self.assertFalse(untrusted.tool_allowed(T + name, mcp_re), name)


if __name__ == "__main__":
    unittest.main()
