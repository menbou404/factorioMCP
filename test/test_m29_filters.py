import copy
import json
import unittest
from unittest.mock import Mock

from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import _normalize_error_detail, _normalize_result, create_server


def payload(response):
    return json.loads(response.content[0].text)


class FiltersTests(unittest.IsolatedAsyncioTestCase):
    async def test_descriptions_and_schema(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for text in (
            "every inserter",
            "burner-inserter",
            "whitelist",
            "mixed belt",
            "blacklist",
            "contaminants",
            "0-5 unique",
            "empty list",
            "no items",
        ):
            self.assertIn(text, tools["set_inserter_filter"].description)
        for text in (
            "belt's travel direction",
            "output priority side",
            "everything else to the other side",
            "empty string",
            "Omitted arguments are unchanged",
            "at least one",
            "hint",
            "no items",
        ):
            self.assertIn(text, tools["set_splitter"].description)
        for operation in ("get_entity_inventory", "get_entity_inventories"):
            self.assertIn("filter_mode", tools[operation].description)
            self.assertIn("slot order", tools[operation].description)
        self.assertIn("these fields survive compact=true", tools["observe_nearby"].description)
        schema = tools["set_inserter_filter"].input_schema
        self.assertEqual(schema["properties"]["items"]["maxItems"], 5)
        self.assertEqual(schema["properties"]["mode"]["default"], "whitelist")
        self.assertEqual(set(schema["required"]), {"target_ref", "items"})
        self.assertEqual(tools["set_splitter"].input_schema["required"], ["target_ref"])

    async def test_invalid_parameters_never_reach_bridge(self):
        cases = [
            ("set_inserter_filter", {"items": ["iron-plate"] * 6}),
            ("set_inserter_filter", {"items": ["iron-plate", "iron-plate"]}),
            ("set_inserter_filter", {"items": [], "mode": "allow"}),
            ("set_inserter_filter", {"items": [], "mode": False}),
            ("set_inserter_filter", {"items": [], "mode": None}),
            *[
                ("set_inserter_filter", {"items": value})
                for value in (
                    None,
                    {},
                    "iron-plate",
                    [""],
                    [1],
                    [False],
                    [None],
                )
            ],
            ("set_splitter", {}),
            ("set_splitter", {"input_priority": None, "output_priority": None, "filter": None}),
            *[
                ("set_splitter", {field: value})
                for field in ("input_priority", "output_priority")
                for value in ("up", "", 1, False, [])
            ],
            *[("set_splitter", {"filter": value}) for value in (1, False, [], {})],
        ]
        bridge = Mock()
        server = create_server(bridge)
        for operation, arguments in cases:
            with self.subTest(operation=operation, arguments=arguments):
                try:
                    response = await server.call_tool(operation, {"target_ref": "a", **arguments})
                    self.assertTrue(response.is_error)
                    result = payload(response)
                except ToolError as exc:
                    result = json.loads(str(exc))
                self.assertEqual(result["error"], "invalid_input")
        bridge.call.assert_not_called()

    async def test_validation_over_both_client_modes(self):
        for mode in ("auto", "legacy"):
            bridge = Mock()
            async with Client(create_server(bridge), mode=mode) as client:
                for operation, arguments in (
                    ("set_inserter_filter", {"items": ["coal"] * 6}),
                    ("set_inserter_filter", {"items": ["coal", "coal"]}),
                    ("set_splitter", {"output_priority": "up"}),
                    ("set_splitter", {}),
                ):
                    response = await client.call_tool(operation, {"target_ref": "a", **arguments})
                    self.assertTrue(response.is_error)
                    self.assertEqual(payload(response)["error"], "invalid_input")
            bridge.call.assert_not_called()

    async def test_inserter_parameters_and_read_back_result(self):
        bridge = Mock()
        server = create_server(bridge)
        for items, mode in (
            ([], "blacklist"),
            (["coal", "iron-plate"], "whitelist"),
            (["coal", "iron-plate", "copper-plate", "stone", "wood"], "blacklist"),
        ):
            result = {
                "name": "inserter",
                "use_filters": bool(items),
                "mode": mode,
                "filters": items or {},
                "position": {"surface": "nauvis", "x": 1, "y": 2},
            }
            bridge.call.return_value = {"tick": 42, "result": copy.deepcopy(result)}
            response = await server.call_tool(
                "set_inserter_filter", {"target_ref": "a", "items": items, "mode": mode}
            )
            self.assertFalse(response.is_error)
            self.assertEqual(
                payload(response), {"ok": True, "tick": 42, **result, "filters": items}
            )
            bridge.call.assert_called_with(
                "set_inserter_filter", {"target_ref": "a", "items": items, "mode": mode}
            )
        await server.call_tool("set_inserter_filter", {"target_ref": "a", "items": []})
        bridge.call.assert_called_with(
            "set_inserter_filter", {"target_ref": "a", "items": [], "mode": "whitelist"}
        )

    async def test_splitter_forwards_only_given_settings_and_preserves_result(self):
        bridge = Mock()
        server = create_server(bridge)
        for settings in (
            {"input_priority": "right"},
            {"output_priority": "none"},
            {"filter": ""},
            {"filter": "coal"},
            {"input_priority": "left", "filter": "iron-plate"},
            {"input_priority": None, "output_priority": "left", "filter": None},
            {"input_priority": "none", "output_priority": "right", "filter": "coal"},
        ):
            for extra in ({}, {"filter": "coal", "hint": "output_priority was set to left"}):
                result = {
                    "name": "splitter",
                    "input_priority": "none",
                    "output_priority": "left",
                    "position": {"surface": "nauvis", "x": 1, "y": 2},
                    **extra,
                }
                bridge.call.return_value = {"tick": 44, "result": copy.deepcopy(result)}
                response = await server.call_tool("set_splitter", {"target_ref": "a", **settings})
                self.assertFalse(response.is_error)
                self.assertEqual(payload(response), {"ok": True, "tick": 44, **result})
                bridge.call.assert_called_with(
                    "set_splitter",
                    {"target_ref": "a", **{k: v for k, v in settings.items() if v is not None}},
                )

    async def test_single_batch_and_cached_inserter_filter_normalization(self):
        for filters in (None, {}, ["coal", "iron-plate"]):
            result = {"name": "burner-inserter", "type": "inserter", "inventories": {"fuel": {}}}
            if filters is not None:
                result.update(filters=filters, filter_mode="blacklist")
            expected = {**result, "inventories": {"fuel": []}}
            if filters is not None:
                expected["filters"] = filters or []
            self.assertEqual(
                _normalize_result("get_entity_inventory", copy.deepcopy(result)), expected
            )
            batch = {"results": [{"index": 1, "ok": True, "result": copy.deepcopy(result)}]}
            self.assertEqual(
                _normalize_result("get_entity_inventories", batch)["results"][0]["result"], expected
            )
        cached = {"original_response": {"result": {"use_filters": False, "filters": {}}}}
        self.assertEqual(
            _normalize_result("get_request_result", cached)["original_response"]["result"][
                "filters"
            ],
            [],
        )
        observation = {
            "filters": {"compact": True},
            "entities": [
                {
                    "name": "splitter",
                    "input_priority": "right",
                    "output_priority": "left",
                    "filter": "coal",
                }
            ],
            "blocked_tiles": [],
            "resources": [],
        }
        self.assertEqual(
            _normalize_result("observe_nearby", copy.deepcopy(observation)), observation
        )

    async def test_new_error_details_survive_normalization(self):
        bridge = Mock()
        server = create_server(bridge)
        for operation, arguments in (
            ("set_inserter_filter", {"items": ["missing-a", "missing-b"]}),
            ("set_splitter", {"filter": "missing-a"}),
        ):
            for code, detail in (
                ("invalid_input", {"unknown_items": ["missing-a", "missing-b"]}),
                ("invalid_target", {"entity": "wooden-chest", "reason": "wrong_entity_type"}),
                ("invalid_target", {"entity": "inserter", "reason": "no_filter_slots"}),
                ("invalid_input", {"reason": "too_many_filters", "filter_slot_count": 2}),
                ("out_of_range", {"distance": 12, "reach": 10, "position": {"x": 12, "y": 0}}),
            ):
                bridge.call.side_effect = BridgeError(code, "rejected", copy.deepcopy(detail))
                response = await server.call_tool(operation, {"target_ref": "a", **arguments})
                self.assertTrue(response.is_error)
                self.assertEqual(payload(response)["error"], code)
                self.assertEqual(payload(response)["detail"], detail)
        self.assertEqual(_normalize_error_detail({"unknown_items": {}}), {"unknown_items": []})


if __name__ == "__main__":
    unittest.main()
