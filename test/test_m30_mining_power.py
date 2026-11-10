import copy
import json
import unittest
from unittest.mock import Mock

from factorio_mcp.server import (
    HINTS,
    ITEMS_STALLED_HINTS,
    TRANSFER_DESCRIPTION,
    _normalize_result,
    create_server,
)


class MiningPowerTests(unittest.IsolatedAsyncioTestCase):
    async def test_descriptions_and_power_hints(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for operation in ("get_entity_inventory", "get_entity_inventories"):
            for text in (
                "electric-pole",
                "interact reach",
                "electric_network",
                "production_kw",
                "consumption_kw",
                "last 5 seconds",
                "{name, kw}",
                "at most 10",
                "no network",
                "do not extend transfer_item support",
            ):
                self.assertIn(text, tools[operation].description)
        for operation in ("mine_entity", "wait_for_action"):
            self.assertIn("target's item products", tools[operation].description)
            self.assertIn("resource", tools[operation].description)
        for text in (
            ITEMS_STALLED_HINTS["no_power"],
            HINTS["missing_materials"],
            TRANSFER_DESCRIPTION,
            tools["get_entity_inventory"].description,
            tools["wait_for_items"].description,
        ):
            for term in (
                "nearby electric pole",
                "get_entity_inventory",
                "production and consumption",
            ):
                self.assertIn(term, text)
        # Reduced power can still make progress and must not become a stalled state.
        self.assertNotIn("low_power", ITEMS_STALLED_HINTS)

    async def test_network_passthrough_single_batch_and_cached_results(self):
        bridge = Mock()
        server = create_server(bridge)
        for entries in ({}, [], [{"name": "electric-energy-interface", "kw": 150.2}]):
            network = {
                "id": 73,
                "production_kw": 150.2,
                "consumption_kw": 100.1,
                "producers": copy.deepcopy(entries),
                "consumers": copy.deepcopy(entries),
            }
            measured = {
                "name": "small-electric-pole",
                "type": "electric-pole",
                "inventories": {},
                "electric_network": network,
            }
            expected = copy.deepcopy(measured)
            for field in ("producers", "consumers"):
                expected["electric_network"][field] = entries or []
            for operation, params, result in (
                ("get_entity_inventory", {"target_ref": "pole"}, measured),
                (
                    "get_entity_inventories",
                    {"target_refs": ["pole"]},
                    {"results": [{"index": 1, "ok": True, "result": measured}]},
                ),
            ):
                bridge.call.return_value = {"tick": 50, "result": copy.deepcopy(result)}
                response = await server.call_tool(operation, params)
                self.assertFalse(response.is_error)
                value = json.loads(response.content[0].text)
                actual = value["results"][0]["result"] if "results" in value else value
                if operation == "get_entity_inventory":
                    self.assertEqual(actual, {"ok": True, "tick": 50, **expected})
                else:
                    self.assertEqual(actual, expected)
                bridge.call.assert_called_with(operation, params)
            cached = _normalize_result(
                "get_request_result", {"original_response": {"result": copy.deepcopy(measured)}}
            )
            self.assertEqual(cached["original_response"]["result"], expected)

    async def test_disconnected_pole_does_not_gain_synthetic_network(self):
        measured = {"name": "small-electric-pole", "type": "electric-pole", "inventories": {}}
        self.assertEqual(
            _normalize_result("get_entity_inventory", copy.deepcopy(measured)), measured
        )


if __name__ == "__main__":
    unittest.main()
