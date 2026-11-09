import json
import unittest
from unittest.mock import Mock, call, patch

from mcp.server.mcpserver.exceptions import ToolError

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import ITEMS_STALLED_HINTS, create_server


def payload(result):
    return json.loads(result.content[0].text)


def inventory(current=0, status="working", *, name="assembling-machine-1", **roles):
    result = {
        "name": name,
        "inventories": {
            "source": {},  # Empty Lua arrays are serialized as objects.
            "result": [{"name": "iron-gear-wheel", "quality": "normal", "count": current}],
            **roles,
        },
    }
    if status is not None:
        result["status"] = status
    return result


class WaitForItemsTests(unittest.IsolatedAsyncioTestCase):
    async def wait_with_clock(self, inventory_at, **arguments):
        elapsed = 0.0

        async def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        bridge = Mock()
        bridge.call.side_effect = lambda operation, _: {
            "result": {"feeders": {}} if operation == "entity_feeders" else inventory_at(elapsed)
        }
        server = create_server(bridge)
        with (
            patch("factorio_mcp.server.time") as clock,
            patch("factorio_mcp.server.asyncio.sleep", side_effect=advance) as sleep,
        ):
            clock.monotonic.side_effect = lambda: elapsed
            result = await server.call_tool(
                "wait_for_items",
                {"target_ref": "assembler", "item": "iron-gear-wheel", "count": 100, **arguments},
            )
        reads = bridge.call.call_args_list.copy()
        if not result.is_error and not payload(result)["reached"]:
            self.assertEqual(reads.pop(), call("entity_feeders", {"target_ref": "assembler"}))
        for read in reads:
            self.assertEqual(read, call("get_entity_inventory", {"target_ref": "assembler"}))
        return result, elapsed, sleep

    async def test_already_reached_returns_immediately_and_sums_roles_and_qualities(self):
        result, elapsed, sleep = await self.wait_with_clock(
            lambda _: inventory(
                2,
                "full_output",
                source=[
                    {"name": "iron-gear-wheel", "quality": "uncommon", "count": 3},
                    {"name": "copper-plate", "quality": "normal", "count": 100},
                ],
            ),
            count=5,
        )
        self.assertFalse(result.is_error)
        self.assertEqual(
            payload(result),
            {
                "target_ref": "assembler",
                "item": "iron-gear-wheel",
                "count": 5,
                "current": 5,
                "reached": True,
                "status": "full_output",
                "stalled": False,
                "timed_out": False,
                "waited_seconds": 0,
            },
        )
        self.assertEqual(elapsed, 0)
        sleep.assert_not_awaited()

    async def test_poll_until_reached_including_at_deadline(self):
        for reached_at in (1, 2):
            with self.subTest(reached_at=reached_at):
                result, elapsed, sleep = await self.wait_with_clock(
                    lambda t, reached_at=reached_at: inventory(4 if t < reached_at else 6),
                    count=5,
                    timeout_seconds=2,
                )
                self.assertFalse(result.is_error)
                value = payload(result)
                self.assertTrue(value["reached"])
                self.assertFalse(value["stalled"])
                self.assertFalse(value["timed_out"])
                self.assertEqual(value["current"], 6)
                self.assertEqual(value["waited_seconds"], reached_at)
                self.assertEqual(elapsed, reached_at)
                self.assertEqual(sleep.await_count, reached_at * 2)
                sleep.assert_awaited_with(0.5)

    async def test_timeout_uses_remaining_sleep_budget(self):
        result, elapsed, sleep = await self.wait_with_clock(
            lambda _: inventory(4), timeout_seconds=1.25
        )
        self.assertFalse(result.is_error)
        value = payload(result)
        self.assertFalse(value["reached"])
        self.assertFalse(value["stalled"])
        self.assertTrue(value["timed_out"])
        self.assertEqual(value["current"], 4)
        self.assertEqual(value["waited_seconds"], 1.25)
        self.assertEqual(elapsed, 1.25)
        self.assertNotIn("stalled_seconds", value)
        self.assertNotIn("hint", value)
        sleep.assert_awaited_with(0.25)

    async def test_full_output_stalls_after_ten_unchanged_seconds(self):
        result, elapsed, sleep = await self.wait_with_clock(lambda _: inventory(4, "full_output"))
        self.assertFalse(result.is_error)
        value = payload(result)
        self.assertFalse(value["reached"])
        self.assertTrue(value["stalled"])
        self.assertFalse(value["timed_out"])
        self.assertEqual(value["current"], 4)
        self.assertEqual(value["status"], "full_output")
        self.assertEqual(value["stalled_seconds"], 10)
        self.assertEqual(value["waited_seconds"], 10)
        self.assertEqual(elapsed, 10)
        self.assertEqual(sleep.await_count, 20)
        self.assertIn("Take products out", value["hint"])
        self.assertIn("output inserter", value["hint"])

    async def test_shortages_have_status_specific_hints(self):
        for status, hint_text in (
            ("no_ingredients", "materials"),
            ("item_ingredient_shortage", "materials"),
            ("fluid_ingredient_shortage", "fluids"),
            ("no_fuel", "fuel"),
            ("no_power", "power"),
        ):
            with self.subTest(status=status):
                result, elapsed, _ = await self.wait_with_clock(
                    lambda _, status=status: inventory(0, status)
                )
                self.assertFalse(result.is_error)
                self.assertTrue(payload(result)["stalled"])
                self.assertEqual(payload(result)["status"], status)
                self.assertEqual(elapsed, 10)
                self.assertIn(hint_text, payload(result)["hint"])

    async def test_changing_count_does_not_stall(self):
        # Changes in either direction count, including items removed by inserters.
        result, elapsed, _ = await self.wait_with_clock(
            lambda t: inventory(4 if int(t / 8) % 2 else 5, "full_output")
        )
        self.assertFalse(result.is_error)
        self.assertFalse(payload(result)["stalled"])
        self.assertTrue(payload(result)["timed_out"])
        self.assertEqual(elapsed, 20)

    async def test_count_change_resets_stall_timer(self):
        result, elapsed, _ = await self.wait_with_clock(
            lambda t: inventory(4 if t < 8 else 5, "full_output")
        )
        self.assertTrue(payload(result)["stalled"])
        self.assertEqual(payload(result)["stalled_seconds"], 10)
        self.assertEqual(elapsed, 18)

    async def test_working_time_does_not_count_towards_stall(self):
        result, elapsed, _ = await self.wait_with_clock(
            lambda t: inventory(4, "working" if 4 <= t < 8 else "full_output")
        )
        self.assertTrue(payload(result)["stalled"])
        self.assertEqual(payload(result)["stalled_seconds"], 10)
        self.assertEqual(elapsed, 18)

    async def test_no_status_containers_and_active_statuses_never_stall(self):
        for status in (None, "normal", "working", "low_power"):
            with self.subTest(status=status):
                result, elapsed, _ = await self.wait_with_clock(
                    lambda _, status=status: {
                        "name": "wooden-chest" if status is None else "assembling-machine-1",
                        "inventories": {"main": {}},
                        **({"status": status} if status is not None else {}),
                    }
                )
                self.assertFalse(result.is_error)
                self.assertFalse(payload(result)["stalled"])
                self.assertTrue(payload(result)["timed_out"])
                self.assertEqual(elapsed, 20)
                if status is None:
                    self.assertNotIn("status", payload(result))

    async def test_role_filter_sums_all_qualities_and_accepts_entity_aliases(self):
        for name, requested, canonical in (
            ("assembling-machine-1", "result", "result"),
            ("assembling-machine-1", "output", "result"),
            ("assembling-machine-1", "input", "source"),
            ("stone-furnace", "output", "result"),
            ("stone-furnace", "input", "source"),
            ("lab", "source", "input"),
            ("iron-chest", "main", "main"),
        ):
            with self.subTest(name=name, requested=requested):
                result, elapsed, _ = await self.wait_with_clock(
                    lambda _, name=name, canonical=canonical: {
                        "name": name,
                        "inventories": {
                            "other": [{"name": "iron-gear-wheel", "count": 100}],
                            canonical: [
                                {"name": "iron-gear-wheel", "count": 2, "quality": "normal"},
                                {"name": "iron-gear-wheel", "count": 1, "quality": "rare"},
                            ],
                        },
                    },
                    inventory_role=requested,
                    count=4,
                    timeout_seconds=1,
                )
                self.assertFalse(result.is_error)
                value = payload(result)
                self.assertEqual(value["current"], 3)
                self.assertEqual(value["inventory_role"], canonical)
                self.assertTrue(value["timed_out"])
                self.assertEqual(elapsed, 1)

    async def test_invalid_role_lists_entity_roles_even_when_count_reached(self):
        for role in ("fuel", "main", "bogus"):
            with self.subTest(role=role):
                result, elapsed, sleep = await self.wait_with_clock(
                    lambda _: inventory(100), inventory_role=role
                )
                self.assertTrue(result.is_error)
                value = payload(result)
                self.assertEqual(value["error"], "invalid_input")
                self.assertEqual(value["detail"]["valid_roles"], ["result", "source"])
                self.assertEqual(value["detail"]["inventory_role"], role)
                self.assertEqual(value["detail"]["role_aliases"]["output"], "result")
                self.assertIn("hint", value)
                self.assertEqual(elapsed, 0)
                sleep.assert_not_awaited()
        # Aliases are entity-specific: input/output do not apply to chests.
        result, _, _ = await self.wait_with_clock(
            lambda _: {"name": "iron-chest", "inventories": {"main": {}}},
            inventory_role="output",
        )
        self.assertEqual(payload(result)["detail"]["valid_roles"], ["main"])

    async def test_inventory_errors_pass_through_including_after_a_poll(self):
        for code in ("out_of_range", "unknown_target", "target_gone", "invalid_target"):
            for after_poll in (False, True):
                with self.subTest(code=code, after_poll=after_poll):
                    bridge = Mock()
                    error = BridgeError(code, "Inventory read failed", {"request_id": "req"})
                    server = create_server(bridge)
                    bridge.call.side_effect = error
                    expected = await server.call_tool("get_entity_inventory", {"target_ref": "a"})
                    bridge.call.side_effect = (
                        [{"result": inventory()}, error] if after_poll else error
                    )
                    with patch("factorio_mcp.server.asyncio.sleep"):
                        result = await server.call_tool(
                            "wait_for_items", {"target_ref": "a", "item": "iron", "count": 1}
                        )
                    self.assertTrue(result.is_error)
                    self.assertEqual(payload(result), payload(expected))
        result = await create_server(None).call_tool(
            "wait_for_items", {"target_ref": "a", "item": "iron", "count": 1}
        )
        self.assertEqual(payload(result)["error"], "not_configured")

    async def test_schema_bounds_and_description(self):
        bridge = Mock()
        bridge.call.return_value = {"result": inventory(10000)}
        server = create_server(bridge)
        tools = {tool.name: tool for tool in await server.list_tools()}
        tool = tools["wait_for_items"]
        schema = tool.input_schema
        self.assertEqual(schema["required"], ["target_ref", "item", "count"])
        properties = schema["properties"]
        self.assertEqual(properties["count"]["type"], "integer")
        self.assertEqual(properties["count"]["minimum"], 1)
        self.assertEqual(properties["count"]["maximum"], 10000)
        self.assertEqual(properties["timeout_seconds"]["minimum"], 1)
        self.assertEqual(properties["timeout_seconds"]["maximum"], 50)
        self.assertEqual(properties["timeout_seconds"]["default"], 20)
        self.assertIsNone(properties["inventory_role"]["default"])
        for text in (
            "get_entity_inventory",
            "Read-only",
            "within reach",
            "0.5 seconds",
            "qualities",
            "10 consecutive seconds",
            "valid_roles",
            "containers without status",
            *ITEMS_STALLED_HINTS,
        ):
            self.assertIn(text, tool.description)
        arguments = {"target_ref": "a", "item": "iron-gear-wheel", "count": 1}
        for count, timeout in ((1, 1), (10000, 50)):
            result = await server.call_tool(
                "wait_for_items", {**arguments, "count": count, "timeout_seconds": timeout}
            )
            self.assertTrue(payload(result)["reached"])
        invalid = (
            [("count", value) for value in (0, 10001, 1.5, "1", True, None)]
            + [("timeout_seconds", value) for value in (0, 50.1, float("inf"), float("nan"))]
            + [("target_ref", ""), ("item", ""), ("inventory_role", "")]
        )
        bridge.call.reset_mock()
        for field, value in invalid:
            with self.subTest(field=field, value=value), self.assertRaises(ToolError):
                await server.call_tool("wait_for_items", {**arguments, field: value})
        bridge.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
