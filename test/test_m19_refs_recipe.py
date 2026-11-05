import copy
import json
import unittest
from unittest.mock import Mock

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import HINTS, OPERATION_HINTS, create_server


def payload(response):
    return json.loads(response.content[0].text)


class OwnedRefsRecipeTests(unittest.IsolatedAsyncioTestCase):
    place_arguments = {"item": "assembling-machine-1", "surface": "nauvis", "x": 2.5, "y": 3.5}
    recipe_arguments = {"target_ref": "35", "recipe": "iron-gear-wheel"}
    stacks = [
        {"name": "iron-plate", "quality": "normal", "count": 3},
        {"name": "iron-gear-wheel", "quality": "uncommon", "count": 1},
    ]

    async def test_descriptions_explain_ref_lifetime_and_placement_refs(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name in (
            "observe_nearby",
            "place_entity",
            "place_entities",
            "check_placement",
            "get_entity_inventories",
            "transfer_items",
        ):
            with self.subTest(name=name):
                for text in (
                    "Owned-structure target refs",
                    "your force, with a unit_number",
                    "stable across observations",
                    "do not expire while the entity exists",
                    "Other refs expire after 60 seconds",
                    "Reach, surface, and force checks still apply",
                ):
                    self.assertIn(text, tools[name].description)
        for name in ("place_entity", "place_entities"):
            self.assertIn("Returns target_ref for the placed entity", tools[name].description)
        for text in ("do not expire", "60 seconds", "reason=expired", "reason=entity_gone"):
            self.assertIn(text, HINTS["target_gone"])

    async def test_recipe_description_and_hints_explain_refunds_and_capacity(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        description = tools["set_recipe"].description
        for text in (
            "Inputs, outputs, and ingredients of an in-progress craft",
            "main inventory as returned_items ({name, quality, count})",
            "conservative capacity check",
            "empty unfiltered slots",
            "inventory_full",
            "detail.items",
            "detail.empty_slots",
            "without changing the recipe, contents, or craft",
            "spilled_items",
            "not_empty with detail.reason=modules",
            "same recipe is a no-op success",
        ):
            self.assertIn(text, description)
        for text in (
            "detail.items",
            "detail.empty_slots",
            "before changing anything",
            "returned_items",
        ):
            self.assertIn(text, OPERATION_HINTS["set_recipe", "inventory_full"])
        for text in ("detail.reason=modules", "remove modules", "returned_items"):
            self.assertIn(text, OPERATION_HINTS["set_recipe", "not_empty"])

    async def test_placement_refs_survive_single_batch_and_cached_results(self):
        bridge = Mock()
        server = create_server(bridge)
        placed = {
            "name": "assembling-machine-1",
            "target_ref": "35",
            "position": {"surface": "nauvis", "x": 2.5, "y": 3.5},
        }
        bridge.call.return_value = {"tick": 42, "result": copy.deepcopy(placed)}
        response = await server.call_tool("place_entity", self.place_arguments)
        self.assertFalse(response.is_error)
        self.assertEqual(payload(response), {"ok": True, "tick": 42, **placed})
        batch = {
            "operation": "place_entity",
            "results": [{"index": 1, "ok": True, "result": copy.deepcopy(placed)}],
            "succeeded": 1,
            "failed": 0,
        }
        bridge.call.return_value = {"result": copy.deepcopy(batch)}
        response = await server.call_tool("place_entities", {"items": [self.place_arguments]})
        self.assertFalse(response.is_error)
        self.assertEqual(payload(response)["results"], batch["results"])
        for result in (placed, batch):
            bridge.call.return_value = {"result": {"original_response": {"result": result}}}
            cached = payload(await server.call_tool("get_request_result", {"request_id": "place"}))
            self.assertEqual(cached["original_response"]["result"], result)

    async def test_recipe_item_fields_preserve_counts_qualities_and_omissions(self):
        bridge = Mock()
        server = create_server(bridge)
        for extra in (
            {},  # Same-recipe no-op or a change returning nothing.
            {"returned_items": self.stacks},
            {"returned_items": self.stacks, "spilled_items": self.stacks[:1]},
        ):
            with self.subTest(extra=extra):
                result = {"name": "assembling-machine-1", "recipe": "iron-gear-wheel", **extra}
                bridge.call.return_value = {"tick": 42, "result": copy.deepcopy(result)}
                response = await server.call_tool("set_recipe", self.recipe_arguments)
                self.assertFalse(response.is_error)
                self.assertEqual(payload(response), {"ok": True, "tick": 42, **result})
                bridge.call.assert_called_with("set_recipe", self.recipe_arguments)
                bridge.call.return_value = {
                    "result": {"original_response": {"result": copy.deepcopy(result)}}
                }
                cached = payload(
                    await server.call_tool("get_request_result", {"request_id": "recipe"})
                )
                self.assertEqual(cached["original_response"]["result"], result)

    async def test_empty_recipe_item_fields_are_arrays_in_live_and_cached_results(self):
        bridge = Mock()
        server = create_server(bridge)
        for empty in ({}, None, []):
            result = {"returned_items": empty, "spilled_items": empty}
            bridge.call.return_value = {"result": copy.deepcopy(result)}
            response = await server.call_tool("set_recipe", self.recipe_arguments)
            self.assertEqual(payload(response)["returned_items"], [])
            self.assertEqual(payload(response)["spilled_items"], [])
            bridge.call.return_value = {"result": {"original_response": {"result": result}}}
            cached = payload(await server.call_tool("get_request_result", {"request_id": "empty"}))
            self.assertEqual(
                cached["original_response"]["result"], {"returned_items": [], "spilled_items": []}
            )

    async def test_recipe_errors_preserve_details_and_use_specific_hints(self):
        bridge = Mock()
        server = create_server(bridge)
        cases = [
            ("not_empty", {"reason": "modules"}, {"reason": "modules"}),
            (
                "inventory_full",
                {"items": self.stacks, "empty_slots": 0},
                {"items": self.stacks, "empty_slots": 0},
            ),
            *[
                (
                    "inventory_full",
                    {"items": empty, "empty_slots": 0},
                    {"items": [], "empty_slots": 0},
                )
                for empty in ({}, None, [])
            ],
        ]
        for code, detail, expected in cases:
            with self.subTest(code=code, detail=detail):
                bridge.call.side_effect = BridgeError(code, detail=copy.deepcopy(detail))
                response = await server.call_tool("set_recipe", self.recipe_arguments)
                self.assertTrue(response.is_error)
                self.assertEqual(payload(response)["detail"], expected)
                self.assertEqual(payload(response)["hint"], OPERATION_HINTS["set_recipe", code])
                bridge.call.side_effect = None
                bridge.call.return_value = {
                    "result": {"original_response": {"ok": False, "error": code, "detail": detail}}
                }
                cached = payload(
                    await server.call_tool("get_request_result", {"request_id": "recipe-error"})
                )
                self.assertEqual(cached["original_response"]["detail"], expected)


if __name__ == "__main__":
    unittest.main()
