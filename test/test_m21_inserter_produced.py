import copy
import json
import unittest
from unittest.mock import Mock, call, patch

from mcp.server.mcpserver.exceptions import ToolError

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import _normalize_error_detail, _normalize_result, create_server


def payload(result):
    return json.loads(result.content[0].text)


def machine(finished=40, status="working", *, amount=1, **overrides):
    return {
        "name": "assembling-machine-1",
        "type": "assembling-machine",
        "recipe": "transport-belt",
        "recipe_products": ["transport-belt"],
        "recipe_product_amounts": {"transport-belt": amount},
        "products_finished": finished,
        "inventories": {"source": {}, "result": {}},
        "status": status,
        **overrides,
    }


class ProducedItemsTests(unittest.IsolatedAsyncioTestCase):
    async def wait_with_clock(self, inventory_at, **arguments):
        elapsed = 0.0

        async def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        def read(operation, _):
            if operation == "entity_feeders":
                return {"result": {"feeders": {}}}
            value = inventory_at(elapsed)
            if isinstance(value, Exception):
                raise value
            return {"result": value}

        bridge = Mock()
        bridge.call.side_effect = read
        with (
            patch("factorio_mcp.server.time") as clock,
            patch("factorio_mcp.server.asyncio.sleep", side_effect=advance) as sleep,
        ):
            clock.monotonic.side_effect = lambda: elapsed
            result = await create_server(bridge).call_tool(
                "wait_for_items",
                {
                    "target_ref": "machine",
                    "item": "transport-belt",
                    "count": 1,
                    "produced": True,
                    **arguments,
                },
            )
        reads = [
            entry
            for entry in bridge.call.call_args_list
            if entry.args[0] != "get_character_position"
        ]
        if not result.is_error and not payload(result)["reached"]:
            self.assertEqual(reads.pop(), call("entity_feeders", {"target_ref": "machine"}))
        for read in reads:
            self.assertEqual(read, call("get_entity_inventory", {"target_ref": "machine"}))
        return result, elapsed, sleep

    async def test_reached_uses_delta_even_when_output_stays_empty(self):
        result, elapsed, _ = await self.wait_with_clock(
            lambda t: machine(40 + int(t)), count=2, inventory_role="output"
        )
        self.assertFalse(result.is_error)
        self.assertEqual(
            payload(result),
            {
                "target_ref": "machine",
                "item": "transport-belt",
                "count": 2,
                "produced": True,
                "current": 2,
                "reached": True,
                "stalled": False,
                "timed_out": False,
                "waited_seconds": 2,
                "status": "working",
                "inventory_role": "result",
            },
        )
        self.assertEqual(elapsed, 2)

    async def test_multiple_items_per_craft_reached_at_deadline(self):
        result, elapsed, _ = await self.wait_with_clock(
            lambda t: machine(40 + int(t), amount=2), count=3, timeout_seconds=2
        )
        self.assertFalse(result.is_error)
        self.assertEqual(payload(result)["current"], 4)
        self.assertTrue(payload(result)["reached"])
        self.assertFalse(payload(result)["timed_out"])
        self.assertEqual(elapsed, 2)

    async def test_furnace_with_previous_recipe_and_multiple_products(self):
        result, _, _ = await self.wait_with_clock(
            lambda t: machine(
                8 + int(t),
                name="stone-furnace",
                type="furnace",
                recipe="iron-plate",
                recipe_products=["iron-plate", "stone"],
                recipe_product_amounts={"iron-plate": 1, "stone": 2},
            ),
            item="stone",
            count=2,
        )
        self.assertFalse(result.is_error)
        self.assertTrue(payload(result)["reached"])
        self.assertEqual(payload(result)["current"], 2)

    async def test_stalls_on_production_even_when_inventory_changes(self):
        for status, seconds, hint in (
            ("no_ingredients", 10, "materials"),
            ("full_output", 3, "output inserter"),
        ):
            with self.subTest(status=status):
                result, elapsed, _ = await self.wait_with_clock(
                    lambda t, status=status: machine(
                        status=status,
                        inventories={"result": [{"name": "transport-belt", "count": int(t) % 2}]},
                    )
                )
                value = payload(result)
                self.assertFalse(result.is_error)
                self.assertTrue(value["produced"])
                self.assertTrue(value["stalled"])
                self.assertFalse(value["reached"])
                self.assertFalse(value["timed_out"])
                self.assertEqual(value["current"], 0)
                self.assertEqual(value["stalled_seconds"], seconds)
                self.assertEqual(elapsed, seconds)
                self.assertIn(hint, value["hint"])

    async def test_transient_full_output_allows_production_to_reach_target(self):
        for inventory_at, count in (
            (lambda t: machine(40 + int(t / 2), "full_output"), 3),
            (
                lambda t: machine(
                    40 + (t >= 6), "full_output" if int(t / 2) % 2 == 0 else "working"
                ),
                1,
            ),
        ):
            with self.subTest(inventory_at=inventory_at):
                result, elapsed, _ = await self.wait_with_clock(inventory_at, count=count)
                value = payload(result)
                self.assertFalse(result.is_error)
                self.assertTrue(value["reached"])
                self.assertTrue(value["produced"])
                self.assertFalse(value["stalled"])
                self.assertFalse(value["timed_out"])
                self.assertNotIn("output_full", value)
                self.assertEqual(elapsed, 6)

    async def test_progress_and_working_status_reset_stall_timer(self):
        for inventory_at in (
            lambda t: machine(40 + (t >= 8), "no_ingredients"),
            lambda t: machine(40, "working" if 4 <= t < 8 else "no_power"),
        ):
            with self.subTest(inventory_at=inventory_at):
                result, elapsed, _ = await self.wait_with_clock(inventory_at, count=10)
                self.assertTrue(payload(result)["stalled"])
                self.assertEqual(payload(result)["stalled_seconds"], 10)
                self.assertEqual(elapsed, 18)

    async def test_working_timeout_does_not_count_old_products_or_stock(self):
        result, elapsed, sleep = await self.wait_with_clock(
            lambda _: machine(inventories={"result": [{"name": "transport-belt", "count": 100}]}),
            timeout_seconds=1.25,
        )
        value = payload(result)
        self.assertTrue(value["timed_out"])
        self.assertTrue(value["produced"])
        self.assertFalse(value["reached"])
        self.assertFalse(value["stalled"])
        self.assertEqual(value["current"], 0)
        self.assertEqual(elapsed, 1.25)
        sleep.assert_awaited_with(0.25)

    async def test_invalid_target_including_unsupported_inventory_entity(self):
        for inventory in (
            {"name": "iron-chest", "type": "container", "inventories": {"main": {}}},
            BridgeError(
                "invalid_target",
                "Unsupported entity",
                {"entity": "transport-belt", "reason": "unsupported_entity"},
            ),
        ):
            with self.subTest(inventory=inventory):
                result, elapsed, sleep = await self.wait_with_clock(
                    lambda _, inventory=inventory: inventory
                )
                self.assertTrue(result.is_error)
                self.assertEqual(payload(result)["error"], "invalid_input")
                self.assertIn("assembling machine or furnace", payload(result)["message"])
                self.assertEqual(elapsed, 0)
                sleep.assert_not_awaited()

    async def test_invalid_item_and_unset_recipe_list_products(self):
        for products in (["iron-plate", "stone"], {}):
            with self.subTest(products=products):
                result, elapsed, sleep = await self.wait_with_clock(
                    lambda _, products=products: machine(recipe_products=products)
                )
                self.assertTrue(result.is_error)
                value = payload(result)
                self.assertEqual(value["error"], "invalid_input")
                self.assertEqual(value["detail"]["recipe_products"], products or [])
                self.assertEqual(value["detail"]["item"], "transport-belt")
                self.assertIn("product", value["message"])
                self.assertEqual(elapsed, 0)
                sleep.assert_not_awaited()

    async def test_invalid_role_is_still_validated(self):
        result, _, sleep = await self.wait_with_clock(lambda _: machine(), inventory_role="main")
        self.assertTrue(result.is_error)
        self.assertEqual(payload(result)["error"], "invalid_input")
        self.assertEqual(payload(result)["detail"]["valid_roles"], ["result", "source"])
        sleep.assert_not_awaited()

    async def test_reach_and_target_errors_pass_through_during_wait(self):
        for code in ("out_of_range", "target_gone", "unknown_target"):
            with self.subTest(code=code):
                result, elapsed, _ = await self.wait_with_clock(
                    lambda t, code=code: (
                        machine()
                        if t == 0
                        else BridgeError(code, "Inventory read failed", {"reason": "test"})
                    )
                )
                self.assertTrue(result.is_error)
                self.assertEqual(payload(result)["error"], code)
                self.assertEqual(payload(result)["detail"], {"reason": "test"})
                self.assertEqual(elapsed, 0.5)

    async def test_produced_false_preserves_inventory_mode(self):
        result, elapsed, sleep = await self.wait_with_clock(
            lambda _: {
                "name": "iron-chest",
                "inventories": {"main": [{"name": "transport-belt", "count": 2}]},
            },
            produced=False,
        )
        self.assertTrue(payload(result)["reached"])
        self.assertEqual(payload(result)["current"], 2)
        self.assertNotIn("produced", payload(result))
        self.assertEqual(elapsed, 0)
        sleep.assert_not_awaited()

    async def test_schema_and_descriptions(self):
        bridge = Mock()
        server = create_server(bridge)
        tools = {tool.name: tool for tool in await server.list_tools()}
        produced_schema = tools["wait_for_items"].input_schema["properties"]["produced"]
        self.assertEqual(produced_schema["type"], "boolean")
        self.assertIs(produced_schema["default"], False)
        for name in ("check_placement", "place_entity", "place_entities"):
            for field in ("pickup_position", "drop_position", "absolute map"):
                self.assertIn(field, tools[name].description)
        self.assertIn("even when not placeable", tools["check_placement"].description)
        for text in (
            "produced=true",
            "output inserter",
            "products_finished",
            "recipe_products",
            "within reach",
            "previous recipe",
            "amount",
            "probability",
        ):
            self.assertIn(text, tools["wait_for_items"].description)
        for name in ("get_entity_inventory", "get_entity_inventories"):
            for field in ("products_finished", "recipe_products", "recipe_product_amounts"):
                self.assertIn(field, tools[name].description)
        for invalid in ("true", "false", 1, 0, None):
            with self.subTest(invalid=invalid), self.assertRaises(ToolError):
                await server.call_tool(
                    "wait_for_items",
                    {"target_ref": "m", "item": "i", "count": 1, "produced": invalid},
                )
        bridge.call.assert_not_called()


class M21NormalizationTests(unittest.TestCase):
    def test_placement_coordinates_preserved_in_preview_single_batch_and_cache(self):
        positions = {
            "pickup_position": {"x": -3.5, "y": 5.5},
            "drop_position": {"x": -0.3, "y": 5.5},
        }
        for operation in ("check_placement", "place_entity"):
            for placeable in (True, False):
                value = {"placeable": placeable, **copy.deepcopy(positions)}
                expected = copy.deepcopy(value)
                self.assertEqual(_normalize_result(operation, value), expected)
                self.assertEqual(value["pickup_position"], positions["pickup_position"])
                self.assertEqual(value["drop_position"], positions["drop_position"])
            self.assertNotIn("pickup_position", _normalize_result(operation, {}))
        entry = {"index": 1, "ok": True, "result": copy.deepcopy(positions)}
        batch = {"results": [entry], "succeeded": 1, "failed": 0}
        self.assertEqual(
            _normalize_result("place_entities", batch)["results"][0]["result"], positions
        )
        cached = {"original_response": {"result": copy.deepcopy(positions)}}
        self.assertEqual(
            _normalize_result("get_request_result", cached)["original_response"]["result"],
            positions,
        )

    def test_recipe_products_normalized_without_changing_counter_or_amount_map(self):
        for products in ({}, [], ["transport-belt"]):
            with self.subTest(products=products):
                raw = machine(recipe_products=products)
                single = _normalize_result("get_entity_inventory", copy.deepcopy(raw))
                self.assertEqual(single["recipe_products"], products or [])
                self.assertEqual(single["products_finished"], 40)
                self.assertEqual(single["recipe_product_amounts"], {"transport-belt": 1})
                self.assertEqual(single["inventories"], {"source": [], "result": []})
                batch = _normalize_result(
                    "get_entity_inventories",
                    {"results": [{"index": 1, "ok": True, "result": copy.deepcopy(raw)}]},
                )
                self.assertEqual(batch["results"][0]["result"], single)
        self.assertNotIn("recipe_products", _normalize_result("get_entity_inventory", {}))
        self.assertEqual(_normalize_error_detail({"recipe_products": {}}), {"recipe_products": []})


if __name__ == "__main__":
    unittest.main()
