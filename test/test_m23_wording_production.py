import copy
import json
import unittest
from unittest.mock import Mock

from factorio_mcp.server import INSERTER_DIRECTION_DESCRIPTION, _normalize_result, create_server


class WordingProductionTests(unittest.IsolatedAsyncioTestCase):
    async def test_inserter_descriptions_share_all_four_pickup_and_drop_sides(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for text in (
            "0 picks up from north and drops south",
            "4 picks up from east and drops west",
            "8 picks up from south and drops north",
            "12 picks up from west and drops east",
        ):
            self.assertIn(text, INSERTER_DIRECTION_DESCRIPTION)
        for name in ("place_entity", "place_entities", "check_placement", "rotate_entity"):
            with self.subTest(tool=name):
                self.assertIn(INSERTER_DIRECTION_DESCRIPTION, tools[name].description)

    async def test_inventory_descriptions_explain_baseline_and_omission(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name in ("get_entity_inventory", "get_entity_inventories"):
            with self.subTest(tool=name):
                for text in (
                    "products_since_recipe",
                    "crafts since the last recipe change",
                    "set_recipe",
                    "products_finished minus the recorded baseline",
                    "only when a baseline exists",
                    "recipe matches the current recipe",
                    "same recipe preserves the baseline",
                ):
                    self.assertIn(text, tools[name].description)

    async def test_single_and_batch_inventory_preserve_measured_counter_or_omit_it(self):
        bridge = Mock()
        server = create_server(bridge)
        for delta in (None, 0, 7):
            with self.subTest(delta=delta):
                measured = {
                    "name": "assembling-machine-1",
                    "recipe": "transport-belt",
                    "products_finished": 42,
                    "inventories": {"source": {}, "result": {}},
                }
                if delta is not None:
                    measured["products_since_recipe"] = delta
                expected = {**measured, "inventories": {"source": [], "result": []}}
                for operation, params, result in (
                    ("get_entity_inventory", {"target_ref": "machine"}, measured),
                    (
                        "get_entity_inventories",
                        {"target_refs": ["machine"]},
                        {
                            "results": [{"index": 1, "ok": True, "result": measured}],
                            "succeeded": 1,
                            "failed": 0,
                        },
                    ),
                ):
                    bridge.call.return_value = {"tick": 54, "result": copy.deepcopy(result)}
                    response = await server.call_tool(operation, params)
                    self.assertFalse(response.is_error)
                    value = json.loads(response.content[0].text)
                    self.assertTrue(value.pop("ok"))
                    self.assertEqual(value.pop("tick"), 54)
                    if operation == "get_entity_inventories":
                        self.assertEqual((value["succeeded"], value["failed"]), (1, 0))
                        value = value["results"][0]["result"]
                    self.assertEqual(value, expected)
                    bridge.call.assert_called_with(operation, params)

    async def test_cached_inventory_preserves_recipe_counter(self):
        for delta in (0, 7):
            with self.subTest(delta=delta):
                measured = {"products_finished": 42, "products_since_recipe": delta}
                cached = {"original_response": {"result": copy.deepcopy(measured)}}
                self.assertEqual(
                    _normalize_result("get_request_result", cached)["original_response"]["result"],
                    measured,
                )


if __name__ == "__main__":
    unittest.main()
