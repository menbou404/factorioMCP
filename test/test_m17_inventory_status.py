import copy
import json
import unittest
from unittest.mock import Mock

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import OPERATION_HINTS, _hint, create_server


def payload(response):
    return json.loads(response.content[0].text)


class InventoryStatusTests(unittest.IsolatedAsyncioTestCase):
    arguments = {
        "target_ref": "furnace",
        "direction": "to_entity",
        "inventory_role": "fuel",
        "item": "coal",
        "count": 5,
    }
    stacks = [{"name": "wood", "quality": "normal", "count": 1}]

    async def test_full_inventory_hint_is_specific_to_transfers(self):
        hint = OPERATION_HINTS["transfer_item", "inventory_full"]
        self.assertEqual(_hint("transfer_item", "inventory_full"), hint)
        self.assertIsNone(_hint("craft", "inventory_full"))
        for text in (
            "destination inventory",
            "inventory_role",
            "contents",
            "fuel/input slot holds a different item",
            "from_entity first",
            "another target",
            "character is full",
            "free inventory space",
        ):
            self.assertIn(text, hint)

    async def test_full_inventory_details_and_hints_in_single_batch_and_cached_errors(self):
        bridge = Mock()
        server = create_server(bridge)
        for destination in ("entity", "character"):
            for contents in ({}, [], None, self.stacks):
                with self.subTest(destination=destination, contents=contents):
                    detail = {
                        "item": "coal",
                        "requested": 5,
                        "destination": destination,
                        "contents": copy.deepcopy(contents),
                        "slots": 1 if destination == "entity" else 80,
                        "empty_slots": 0,
                        "insertable": 0,
                    }
                    if destination == "entity":
                        detail["inventory_role"] = "fuel"
                    expected = {**detail, "contents": contents or []}
                    arguments = {
                        **self.arguments,
                        "direction": "to_entity" if destination == "entity" else "from_entity",
                    }
                    bridge.call.side_effect = BridgeError(
                        "inventory_full", detail=copy.deepcopy(detail)
                    )
                    response = await server.call_tool("transfer_item", arguments)
                    self.assertTrue(response.is_error)
                    single = payload(response)
                    self.assertEqual(single["detail"], expected)
                    self.assertEqual(
                        single["hint"], OPERATION_HINTS["transfer_item", "inventory_full"]
                    )

                    bridge.call.side_effect = None
                    batch = {
                        "operation": "transfer_item",
                        "results": [
                            {
                                "index": 1,
                                "ok": False,
                                "error": "inventory_full",
                                "detail": copy.deepcopy(detail),
                            },
                            {"index": 2, "ok": True, "result": {"moved": 1}},
                        ],
                        "succeeded": 1,
                        "failed": 1,
                    }
                    bridge.call.return_value = {"result": copy.deepcopy(batch)}
                    response = await server.call_tool(
                        "transfer_items", {"items": [arguments, arguments]}
                    )
                    self.assertFalse(response.is_error)
                    entries = payload(response)["results"]
                    self.assertEqual(entries[0]["detail"], expected)
                    self.assertEqual(entries[0]["hint"], single["hint"])
                    self.assertEqual(entries[1], batch["results"][1])

                    bridge.call.return_value = {
                        "result": {"original_response": {"result": copy.deepcopy(batch)}}
                    }
                    cached = payload(
                        await server.call_tool("get_request_result", {"request_id": "full-batch"})
                    )
                    self.assertEqual(cached["original_response"]["result"]["results"], entries)

                    bridge.call.return_value = {
                        "result": {"original_response": {"ok": False, "detail": detail}}
                    }
                    cached = payload(
                        await server.call_tool("get_request_result", {"request_id": "full-single"})
                    )
                    self.assertEqual(cached["original_response"]["detail"], expected)

    async def test_transfer_descriptions_explain_destination_details(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name in ("transfer_item", "transfer_items"):
            with self.subTest(name=name):
                for text in (
                    "inventory_full detail describes the destination inventory",
                    "item, requested",
                    "entity or character",
                    "inventory_role (entity only)",
                    "up to 10 {name, quality, count}",
                    "sorted by name then quality",
                    "slots, empty_slots, and insertable",
                ):
                    self.assertIn(text, tools[name].description)

    async def test_machine_descriptions_explain_status_and_recipes(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name in ("get_entity_inventory", "get_entity_inventories", "observe_nearby"):
            with self.subTest(name=name):
                for text in (
                    "status",
                    "full_output means the result inventory is full and the machine stopped",
                    "transfer from result",
                    "add an output inserter",
                    "waiting_for_space_in_destination for inserters means their target is full",
                    "omitted when the entity has no status",
                ):
                    self.assertIn(text, tools[name].description)
        for name in ("get_entity_inventory", "get_entity_inventories"):
            self.assertIn("Assemblers and furnaces include recipe", tools[name].description)
            self.assertIn("idle furnaces use their previous recipe", tools[name].description)
            self.assertIn("Omitted when no recipe is available", tools[name].description)

    async def test_inventory_results_preserve_measured_status_recipe_and_omissions(self):
        bridge = Mock()
        server = create_server(bridge)
        machines = [
            {
                "name": "assembling-machine-1",
                "status": "full_output",
                "recipe": "logistic-science-pack",
                "inventories": {"source": {}, "result": self.stacks},
            },
            {
                "name": "stone-furnace",
                "status": "no_ingredients",
                "recipe": "iron-plate",
                "inventories": {"fuel": {}},
            },
            {"name": "lab", "status": "no_power", "inventories": {"input": {}}},
            {"name": "wooden-chest", "inventories": {"main": {}}},
        ]
        expected = []
        for machine in machines:
            bridge.call.return_value = {"tick": 42, "result": copy.deepcopy(machine)}
            result = payload(await server.call_tool("get_entity_inventory", {"target_ref": "a"}))
            normalized = {
                **machine,
                "inventories": {
                    role: items or [] for role, items in machine["inventories"].items()
                },
            }
            self.assertEqual(result, {"ok": True, "tick": 42, **normalized})
            expected.append(normalized)
        batch = {
            "operation": "get_entity_inventory",
            "results": [
                {"index": index, "ok": True, "result": machine}
                for index, machine in enumerate(machines, 1)
            ],
            "succeeded": len(machines),
            "failed": 0,
        }
        bridge.call.return_value = {"tick": 42, "result": copy.deepcopy(batch)}
        result = payload(
            await server.call_tool("get_entity_inventories", {"target_refs": ["a"] * len(machines)})
        )
        self.assertEqual([entry["result"] for entry in result["results"]], expected)
        bridge.call.return_value = {"result": {"original_response": {"result": batch}}}
        cached = payload(await server.call_tool("get_request_result", {"request_id": "machines"}))
        self.assertEqual(cached["original_response"]["result"]["results"], result["results"])


if __name__ == "__main__":
    unittest.main()
