import copy
import json
import unittest
from unittest.mock import Mock, call, patch

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import _normalize_result, create_server

FEEDER = {
    "name": "inserter",
    "x": 1.5,
    "y": 2.5,
    "status": "no_power",
    "pickup_target": "wooden-chest",
}


def payload(result):
    return json.loads(result.content[0].text)


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def wait_with_clock(self, diagnostics, *, timeout=1, status=None, reached_at=None):
        elapsed = 0.0

        async def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        def read(operation, params):
            self.assertEqual(params, {"target_ref": "target"})
            if operation == "get_entity_inventory":
                reached = reached_at is not None and elapsed >= reached_at
                value = {
                    "name": "wooden-chest" if status is None else "assembling-machine-1",
                    "inventories": {
                        "main": [{"name": "iron-plate", "count": 1}] if reached else {}
                    },
                }
                if status is not None:
                    value["status"] = status
                return {"result": value}
            self.assertEqual(operation, "entity_feeders")
            if isinstance(diagnostics, Exception):
                raise diagnostics
            return {"result": copy.deepcopy(diagnostics)}

        bridge = Mock()
        bridge.call.side_effect = read
        with (
            patch("factorio_mcp.server.time") as clock,
            patch("factorio_mcp.server.asyncio.sleep", side_effect=advance),
        ):
            clock.monotonic.side_effect = lambda: elapsed
            result = await create_server(bridge).call_tool(
                "wait_for_items",
                {
                    "target_ref": "target",
                    "item": "iron-plate",
                    "count": 1,
                    "timeout_seconds": timeout,
                },
            )
        self.assertFalse(result.is_error)
        return payload(result), bridge

    async def test_timed_out_and_stalled_waits_get_feeders_once_after_last_read(self):
        for status, timeout, stalled in ((None, 1, False), ("no_ingredients", 20, True)):
            with self.subTest(status=status):
                value, bridge = await self.wait_with_clock(
                    {"feeders": [FEEDER]}, status=status, timeout=timeout
                )
                self.assertFalse(value["reached"])
                self.assertEqual(value["timed_out"], not stalled)
                self.assertEqual(value["stalled"], stalled)
                self.assertEqual(value["current"], 0)
                self.assertEqual(value["waited_seconds"], 10 if stalled else timeout)
                if stalled:
                    self.assertEqual(value["stalled_seconds"], 10)
                    self.assertIn("materials", value["hint"])
                self.assertEqual(value["feeders"], [FEEDER])
                diagnostic_call = call("entity_feeders", {"target_ref": "target"})
                self.assertEqual(bridge.call.call_args_list[-1], diagnostic_call)
                self.assertEqual(bridge.call.call_args_list.count(diagnostic_call), 1)
                self.assertTrue(
                    all(
                        entry == call("get_entity_inventory", {"target_ref": "target"})
                        for entry in bridge.call.call_args_list[:-1]
                    )
                )

    async def test_empty_lua_feeders_become_lists(self):
        for feeders in ({}, [], None, "invalid"):
            with self.subTest(feeders=feeders):
                value, _ = await self.wait_with_clock({"feeders": feeders})
                self.assertEqual(value["feeders"], [])
        value, _ = await self.wait_with_clock({})
        self.assertEqual(value["feeders"], [])

    async def test_reached_waits_never_request_or_include_feeders(self):
        for reached_at in (0, 0.5, 1):
            with self.subTest(reached_at=reached_at):
                value, bridge = await self.wait_with_clock(
                    RuntimeError("must not diagnose"), reached_at=reached_at
                )
                self.assertTrue(value["reached"])
                self.assertFalse(value["timed_out"])
                self.assertFalse(value["stalled"])
                self.assertNotIn("feeders", value)
                self.assertTrue(
                    all(
                        entry.args[0] == "get_entity_inventory"
                        for entry in bridge.call.call_args_list
                    )
                )

    async def test_diagnostic_failures_preserve_timeout_and_stall_without_feeders(self):
        for error in (BridgeError("out_of_range", "Moved away"), RuntimeError("Bad response")):
            for status, timeout in ((None, 1), ("no_power", 20)):
                with self.subTest(error=error, status=status):
                    expected, _ = await self.wait_with_clock(
                        {"feeders": []}, status=status, timeout=timeout
                    )
                    value, bridge = await self.wait_with_clock(
                        error, status=status, timeout=timeout
                    )
                    expected.pop("feeders")
                    self.assertEqual(value, expected)
                    self.assertEqual(
                        bridge.call.call_args_list[-1],
                        call("entity_feeders", {"target_ref": "target"}),
                    )

    async def test_out_of_range_character_position_survives_public_error(self):
        detail = {
            "distance": 12.5,
            "reach": 10,
            "character_position": {"x": -2.25, "y": 3.75},
        }
        bridge = Mock()
        bridge.call.side_effect = BridgeError("out_of_range", "Too far", detail)
        result = await create_server(bridge).call_tool(
            "get_entity_inventory", {"target_ref": "target"}
        )
        self.assertTrue(result.is_error)
        self.assertEqual(payload(result)["detail"], detail)

    async def test_descriptions_and_internal_only_operation(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        self.assertNotIn("entity_feeders", tools)
        for phrase in (
            "reached=false (timed out or stalled)",
            "feeders",
            "16 owned inserters",
            "drop_target",
            "sorted by x then y",
            "name, x, y, status",
            "pickup_target",
            "omitted when none",
            "empty list",
            "omitted when reached=true",
            "diagnostic failures preserve the wait result",
        ):
            self.assertIn(phrase, tools["wait_for_items"].description)
        for phrase in ("10 tiles in a circle", "near the middle", "reliably places about 19 tiles"):
            self.assertIn(phrase, tools["place_line"].description)
        self.assertNotIn("about 21", tools["place_line"].description)

    def test_feeder_normalization_preserves_optional_fields_and_order(self):
        feeders = [FEEDER, {"name": "long-handed-inserter", "x": 3, "y": 1, "status": "no_power"}]
        self.assertEqual(
            _normalize_result("entity_feeders", {"feeders": copy.deepcopy(feeders)}),
            {"feeders": feeders},
        )


if __name__ == "__main__":
    unittest.main()
