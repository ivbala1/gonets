"""Whitelist путей записи: `.claude` и `.mcp.json` закрыты (хуки и MCP проекта
исполняются при следующем ране), память под `.claude/projects` открыта."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))



class WhitelistClaudeTests(unittest.TestCase):
    """`.claude` и `.mcp.json` в путях APPROVE_WHITELIST_PATHS: хуки и
    MCP-серверы проекта исполняются при следующем ране (guard.md п. 21)."""

    def test_project_claude_config_closed(self) -> None:
        from claude_tg_bot import approval
        state = approval._BOT_ROOT / "state"
        for target in (
            Path("/mnt/c/Users/user/!Obsidian/proj/.claude/settings.local.json"),
            Path("/mnt/c/Users/user/!Obsidian/proj/.claude/settings.json"),
            Path("/home/user/src/domovoy/journal/.claude/agents/a.md"),
            Path("/home/user/src/domovoy/journal/.claude/commands/c.md"),
            Path("/home/user/src/domovoy/journal/.claude/hooks/h.sh"),
            Path("/home/user/src/domovoy/journal/.mcp.json"),
            Path("/home/user/.claude/settings.json"),
        ):
            with self.subTest(target=target):
                self.assertFalse(approval._whitelist_target_ok(target, state))

    def test_memory_under_claude_projects_open(self) -> None:
        from claude_tg_bot import approval
        state = approval._BOT_ROOT / "state"
        self.assertTrue(approval._whitelist_target_ok(
            Path("/home/user/.claude/projects/-home-user-src/memory/a.md"), state))
        self.assertTrue(approval._whitelist_target_ok(
            Path("/mnt/c/Users/user/!Obsidian/Машина/claude.md"), state))


if __name__ == "__main__":
    unittest.main()
