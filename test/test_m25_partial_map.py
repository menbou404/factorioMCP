import copy
import json
import unittest
from unittest.mock import Mock

from factorio_mcp.server import _normalize_result, create_server


class PartialMapTests(unittest.IsolatedAsyncioTestCase):
    async def test_descriptions_explain_partial_resources_and_action_ids(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for text in (
            "Partially observed cells record only seen resources",
            "partial = true if any cell is partial",
            "tile and amount totals are only what was seen",
            "summed resource tiles are at least as many",
            "a full observation replaces it and clears partial",
            "Partial cells do not count in explored.cells or explored bounds",
            "may appear in frontier",
            "never overwrite fully observed cells",
        ):
            with self.subTest(text=text):
                self.assertIn(text, tools["get_explored_map"].description)
        self.assertIn(
            "seen resources in partially observed cells", tools["observe_nearby"].description
        )
        self.assertIn("action_id (the same value as id", tools["get_agent_status"].description)

    async def test_partial_components_pass_through_with_order_totals_and_filters(self):
        bridge = Mock()
        groups = [
            {
                "name": "coal",
                "cells": 2,
                "tiles": 3,
                "total_amount": 750,
                "partial": True,
                "observed_tick": 12,
                "nearest": {"x": 3.5, "y": 4.5},
                "distance": 0.5,
                "left_top": {"x": 0, "y": 0},
                "right_bottom": {"x": 16, "y": 8},
            },
            {"name": "coal", "cells": 1, "tiles": 4, "total_amount": 800, "distance": 20},
        ]
        measured = {
            "resources": copy.deepcopy(groups),
            "resources_total": 5,
            "explored": {"cells": 1},
            "frontier": [{"x": 12, "y": 4, "distance": 8}],
            "frontier_total": 4,
        }
        bridge.call.return_value = {"tick": 30, "result": measured}
        response = await create_server(bridge).call_tool(
            "get_explored_map", {"limit": 2, "resource_names": ["coal"]}
        )
        value = json.loads(response.content[0].text)
        self.assertFalse(response.is_error)
        self.assertTrue(value["ok"])
        self.assertEqual(value["resources"], groups)
        self.assertNotIn("partial", value["resources"][1])
        for field in ("resources_total", "explored", "frontier", "frontier_total"):
            self.assertEqual(value[field], measured[field])
        bridge.call.assert_called_once_with(
            "get_explored_map", {"limit": 2, "resource_names": ["coal"]}
        )

    async def test_status_preserves_action_id_and_legacy_id_in_both_slots(self):
        bridge = Mock()
        server = create_server(bridge)
        slots = {
            "action": {"id": "23", "action_id": "23", "type": "move", "status": "running"},
            "craft_action": {"id": "24", "action_id": "24", "type": "craft", "status": "running"},
        }
        for measured in ({}, {"action": slots["action"]}, slots):
            with self.subTest(slots=measured):
                bridge.call.return_value = {"tick": 31, "result": copy.deepcopy(measured)}
                response = await server.call_tool("get_agent_status", {})
                value = json.loads(response.content[0].text)
                self.assertFalse(response.is_error)
                self.assertEqual(value, {"ok": True, "tick": 31, "inventory": [], **measured})
                bridge.call.assert_called_with("get_agent_status", None)

    def test_old_results_do_not_gain_synthetic_partial_or_action_id(self):
        groups = [{"name": "coal", "tiles": 1, "total_amount": 100}]
        mapped = _normalize_result("get_explored_map", {"resources": copy.deepcopy(groups)})
        self.assertEqual(mapped["resources"], groups)
        old_action = {"id": "1", "type": "move", "status": "running"}
        status = _normalize_result("get_agent_status", {"action": copy.deepcopy(old_action)})
        self.assertEqual(status["action"], old_action)


if __name__ == "__main__":
    unittest.main()
