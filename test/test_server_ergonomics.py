import asyncio
import json
import unittest
from unittest.mock import Mock, patch

from mcp.server.mcpserver.exceptions import ToolError

from factorio_mcp.bridge import BridgeError, parse_bridge_response
from factorio_mcp.server import HINTS, _normalize_result, create_server


def payload(result):
    return json.loads(result.content[0].text)


class ErrorDetailTests(unittest.TestCase):
    def test_parse_error_detail_is_separate_from_result(self):
        detail = {"entity": "stone-furnace", "valid_roles": ["fuel", "result", "source"]}
        raw = json.dumps(
            {
                "protocol_version": 1,
                "request_id": "test",
                "ok": False,
                "error": "invalid_target",
                "result": None,
                "detail": detail,
            }
        )
        with self.assertRaises(BridgeError) as caught:
            parse_bridge_response(raw, "test")
        self.assertEqual(caught.exception.code, "invalid_target")
        self.assertEqual(caught.exception.detail, detail)


class ErgonomicsTests(unittest.IsolatedAsyncioTestCase):
    async def test_placement_tool_descriptions_explain_preview_metadata(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name in ("check_placement", "place_entity", "observe_nearby"):
            with self.subTest(name=name):
                for field in ("snapped position", "fluid_connections", "valid_directions"):
                    self.assertIn(field, tools[name].description)
        self.assertIn("connected", tools["observe_nearby"].description)
        self.assertIn("blocked_reason", tools["check_placement"].description)

    async def test_blocked_placement_preserves_rules_and_direction_advice(self):
        bridge = Mock()
        detail = {"reason": "placement_rules", "valid_directions": [4, 12]}
        bridge.call.side_effect = BridgeError("blocked", detail=detail)
        result = await create_server(bridge).call_tool(
            "place_entity", {"item": "offshore-pump", "surface": "nauvis", "x": 1, "y": 2}
        )
        self.assertTrue(result.is_error)
        value = payload(result)
        self.assertEqual(value["detail"], detail)
        self.assertIn("valid_directions", value["hint"])

    async def test_expiry_hints_mention_60_seconds(self):
        for code in ("target_gone", "not_visible"):
            with self.subTest(code=code):
                bridge = Mock()
                bridge.call.side_effect = BridgeError(code)
                result = payload(await create_server(bridge).call_tool("factorio_ping", {}))
                self.assertIn("60 seconds", result["hint"])

    async def test_error_detail_hint_and_transport_request_id(self):
        for operation, params in [
            ("get_entity_inventory", {"target_ref": "42"}),
            ("wait_for_action", {"action_id": "42"}),
        ]:
            with self.subTest(operation=operation):
                detail = {"request_id": "internal-id", "reason": "expired"}
                bridge = Mock()
                bridge.call.side_effect = BridgeError("target_gone", "target_gone", detail)
                result = await create_server(bridge).call_tool(operation, params)
                self.assertTrue(result.is_error)
                self.assertEqual(
                    payload(result),
                    {
                        "ok": False,
                        "error": "target_gone",
                        "message": "target_gone",
                        "request_id": "internal-id",
                        "detail": {"reason": "expired"},
                        "hint": HINTS["target_gone"],
                    },
                )
                self.assertEqual(detail["request_id"], "internal-id")

    async def test_unknown_errors_and_non_dict_detail_have_no_hint_or_detail(self):
        for error in [BridgeError("unknown", detail=["private"]), RuntimeError("unexpected")]:
            bridge = Mock()
            bridge.call.side_effect = error
            result = await create_server(bridge).call_tool("factorio_ping", {})
            self.assertTrue(result.is_error)
            self.assertNotIn("hint", payload(result))
            self.assertNotIn("detail", payload(result))

    async def test_transport_request_id_without_public_detail(self):
        bridge = Mock()
        bridge.call.side_effect = BridgeError("not_connected", detail={"request_id": "req"})
        result = payload(await create_server(bridge).call_tool("factorio_ping", {}))
        self.assertEqual(result["request_id"], "req")
        self.assertNotIn("detail", result)

    async def test_empty_lua_arrays_are_normalized_in_new_fields(self):
        bridge = Mock()
        bridge.call.side_effect = BridgeError("blocked", detail={"blocking": {}, "tiles": {}})
        result = payload(await create_server(bridge).call_tool("factorio_ping", {}))
        self.assertEqual(result["detail"], {"blocking": [], "tiles": []})
        self.assertEqual(
            _normalize_result("check_placement", {"blocking": {}, "tiles": {}}),
            {"blocking": [], "tiles": []},
        )
        observed = _normalize_result(
            "observe_nearby",
            {"filters": {"types": {}, "names": {}}, "entities": [{"inventory_roles": {}}]},
        )
        self.assertEqual(observed["filters"], {"types": [], "names": []})
        self.assertEqual(observed["blocked_tiles"], [])
        self.assertEqual(observed["entities"][0]["inventory_roles"], [])

    async def test_wait_completes_after_running_polls(self):
        bridge = Mock()
        bridge.call.side_effect = [
            {"tick": tick, "result": {"action_id": "42", "status": status}}
            for tick, status in enumerate(["running", "running", "succeeded"], 10)
        ]
        with patch("factorio_mcp.server.asyncio.sleep") as sleep:
            result = await create_server(bridge).call_tool("wait_for_action", {"action_id": "42"})
        self.assertFalse(result.is_error)
        value = payload(result)
        self.assertEqual(value["status"], "succeeded")
        self.assertEqual(value["tick"], 12)
        self.assertFalse(value["timed_out"])
        self.assertGreaterEqual(value["waited_seconds"], 0)
        self.assertEqual(bridge.call.call_count, 3)
        self.assertEqual(sleep.await_count, 2)
        sleep.assert_awaited_with(0.25)
        bridge.call.assert_called_with("get_action_status", {"action_id": "42"})

    async def test_wait_timeout_is_fast_and_preserves_last_status(self):
        bridge = Mock()
        bridge.call.return_value = {"tick": 100, "result": {"action_id": "42", "status": "running"}}
        elapsed = 0.0

        async def sleep(seconds):
            nonlocal elapsed
            elapsed += seconds

        # Replace the module's time binding, leaving asyncio's own clock intact.
        with (
            patch("factorio_mcp.server.time") as clock,
            patch("factorio_mcp.server.asyncio.sleep", side_effect=sleep) as sleeper,
        ):
            clock.monotonic.side_effect = lambda: elapsed
            result = await create_server(bridge).call_tool(
                "wait_for_action", {"action_id": "42", "timeout_seconds": 1}
            )
        self.assertFalse(result.is_error)
        self.assertEqual(
            payload(result),
            {
                "ok": True,
                "tick": 100,
                "action_id": "42",
                "status": "running",
                "timed_out": True,
                "waited_seconds": 1,
            },
        )
        self.assertEqual(sleeper.await_count, 4)

    async def test_wait_terminal_failure_and_unconfigured(self):
        bridge = Mock()
        bridge.call.return_value = {
            "tick": 20,
            "result": {"status": "failed", "error": "blocked", "detail": {"x": 1}},
        }
        result = payload(
            await create_server(bridge).call_tool("wait_for_action", {"action_id": "1"})
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "blocked")
        self.assertEqual(result["detail"], {"x": 1})
        self.assertFalse(result["timed_out"])
        self.assertEqual(bridge.call.call_count, 1)
        result = await create_server(None).call_tool("wait_for_action", {"action_id": "1"})
        self.assertTrue(result.is_error)
        self.assertEqual(payload(result)["error"], "not_configured")

    async def test_wait_error_after_running_poll(self):
        bridge = Mock()
        bridge.call.side_effect = [
            {"result": {"status": "running"}},
            BridgeError("not_connected", "Disconnected", {"request_id": "req"}),
        ]
        with patch("factorio_mcp.server.asyncio.sleep") as sleep:
            result = await create_server(bridge).call_tool("wait_for_action", {"action_id": "42"})
        self.assertTrue(result.is_error)
        self.assertEqual(payload(result)["error"], "not_connected")
        self.assertEqual(payload(result)["request_id"], "req")
        sleep.assert_awaited_once_with(0.25)

    async def test_wait_releases_event_loop_during_bridge_call(self):
        # A synchronous bridge call must run outside the event-loop thread.
        loop = asyncio.get_running_loop()
        bridge = Mock()

        def call(*_):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return {"result": {"status": "succeeded"}}

        bridge.call.side_effect = call
        result = await create_server(bridge).call_tool("wait_for_action", {"action_id": "1"})
        self.assertFalse(result.is_error)
        self.assertIs(asyncio.get_running_loop(), loop)

    async def test_observe_schema_and_forwarding(self):
        bridge = Mock()
        bridge.call.return_value = {"tick": 1, "result": {}}
        server = create_server(bridge)
        tools = {tool.name: tool for tool in await server.list_tools()}
        schema = tools["observe_nearby"].input_schema
        self.assertFalse(schema.get("required"))
        props = schema["properties"]
        self.assertEqual(props["radius"]["minimum"], 1)
        self.assertEqual(props["radius"]["maximum"], 16)
        self.assertEqual(props["radius"]["default"], 16)
        for name in ("types", "names"):
            array = next(item for item in props[name]["anyOf"] if item["type"] == "array")
            self.assertEqual(array["maxItems"], 32)
            self.assertEqual(array["items"]["minLength"], 1)
            self.assertEqual(array["items"]["maxLength"], 200)
        for name, default in [
            ("owned_only", False),
            ("include_resources", True),
            ("include_tiles", True),
        ]:
            self.assertEqual(props[name]["type"], "boolean")
            self.assertIs(props[name]["default"], default)
        await server.call_tool("observe_nearby", {})
        bridge.call.assert_called_with("observe_nearby", {"entity_offset": 0, "tile_offset": 0})
        await server.call_tool(
            "observe_nearby",
            {"radius": 16, "owned_only": False, "include_resources": True, "include_tiles": True},
        )
        bridge.call.assert_called_with("observe_nearby", {"entity_offset": 0, "tile_offset": 0})
        await server.call_tool("observe_nearby", {"types": [], "names": []})
        bridge.call.assert_called_with(
            "observe_nearby", {"entity_offset": 0, "tile_offset": 0, "types": [], "names": []}
        )
        params = {
            "entity_offset": 2,
            "tile_offset": 3,
            "radius": 8,
            "types": ["furnace"],
            "names": ["stone-furnace"],
            "owned_only": True,
            "include_resources": False,
            "include_tiles": False,
        }
        await server.call_tool("observe_nearby", params)
        bridge.call.assert_called_with("observe_nearby", params)
        wait = tools["wait_for_action"].input_schema
        self.assertEqual(wait["required"], ["action_id"])
        self.assertEqual(wait["properties"]["timeout_seconds"]["default"], 20)
        self.assertEqual(wait["properties"]["timeout_seconds"]["minimum"], 1)
        self.assertEqual(wait["properties"]["timeout_seconds"]["maximum"], 50)

    async def test_wait_accepts_maximum_timeout(self):
        bridge = Mock()
        bridge.call.return_value = {"result": {"status": "succeeded"}}
        result = await create_server(bridge).call_tool(
            "wait_for_action", {"action_id": "1", "timeout_seconds": 50}
        )
        self.assertFalse(result.is_error)
        self.assertEqual(payload(result)["status"], "succeeded")

    async def test_invalid_observe_and_wait_params_are_rejected_before_bridge(self):
        bridge = Mock()
        server = create_server(bridge)
        for params in [
            {"radius": 0},
            {"radius": 17},
            {"radius": float("nan")},
            {"radius": True},
            {"types": [""]},
            {"names": ["a"] * 33},
            {"names": ["x" * 201]},
            {"types": [1]},
            {"types": "furnace"},
            {"owned_only": "true"},
            {"include_resources": 0},
            {"include_tiles": "false"},
        ]:
            with self.subTest(params=params), self.assertRaises(ToolError):
                await server.call_tool("observe_nearby", params)
        for timeout in (0, 51, float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ToolError):
                await server.call_tool(
                    "wait_for_action", {"action_id": "1", "timeout_seconds": timeout}
                )
        bridge.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
