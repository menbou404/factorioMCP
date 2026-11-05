import json
import unittest
from unittest.mock import Mock

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import HINTS, create_server


class PlacementVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_descriptions_and_hint_include_current_surroundings_and_recent_observations(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        descriptions = {
            name: tools[name].description
            for name in ("place_entity", "place_entities", "check_placement", "observe_nearby")
        }
        descriptions["not_visible hint"] = HINTS["not_visible"]
        for name, description in descriptions.items():
            with self.subTest(name=name):
                for text in (
                    "character's current surroundings",
                    "within 16 tiles",
                    "count as observed for placement",
                    "any of the last 8 observations",
                    "within 60 seconds",
                ):
                    self.assertIn(text, description)

    async def test_not_visible_preserves_last_explicit_observation_detail(self):
        detail = {"observed": {"x": 0, "y": 0, "surface": "nauvis", "radius": 4, "age_seconds": 87}}
        bridge = Mock()
        bridge.call.side_effect = BridgeError("not_visible", detail=detail)
        response = await create_server(bridge).call_tool(
            "place_entity", {"item": "stone-furnace", "surface": "nauvis", "x": 0, "y": 0}
        )
        value = json.loads(response.content[0].text)
        self.assertTrue(response.is_error)
        self.assertEqual(value["error"], "not_visible")
        self.assertEqual(value["detail"], detail)
        self.assertEqual(value["hint"], HINTS["not_visible"])

    async def test_inventory_and_wait_descriptions_explain_machine_buffers(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name in ("get_entity_inventory", "get_entity_inventories", "wait_for_items"):
            with self.subTest(name=name):
                for text in (
                    "Assemblers and furnaces stop with full_output",
                    "result slot holds a recipe-dependent amount",
                    "Inserters fill machine and lab inputs only up to a small buffer",
                    "a few crafts or research units",
                    "labs may hold only about 2 packs of a type from inserters",
                    "To stock more, insert by hand with transfer_item",
                ):
                    self.assertIn(text, tools[name].description)


if __name__ == "__main__":
    unittest.main()
