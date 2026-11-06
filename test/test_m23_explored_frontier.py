import copy
import json
import unittest
from unittest.mock import Mock

from factorio_mcp.server import _normalize_result, create_server


class ExploredFrontierTests(unittest.IsolatedAsyncioTestCase):
    async def test_description_explains_frontier_and_exploration(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        description = tools["get_explored_map"].description
        for text in (
            "current surface",
            "8x8 tiles",
            "unexplored cells sharing an edge with an explored cell",
            "{x, y, distance}",
            "cell centres",
            "sorted by distance then x then y",
            "up to limit",
            "frontier_total counts all frontier cells before truncation",
            "without terrain, resource or tile data",
            "Walk toward the nearest frontier cell",
            "observe_nearby to explore efficiently",
        ):
            with self.subTest(text=text):
                self.assertIn(text, description)

    async def test_frontier_normalization_through_tool_preserves_metadata_and_order(self):
        bridge = Mock()
        server = create_server(bridge)
        entries = [{"x": -4, "y": 4, "distance": 8}, {"x": 4, "y": -4, "distance": 8}]
        for frontier in (None, {}, [], entries):
            with self.subTest(frontier=frontier):
                measured = {
                    "position": {"surface": "nauvis", "x": 4, "y": 4},
                    "cell_size": 8,
                    "explored": {"cells": 1},
                    "frontier_total": 4,
                }
                if frontier is not None:
                    measured["frontier"] = copy.deepcopy(frontier)
                expected = copy.deepcopy(measured)
                bridge.call.return_value = {"tick": 54, "result": measured}
                response = await server.call_tool("get_explored_map", {"limit": 2})
                self.assertFalse(response.is_error)
                value = json.loads(response.content[0].text)
                self.assertEqual(
                    value,
                    {
                        "ok": True,
                        "tick": 54,
                        **expected,
                        "resources": [],
                        "water": [],
                        "trees": [],
                        "rocks": [],
                        "frontier": entries if frontier == entries else [],
                    },
                )
                bridge.call.assert_called_with("get_explored_map", {"limit": 2})

    def test_non_list_frontier_normalizes_to_list(self):
        for frontier in (None, {}, "invalid", 0):
            with self.subTest(frontier=frontier):
                result = _normalize_result(
                    "get_explored_map", {"frontier": frontier, "frontier_total": 0}
                )
                self.assertEqual(result["frontier"], [])
                self.assertEqual(result["frontier_total"], 0)


if __name__ == "__main__":
    unittest.main()
