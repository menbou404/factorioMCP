import copy
import json
import unittest
from unittest.mock import Mock

from factorio_mcp.server import _normalize_result, create_server

PLACEMENT = {"item": "burner-mining-drill", "surface": "nauvis", "x": 6, "y": 6}
DROP = {"x": 6.0, "y": 4.5}


class DrillDropTests(unittest.IsolatedAsyncioTestCase):
    async def test_descriptions_explain_drill_output_tile(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for operation in ("check_placement", "place_entity", "place_entities"):
            with self.subTest(operation=operation):
                description = tools[operation].description
                for text in (
                    "Mining drills include",
                    "drop_position as absolute map coordinates",
                    "the tile where the drill outputs",
                    "put a chest, furnace or belt there",
                ):
                    self.assertIn(text, description)
        self.assertIn(
            "the snapped position and direction, even when not placeable",
            tools["check_placement"].description,
        )

    async def test_placeable_and_blocked_preview_preserve_drop_position(self):
        bridge = Mock()
        server = create_server(bridge)
        for item in ("burner-mining-drill", "electric-mining-drill"):
            for placeable in (True, False):
                with self.subTest(item=item, placeable=placeable):
                    measured = {"placeable": placeable, "drop_position": copy.deepcopy(DROP)}
                    if not placeable:
                        measured["reason"] = "blocked"
                    bridge.call.return_value = {"tick": 24, "result": measured}
                    response = await server.call_tool(
                        "check_placement", {**PLACEMENT, "item": item, "direction": 4}
                    )
                    value = json.loads(response.content[0].text)
                    self.assertFalse(response.is_error)
                    self.assertTrue(value["ok"])
                    self.assertEqual(value["tick"], 24)
                    self.assertIs(value["placeable"], placeable)
                    self.assertEqual(value["drop_position"], DROP)
                    self.assertNotIn("pickup_position", value)
                    bridge.call.assert_called_with(
                        "check_placement", {**PLACEMENT, "item": item, "direction": 4}
                    )

    async def test_single_placement_preserves_real_drop_position(self):
        bridge = Mock()
        server = create_server(bridge)
        for item in ("burner-mining-drill", "electric-mining-drill"):
            with self.subTest(item=item):
                bridge.call.return_value = {
                    "tick": 25,
                    "result": {"name": item, "drop_position": copy.deepcopy(DROP)},
                }
                response = await server.call_tool("place_entity", {**PLACEMENT, "item": item})
                value = json.loads(response.content[0].text)
                self.assertFalse(response.is_error)
                self.assertTrue(value["ok"])
                self.assertEqual(value["tick"], 25)
                self.assertEqual(value["name"], item)
                self.assertEqual(value["drop_position"], DROP)
                self.assertNotIn("pickup_position", value)
                bridge.call.assert_called_with(
                    "place_entity", {**PLACEMENT, "item": item, "direction": 0}
                )

    async def test_mixed_batch_preserves_drill_drops_and_omits_missing_fields(self):
        bridge = Mock()
        measured = [
            {"name": "burner-mining-drill", "drop_position": copy.deepcopy(DROP)},
            {
                "name": "electric-mining-drill",
                "drop_position": {"x": -3.5, "y": 8.25},
                "electric_network_connected": True,
                "electric_network_id": 42,
            },
            {"name": "stone-furnace"},
        ]
        bridge.call.return_value = {
            "result": {
                "results": [
                    {"index": i, "ok": True, "result": copy.deepcopy(result)}
                    for i, result in enumerate(measured, 1)
                ]
                + [{"index": 4, "ok": False, "error": "blocked"}],
                "succeeded": 3,
                "failed": 1,
            }
        }
        items = [
            {**PLACEMENT, "item": item}
            for item in (
                "burner-mining-drill",
                "electric-mining-drill",
                "stone-furnace",
                "burner-mining-drill",
            )
        ]
        response = await create_server(bridge).call_tool("place_entities", {"items": items})
        value = json.loads(response.content[0].text)
        self.assertFalse(response.is_error)
        self.assertTrue(value["ok"])
        self.assertEqual((value["succeeded"], value["failed"]), (3, 1))
        self.assertEqual([entry["result"] for entry in value["results"][:3]], measured)
        failed = value["results"][3]
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error"], "blocked")
        self.assertNotIn("drop_position", failed)
        bridge.call.assert_called_with(
            "place_entities", {"items": [{**item, "direction": 0} for item in items]}
        )


class DrillDropNormalizationTests(unittest.TestCase):
    def test_coordinates_survive_normalization_and_are_not_synthesized(self):
        for operation in ("check_placement", "place_entity", "get_entity_inventory"):
            with self.subTest(operation=operation):
                result = {"drop_position": copy.deepcopy(DROP)}
                self.assertEqual(_normalize_result(operation, result), {"drop_position": DROP})
                self.assertNotIn("drop_position", _normalize_result(operation, {}))
        cached = {"original_response": {"result": {"drop_position": copy.deepcopy(DROP)}}}
        self.assertEqual(_normalize_result("get_request_result", copy.deepcopy(cached)), cached)
