import copy
import json
import unittest
from unittest.mock import Mock

from factorio_mcp.server import HINTS, _normalize_result, create_server


def payload(result):
    return json.loads(result.content[0].text)


PLACEMENT = {"item": "electric-mining-drill", "surface": "nauvis", "x": -35, "y": 17}
RESOURCES = [{"name": "coal", "tiles": 2}, {"name": "iron-ore", "tiles": 1}]


class PlacementInfoTests(unittest.IsolatedAsyncioTestCase):
    async def test_descriptions_explain_success_metadata_and_connection_hint(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name in ("check_placement", "place_entity", "place_entities"):
            description = tools[name].description
            for text in ("mining_area_resources", "name, tiles", "snapped mining area", "sorted"):
                self.assertIn(text, description)
        self.assertIn("including when placeable=true", tools["check_placement"].description)
        for name in ("place_entity", "place_entities"):
            description = tools[name].description
            for text in (
                "Successful mining-drill placements",
                "electric_network_connected (boolean)",
                "electric_network_id when connected",
                "created entity in the same tick",
                "network membership",
                "does not guarantee power",
                "non-electric entities omit",
                "hint",
                "check_placement",
                "electric_coverage",
                "adding a pole",
            ):
                self.assertIn(text, description)

    async def test_placeable_drill_preview_and_success_preserve_resource_counts(self):
        bridge = Mock()
        server = create_server(bridge)
        for operation in ("check_placement", "place_entity"):
            with self.subTest(operation=operation):
                measured = {"mining_area_resources": copy.deepcopy(RESOURCES)}
                if operation == "check_placement":
                    measured["placeable"] = True
                else:
                    measured.update(electric_network_connected=True, electric_network_id=72)
                bridge.call.return_value = {"tick": 54, "result": measured}
                response = await server.call_tool(operation, PLACEMENT)
                value = payload(response)
                self.assertFalse(response.is_error)
                self.assertTrue(value["ok"])
                self.assertEqual(value["tick"], 54)
                self.assertEqual(value["mining_area_resources"], RESOURCES)
                self.assertNotIn("reason_detail", value)
                self.assertNotIn("hint", value)
                if operation == "place_entity":
                    self.assertIs(value["electric_network_connected"], True)
                    self.assertEqual(value["electric_network_id"], 72)
                bridge.call.assert_called_with(operation, {**PLACEMENT, "direction": 0})

    async def test_disconnected_single_placement_succeeds_with_hint(self):
        bridge = Mock()
        bridge.call.return_value = {
            "tick": 55,
            "result": {
                "name": "electric-mining-drill",
                "consumed_item": "electric-mining-drill",
                "electric_network_connected": False,
                "mining_area_resources": RESOURCES,
            },
        }
        response = await create_server(bridge).call_tool("place_entity", PLACEMENT)
        value = payload(response)
        self.assertFalse(response.is_error)
        self.assertTrue(value["ok"])
        self.assertIs(value["electric_network_connected"], False)
        self.assertNotIn("electric_network_id", value)
        self.assertEqual(value["mining_area_resources"], RESOURCES)
        for text in ("check_placement", "electric_coverage", "add a pole"):
            self.assertIn(text, value["hint"])

    async def test_mixed_batch_hints_only_disconnected_success_and_keeps_failure(self):
        bridge = Mock()
        measured = [
            {"electric_network_connected": False, "mining_area_resources": {}},
            {"electric_network_connected": True, "electric_network_id": 72},
            {"name": "burner-mining-drill", "mining_area_resources": RESOURCES},
        ]
        bridge.call.return_value = {
            "result": {
                "results": [
                    {"index": i, "ok": True, "result": copy.deepcopy(value)}
                    for i, value in enumerate(measured, 1)
                ]
                + [
                    {
                        "index": 4,
                        "ok": False,
                        "error": "blocked",
                        "detail": {
                            "reason": "placement_rules",
                            "mining_area_resources": {},
                            "reason_detail": "no_resources_in_mining_area",
                        },
                    }
                ],
                "succeeded": 3,
                "failed": 1,
            }
        }
        response = await create_server(bridge).call_tool(
            "place_entities", {"items": [PLACEMENT] * 4}
        )
        value = payload(response)
        self.assertFalse(response.is_error)
        self.assertTrue(value["ok"])
        self.assertEqual((value["succeeded"], value["failed"]), (3, 1))
        disconnected, connected, burner, failed = value["results"]
        self.assertEqual(disconnected["result"]["mining_area_resources"], [])
        self.assertIs(disconnected["result"]["electric_network_connected"], False)
        self.assertNotIn("electric_network_id", disconnected["result"])
        for text in ("check_placement", "electric_coverage", "add a pole"):
            self.assertIn(text, disconnected["result"]["hint"])
        self.assertEqual(connected["result"], measured[1])
        self.assertEqual(burner["result"], measured[2])
        self.assertEqual(failed["hint"], HINTS["blocked"])
        self.assertEqual(failed["detail"]["mining_area_resources"], [])
        self.assertEqual(failed["detail"]["reason_detail"], "no_resources_in_mining_area")


class PlacementInfoNormalizationTests(unittest.TestCase):
    def test_resources_normalized_in_preview_single_batch_and_cached_results(self):
        for resources in ({}, [], RESOURCES):
            for connected in (True, False):
                with self.subTest(resources=resources, connected=connected):
                    measured = {
                        "consumed_item": "electric-mining-drill",
                        "mining_area_resources": copy.deepcopy(resources),
                        "electric_network_connected": connected,
                    }
                    if connected:
                        measured["electric_network_id"] = 72
                    preview = _normalize_result(
                        "check_placement", {"placeable": True, "mining_area_resources": resources}
                    )
                    self.assertEqual(preview["mining_area_resources"], resources or [])
                    single = _normalize_result("place_entity", copy.deepcopy(measured))
                    self.assertEqual(single["mining_area_resources"], resources or [])
                    self.assertIs(single["electric_network_connected"], connected)
                    batch = {
                        "operation": "place_entity",
                        "results": [{"index": 1, "ok": True, "result": copy.deepcopy(measured)}],
                        "succeeded": 1,
                        "failed": 0,
                    }
                    normalized = _normalize_result("place_entities", copy.deepcopy(batch))
                    self.assertEqual(normalized["results"][0]["result"], single)
                    for cached_result in (measured, batch):
                        cached = _normalize_result(
                            "get_request_result",
                            {
                                "original_response": {
                                    "ok": True,
                                    "result": copy.deepcopy(cached_result),
                                }
                            },
                        )["original_response"]["result"]
                        if cached_result is batch:
                            cached = cached["results"][0]["result"]
                        self.assertEqual(cached, single)

    def test_absent_metadata_is_not_invented_or_connection_values_coerced(self):
        for operation in ("check_placement", "place_entity"):
            self.assertEqual(_normalize_result(operation, {}), {})
        for connected in (True, None, 0, "false"):
            with self.subTest(connected=connected):
                measured = {"electric_network_connected": connected}
                self.assertEqual(_normalize_result("place_entity", measured.copy()), measured)
        measured = {"name": "burner-mining-drill", "mining_area_resources": RESOURCES}
        self.assertEqual(_normalize_result("place_entity", copy.deepcopy(measured)), measured)


if __name__ == "__main__":
    unittest.main()
