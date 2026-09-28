import json
import unittest

from factorio_mcp.bridge import BridgeError, FactorioBridge, parse_bridge_response
from factorio_mcp.server import create_server


class BridgeTests(unittest.TestCase):
    def test_matching_response_only(self):
        raw = "\n".join([
            "unrelated log line",
            json.dumps({"protocol_version": 1, "request_id": "other", "ok": True}),
            json.dumps({"protocol_version": 1, "request_id": "wanted", "ok": True, "result": {"x": 2}}),
        ])
        self.assertEqual(parse_bridge_response(raw, "wanted")["result"], {"x": 2})
        with self.assertRaises(BridgeError) as caught:
            parse_bridge_response(raw, "missing")
        self.assertEqual(caught.exception.code, "mod_unavailable")

    def test_game_error_and_remote_host(self):
        raw = json.dumps({"protocol_version": 1, "request_id": "x", "ok": False, "error": "missing_materials"})
        with self.assertRaises(BridgeError) as caught:
            parse_bridge_response(raw, "x")
        self.assertEqual(caught.exception.code, "missing_materials")
        with self.assertRaises(BridgeError) as caught:
            FactorioBridge(host="192.0.2.1", password="test")
        self.assertEqual(caught.exception.code, "invalid_config")

    def test_uncertain_send_retries_same_request_id(self):
        request_ids = []

        def send(host, port, password, command):
            request = json.loads(command.removeprefix("/ai_mcp "))
            request_ids.append(request["request_id"])
            if len(request_ids) == 1:
                raise OSError("lost response")
            return json.dumps({"protocol_version": 1, "request_id": request["request_id"],
                               "ok": True, "result": {"action_id": "1"}})

        bridge = FactorioBridge(password="test", send=send)
        self.assertEqual(bridge.call("move_to", {"x": 1})["result"]["action_id"], "1")
        self.assertEqual(request_ids, [request_ids[0], request_ids[0]])

    def test_failed_send_exposes_request_id(self):
        def send(*_):
            raise TimeoutError("lost response")

        bridge = FactorioBridge(password="test", send=send)
        with self.assertRaises(BridgeError) as caught:
            bridge.call("craft", {"recipe": "wooden-chest", "count": 1})
        self.assertEqual(caught.exception.code, "not_connected")
        self.assertEqual(len(caught.exception.detail["request_id"]), 36)


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_and_success_shape(self):
        def send(host, port, password, command):
            request = json.loads(command.removeprefix("/ai_mcp "))
            self.assertEqual(request["agent_id"], "agent-1")
            return json.dumps({"protocol_version": 1, "request_id": request["request_id"],
                               "ok": True, "tick": 42, "result": {"items": {}}})

        server = create_server(FactorioBridge(password="test", send=send))
        self.assertEqual(len(await server.list_tools()), 15)
        result = await server.call_tool("get_inventory", {})
        self.assertFalse(result.is_error)
        self.assertEqual(json.loads(result.content[0].text), {"ok": True, "tick": 42, "items": []})

    async def test_input_validation_and_unconfigured_error(self):
        server = create_server(None)
        result = await server.call_tool("factorio_ping", {})
        self.assertTrue(result.is_error)
        self.assertEqual(json.loads(result.content[0].text)["error"], "not_configured")
        move = next(tool for tool in await server.list_tools() if tool.name == "move_to")
        self.assertEqual(set(move.input_schema["required"]), {"surface", "x", "y"})
        place = next(tool for tool in await server.list_tools() if tool.name == "place_entity")
        self.assertEqual(place.input_schema["properties"]["direction"]["default"], 0)


if __name__ == "__main__":
    unittest.main()
