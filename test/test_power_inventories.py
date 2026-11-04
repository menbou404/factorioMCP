import copy
import json
import unittest
from unittest.mock import Mock, patch

from mcp import Client

from factorio_mcp.server import HINTS, OPERATION_HINTS, _normalize_result, create_server


def payload(response):
    return json.loads(response.content[0].text)


class PowerAndInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_schema_and_description(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        tool = tools["get_entity_inventories"]
        self.assertEqual(tool.input_schema["required"], ["target_refs"])
        refs = tool.input_schema["properties"]["target_refs"]
        self.assertEqual((refs["type"], refs["minItems"], refs["maxItems"]), ("array", 1, 10))
        self.assertEqual(
            refs["items"],
            {
                key: value
                for key, value in tools["get_entity_inventory"]
                .input_schema["properties"]["target_ref"]
                .items()
                if key != "title"
            },
        )
        for text in (
            "1-10",
            "in order in one tick",
            "without walking",
            "Read-only",
            "get_entity_inventory",
            "owned",
            "same surface",
            "within reach",
            "60 seconds",
            "Failures do not stop later entries",
            "1-based index",
            "error/detail/hint",
            "succeeded and failed",
            "ok: true",
            "even if all entries failed",
        ):
            self.assertIn(text, tool.description)

    async def test_inventory_forwarding_preserves_order_duplicates_and_bounds(self):
        bridge = Mock()
        bridge.call.return_value = {
            "tick": 42,
            "result": {"results": {}, "succeeded": 0, "failed": 0},
        }
        server = create_server(bridge)
        for refs in (["42"], ["42", "invalid", "42"], [str(i) for i in range(10)]):
            with self.subTest(refs=refs):
                response = await server.call_tool("get_entity_inventories", {"target_refs": refs})
                self.assertFalse(response.is_error)
                self.assertEqual(payload(response)["results"], [])
                bridge.call.assert_called_once_with("get_entity_inventories", {"target_refs": refs})
                bridge.reset_mock()

    async def test_inventory_validation_reports_dotted_paths_before_forwarding(self):
        cases = [
            ({}, {"target_refs": "missing"}),
            ({"target_refs": None}, {"target_refs": "list_type"}),
            ({"target_refs": {}}, {"target_refs": "list_type"}),
            ({"target_refs": "42"}, {"target_refs": "list_type"}),
            ({"target_refs": []}, {"target_refs": "too_short"}),
            ({"target_refs": ["42"] * 11}, {"target_refs": "too_long"}),
            ({"target_refs": ["42", ""]}, {"target_refs.1": "string_too_short"}),
        ]
        for invalid in (1, False, None, {}, []):
            cases.append(({"target_refs": ["42", invalid]}, {"target_refs.1": "string_type"}))
        for mode in ("auto", "legacy"):
            bridge = Mock()
            async with Client(create_server(bridge), mode=mode) as client:
                for arguments, expected in cases:
                    with self.subTest(mode=mode, arguments=arguments):
                        response = await client.call_tool("get_entity_inventories", arguments)
                        self.assertTrue(response.is_error)
                        value = payload(response)
                        self.assertFalse(value["ok"])
                        self.assertEqual(value["error"], "invalid_input")
                        self.assertEqual(value["hint"], HINTS["invalid_input"])
                        self.assertEqual(
                            {e["field"]: e["type"] for e in value["detail"]["errors"]}, expected
                        )
            bridge.call.assert_not_called()

    async def test_batch_normalizes_inventory_results_and_uses_single_operation_hints(self):
        raw = {
            "operation": "get_entity_inventory",
            "results": [
                {"index": 1, "ok": True, "result": {"name": "lab", "inventories": {"input": {}}}},
                {
                    "index": 2,
                    "ok": False,
                    "error": "invalid_target",
                    "detail": {"supported_entities": {}, "valid_roles": {}, "role_aliases": []},
                },
                {"index": 3, "ok": False, "error": "out_of_range", "detail": {"reach": 10}},
                {
                    "index": 4,
                    "ok": True,
                    "result": {"inventories": {"fuel": [{"name": "coal", "count": 2}]}},
                },
            ],
            "succeeded": 2,
            "failed": 2,
        }
        bridge = Mock()
        bridge.call.return_value = {"tick": 42, "result": copy.deepcopy(raw)}
        with patch.dict(
            OPERATION_HINTS, {("get_entity_inventory", "out_of_range"): "inventory reach hint"}
        ):
            response = await create_server(bridge).call_tool(
                "get_entity_inventories", {"target_refs": ["a", "b", "c", "d"]}
            )
        self.assertFalse(response.is_error)
        value = payload(response)
        self.assertEqual((value["succeeded"], value["failed"]), (2, 2))
        entries = value["results"]
        self.assertEqual(
            entries[0]["result"],
            _normalize_result("get_entity_inventory", raw["results"][0]["result"]),
        )
        self.assertEqual(
            entries[1]["detail"], {"supported_entities": [], "valid_roles": [], "role_aliases": {}}
        )
        self.assertEqual(
            entries[1]["hint"], OPERATION_HINTS["get_entity_inventory", "invalid_target"]
        )
        self.assertEqual(entries[2]["hint"], "inventory reach hint")
        self.assertEqual(entries[3], raw["results"][3])

    async def test_all_failed_inventory_batch_still_succeeds(self):
        bridge = Mock()
        bridge.call.return_value = {
            "result": {
                "results": [{"index": 1, "ok": False, "error": "target_gone"}],
                "succeeded": 0,
                "failed": 1,
            }
        }
        response = await create_server(bridge).call_tool(
            "get_entity_inventories", {"target_refs": ["bad"]}
        )
        self.assertFalse(response.is_error)
        value = payload(response)
        self.assertTrue(value["ok"])
        self.assertEqual((value["succeeded"], value["failed"]), (0, 1))
        self.assertEqual(value["results"][0]["hint"], HINTS["target_gone"])

    async def test_coverage_forwarding_and_normalization_for_placeable_and_blocked(self):
        params = {"item": "inserter", "surface": "nauvis", "x": 1, "y": 2, "direction": 4}
        for field in ("covered", "connects"):
            for placeable in (True, False):
                for poles in ({}, [], [{"x": 0.5, "y": 0.5, "electric_network_id": 7}]):
                    with self.subTest(field=field, placeable=placeable, poles=poles):
                        bridge = Mock()
                        bridge.call.return_value = {
                            "result": {
                                "placeable": placeable,
                                "electric_coverage": {field: bool(poles), "poles": poles},
                            }
                        }
                        response = await create_server(bridge).call_tool("check_placement", params)
                        self.assertFalse(response.is_error)
                        self.assertEqual(
                            payload(response)["electric_coverage"],
                            {field: bool(poles), "poles": poles or []},
                        )
                        bridge.call.assert_called_once_with("check_placement", params)
        self.assertNotIn(
            "electric_coverage", _normalize_result("check_placement", {"placeable": True})
        )

    async def test_coverage_description_explains_geometry_and_limits(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for text in (
            "electric_coverage.covered",
            "bounding box overlaps",
            "square supply area",
            "electric_coverage.connects",
            "smaller of both poles",
            "placeable and blocked",
            "up to 3",
            "own-force",
            "same surface",
            "nearest first",
            "electric_network_id",
            "does not guarantee power",
        ):
            self.assertIn(text, tools["check_placement"].description)


if __name__ == "__main__":
    unittest.main()
