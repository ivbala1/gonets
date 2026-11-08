"""Аргументы MCP Grafana, по которым чтение отличается от записи: нужны
запуску по чужому тексту (untrusted), где разрешён только MCP на чтение."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot.approval import _mcp_args_readonly  # noqa: E402


class GrafanaMcpArgsTests(unittest.TestCase):
    def test_read_operations(self) -> None:
        for tool, args in (
            ("alerting_manage_rules", {"operation": "list"}),
            ("alerting_manage_rules", {"operation": "versions", "rule_uid": "x"}),
            ("alerting_manage_routing", {"operation": "get_contact_points"}),
            ("grafana_api_request", {"endpoint": "/api/org"}),
            ("grafana_api_request", {"endpoint": "/api/org", "method": "get"}),
        ):
            with self.subTest(tool=tool, args=args):
                self.assertTrue(_mcp_args_readonly(tool, args))

    def test_write_or_unknown(self) -> None:
        for tool, args in (
            ("alerting_manage_rules", {"operation": "create"}),
            ("alerting_manage_rules", {}),
            ("alerting_manage_routing", {"operation": "update_policy"}),
            ("grafana_api_request", {"endpoint": "/api/x", "method": "POST"}),
            ("update_dashboard", {"operation": "get"}),
        ):
            with self.subTest(tool=tool, args=args):
                self.assertFalse(_mcp_args_readonly(tool, args))


if __name__ == "__main__":
    unittest.main()
