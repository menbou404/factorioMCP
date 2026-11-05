import copy
import json
import unittest
from unittest.mock import Mock

from factorio_mcp.bridge import BridgeError, parse_bridge_response
from factorio_mcp.server import _hint, create_server


def payload(response):
    return json.loads(response.content[0].text)


class TransferErrorTests(unittest.IsolatedAsyncioTestCase):
    arguments = {
        "target_ref": "assembler",
        "direction": "to_entity",
        "inventory_role": "source",
        "item": "inserter",
        "count": 1,
    }

    async def check_error_paths(self, code, detail, expected, arguments=None):
        arguments = arguments or self.arguments
        bridge = Mock()
        server = create_server(bridge)
        # The bridge accepts the new code without an allowlist or translation.
        raw = json.dumps(
            {
                "protocol_version": 1,
                "request_id": "rejected",
                "ok": False,
                "error": code,
                "detail": detail,
            }
        )
        with self.assertRaises(BridgeError) as raised:
            parse_bridge_response(raw, "rejected")
        self.assertEqual(raised.exception.code, code)
        bridge.call.side_effect = raised.exception
        response = await server.call_tool("transfer_item", arguments)
        self.assertTrue(response.is_error)
        single = payload(response)
        self.assertEqual(single["error"], code)
        self.assertEqual(single["detail"], expected)
        self.assertEqual(single["hint"], _hint("transfer_item", code))

        bridge.call.side_effect = None
        batch = {
            "operation": "transfer_item",
            "results": [
                {"index": 1, "ok": False, "error": code, "detail": copy.deepcopy(detail)},
                {"index": 2, "ok": True, "result": {"moved": 1, "requested": 1}},
            ],
            "succeeded": 1,
            "failed": 1,
        }
        bridge.call.return_value = {"result": copy.deepcopy(batch)}
        response = await server.call_tool("transfer_items", {"items": [arguments, arguments]})
        self.assertFalse(response.is_error)
        value = payload(response)
        self.assertEqual((value["succeeded"], value["failed"]), (1, 1))
        entries = value["results"]
        self.assertEqual(entries[0]["error"], code)
        self.assertEqual(entries[0]["detail"], expected)
        self.assertEqual(entries[0]["hint"], single["hint"])
        self.assertEqual(entries[1], batch["results"][1])

        for original in (
            {"ok": False, "error": code, "detail": copy.deepcopy(detail)},
            {"result": copy.deepcopy(batch)},
        ):
            bridge.call.return_value = {"result": {"original_response": original}}
            response = await server.call_tool("get_request_result", {"request_id": "cached"})
            cached = payload(response)["original_response"]
            if "result" in cached:
                self.assertEqual(cached["result"]["results"], entries)
            else:
                self.assertEqual(cached["error"], code)
                self.assertEqual(cached["detail"], expected)

    async def test_item_not_accepted_details_survive_single_batch_and_cached_errors(self):
        details = [
            {
                "item": "inserter",
                "entity": "assembling-machine-1",
                "inventory_role": "source",
                "recipe": "automation-science-pack",
                "ingredients": ["copper-plate", "iron-gear-wheel"],
                "insertable": 0,
            },
            {
                "item": "inserter",
                "entity": "assembling-machine-1",
                "inventory_role": "source",
                "reason": "no_recipe",
                "insertable": 0,
            },
            {
                "item": "wood",
                "entity": "stone-furnace",
                "inventory_role": "source",
                "reason": "no_smelting_recipe",
                "insertable": 0,
            },
            {
                "item": "inserter",
                "destination": "entity",
                "inventory_role": "source",
                "contents": {},
                "slots": 2,
                "empty_slots": 2,
                "insertable": 0,
            },
        ]
        for detail in details:
            with self.subTest(detail=detail):
                expected = copy.deepcopy(detail)
                if "contents" in expected:
                    expected["contents"] = []
                await self.check_error_paths("item_not_accepted", detail, expected)

    async def test_empty_ingredients_are_normalized_without_inventing_absent_fields(self):
        for ingredients in ({}, None, []):
            with self.subTest(ingredients=ingredients):
                detail = {"item": "wood", "recipe": "empty-recipe", "ingredients": ingredients}
                await self.check_error_paths(
                    "item_not_accepted", detail, {**detail, "ingredients": []}
                )

    async def test_empty_source_preserves_status_recipe_contents_and_omissions(self):
        details = [
            {"status": "no_fuel", "recipe": "iron-plate", "contents": {}},
            {"status": "no_power", "recipe": "automation-science-pack"},
            {"status": "item_ingredient_shortage", "recipe": "iron-gear-wheel"},
            {"status": "no_ingredients", "recipe": "iron-plate"},
            {"status": "no_recipe"},
            {"contents": [{"name": "copper-plate", "count": 1, "quality": "normal"}]},
            {},  # Non-machine sources need not have status or recipe.
        ]
        for extra in details:
            with self.subTest(extra=extra):
                detail = {
                    "item": "iron-plate",
                    "requested": 1,
                    "available": 0,
                    "inventory_role": "result",
                    **extra,
                }
                expected = copy.deepcopy(detail)
                if "contents" in expected and not isinstance(expected["contents"], list):
                    expected["contents"] = []
                await self.check_error_paths(
                    "missing_materials",
                    detail,
                    expected,
                    {**self.arguments, "direction": "from_entity", "inventory_role": "result"},
                )

    async def test_transfer_hints_and_descriptions_explain_acceptance_and_source_status(self):
        hint = _hint("transfer_item", "item_not_accepted")
        for text in ("assembler", "detail.ingredients", "set_recipe", "no_recipe", "smeltable"):
            self.assertIn(text, hint)
        shortage = _hint("transfer_item", "missing_materials")
        for text in (
            "detail.contents",
            "detail.status",
            "detail.recipe",
            "no_fuel",
            "no_power",
            "item_ingredient_shortage",
            "no_ingredients",
            "first, then wait with wait_for_items",
        ):
            self.assertIn(text, shortage)
        self.assertIn("missing_raw", _hint("craft", "missing_materials"))
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name in ("transfer_item", "transfer_items"):
            for text in (
                "item_not_accepted",
                "recipe ingredients",
                "detail.ingredients",
                "set_recipe",
                "reason=no_recipe",
                "reason=no_smelting_recipe",
                "smeltable",
                "insertable=0",
                "capacity estimate",
                "assembler filtered slots",
                "status when available",
                "recipe for assemblers/furnaces",
                "previous_recipe",
                "wait_for_items",
            ):
                with self.subTest(tool=name, text=text):
                    self.assertIn(text, tools[name].description)


if __name__ == "__main__":
    unittest.main()
