import copy
import json
import unittest
from unittest.mock import Mock

from mcp import Client

from factorio_mcp.server import _normalize_result, create_server


def payload(response):
    return json.loads(response.content[0].text)


class StatusCompactTests(unittest.IsolatedAsyncioTestCase):
    async def test_descriptions_explain_status_only_reads_and_compact_observation(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for operation in ("get_entity_inventory", "get_entity_inventories"):
            with self.subTest(operation=operation):
                for text in (
                    "inserter and mining-drill types without inventory roles",
                    "interact reach",
                    "empty inventories",
                    "other unsupported entities return invalid_target",
                    "pickup_position",
                    "drop_position",
                    "held_stack = {name, quality, count}",
                    "only when holding an item",
                    "mining_target",
                    "resource name",
                    "direction",
                    "status",
                    "do not extend transfer_item support",
                ):
                    self.assertIn(text, tools[operation].description)
        description = tools["observe_nearby"].description
        for text in (
            "compact=true when inspecting many belts or machines",
            "omits bounding boxes",
            "other per-entity details",
            "target_ref stays usable",
            "Counts, paging, resources and blocked_tiles are unchanged",
            "compact defaults to false",
        ):
            self.assertIn(text, description)

    async def test_compact_schema_and_only_true_is_forwarded(self):
        bridge = Mock()
        bridge.call.return_value = {"result": {}}
        server = create_server(bridge)
        tool = next(t for t in await server.list_tools() if t.name == "observe_nearby")
        prop = tool.input_schema["properties"]["compact"]
        self.assertEqual(prop["type"], "boolean")
        self.assertIs(prop["default"], False)
        self.assertNotIn("compact", tool.input_schema.get("required", []))
        for arguments in ({}, {"compact": False}, {"compact": True}):
            with self.subTest(arguments=arguments):
                response = await server.call_tool("observe_nearby", arguments)
                self.assertFalse(response.is_error)
                expected = {"entity_offset": 0, "tile_offset": 0}
                if arguments.get("compact"):
                    expected["compact"] = True
                bridge.call.assert_called_with("observe_nearby", expected)
        arguments = {
            "entity_offset": 100,
            "tile_offset": 256,
            "radius": 12,
            "types": ["inserter", "mining-drill"],
            "names": ["inserter"],
            "owned_only": True,
            "compact": True,
            "include_resources": False,
            "include_tiles": False,
        }
        await server.call_tool("observe_nearby", arguments)
        bridge.call.assert_called_with("observe_nearby", arguments)

    async def test_compact_rejects_non_booleans_before_bridge(self):
        for mode in ("auto", "legacy"):
            bridge = Mock()
            async with Client(create_server(bridge), mode=mode) as client:
                for compact in (0, 1, -1, 0.0, 1.0, "true", "false", None, [], {}):
                    with self.subTest(mode=mode, compact=compact):
                        response = await client.call_tool("observe_nearby", {"compact": compact})
                        self.assertTrue(response.is_error)
                        value = payload(response)
                        self.assertFalse(value["ok"])
                        self.assertEqual(value["error"], "invalid_input")
                        self.assertEqual(value["detail"]["errors"][0]["field"], "compact")
            bridge.call.assert_not_called()

    async def test_inventory_fields_survive_single_and_batch_normalization(self):
        bridge = Mock()
        server = create_server(bridge)
        machines = [
            {
                "name": "electric-mining-drill",
                "type": "mining-drill",
                "direction": 4,
                "inventories": {},
                "status": "working",
                "drop_position": {"x": 3.5, "y": 1},
                "mining_target": "iron-ore",
            },
            {
                "name": "inserter",
                "type": "inserter",
                "direction": 0,
                "inventories": {},
                "status": "waiting_for_source_items",
                "pickup_position": {"x": 0.5, "y": -0.5},
                "drop_position": {"x": 0.5, "y": 1.5},
            },
            {
                "name": "burner-inserter",
                "type": "inserter",
                "direction": 8,
                "inventories": {"fuel": {}},
                "status": "no_fuel",
                "pickup_position": {"x": 2.5, "y": 1.5},
                "drop_position": {"x": 2.5, "y": -0.5},
                "held_stack": {"name": "iron-plate", "quality": "normal", "count": 1},
            },
            {
                "name": "burner-mining-drill",
                "type": "mining-drill",
                "direction": 12,
                "inventories": {"fuel": {}},
                "status": "no_minable_resources",
                "drop_position": {"x": 5, "y": 3},
            },
        ]
        expected = []
        for machine in machines:
            with self.subTest(name=machine["name"]):
                bridge.call.return_value = {"tick": 42, "result": copy.deepcopy(machine)}
                response = await server.call_tool("get_entity_inventory", {"target_ref": "a"})
                self.assertFalse(response.is_error)
                normalized = {
                    **machine,
                    "inventories": {
                        role: items or [] for role, items in machine["inventories"].items()
                    },
                }
                self.assertEqual(payload(response), {"ok": True, "tick": 42, **normalized})
                expected.append(normalized)
                bridge.call.assert_called_with("get_entity_inventory", {"target_ref": "a"})
        bridge.call.return_value = {
            "tick": 43,
            "result": {
                "operation": "get_entity_inventory",
                "results": [
                    {"index": index, "ok": True, "result": copy.deepcopy(machine)}
                    for index, machine in enumerate(machines, 1)
                ],
                "succeeded": 4,
                "failed": 0,
            },
        }
        response = await server.call_tool("get_entity_inventories", {"target_refs": ["a"] * 4})
        self.assertFalse(response.is_error)
        self.assertEqual([e["result"] for e in payload(response)["results"]], expected)
        bridge.call.assert_called_with("get_entity_inventories", {"target_refs": ["a"] * 4})

    async def test_compact_result_is_forwarded_without_synthesizing_omitted_fields(self):
        measured = {
            "filters": {"compact": True},
            "entities": [
                {
                    "target_ref": "belt",
                    "name": "transport-belt",
                    "x": 0.5,
                    "y": 0.5,
                    "direction": 4,
                },
                {"target_ref": "drill", "name": "electric-mining-drill", "x": 3.5, "y": 0.5},
            ],
            "entity_total": 102,
            "next_entity_offset": 100,
            "resources": [{"name": "iron-ore", "tiles": 1, "total_amount": 500}],
            "blocked_tiles": [{"x": 1, "y": 1, "name": "water"}],
        }
        bridge = Mock()
        bridge.call.return_value = {"tick": 44, "result": copy.deepcopy(measured)}
        response = await create_server(bridge).call_tool("observe_nearby", {"compact": True})
        self.assertFalse(response.is_error)
        self.assertEqual(payload(response), {"ok": True, "tick": 44, **measured})
        self.assertEqual(
            _normalize_result("get_entity_inventory", {"inventories": {}}), {"inventories": {}}
        )


if __name__ == "__main__":
    unittest.main()
