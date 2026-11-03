import asyncio
import json
import unittest
from unittest.mock import Mock, patch

from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ValidationError

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
    async def test_m14u_descriptions_and_schema(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name in ("place_entity", "check_placement"):
            tool = tools[name]
            for text in (
                "underground-belt",
                "splitter",
                "long-handed-inserter",
                "pipe-to-ground",
                "underground_type is only valid for underground-belt",
                "default input",
                "for output it is the direction items leave",
            ):
                with self.subTest(tool=name, text=text):
                    self.assertIn(text, tool.description)
            schema = tool.input_schema
            self.assertNotIn("underground_type", schema.get("required", []))
            parameter = schema["properties"]["underground_type"]
            self.assertIsNone(parameter["default"])
            self.assertIn({"enum": ["input", "output"], "type": "string"}, parameter["anyOf"])
        for text in (
            "belt_items",
            "all transport lines",
            "underground_type",
            "neighbour = {x, y}",
            "connection_type (normal or underground)",
            "long-handed-inserter",
            "pickup_position and drop_position",
        ):
            self.assertIn(text, tools["observe_nearby"].description)

    async def test_underground_type_forwarded_only_when_set(self):
        bridge = Mock()
        bridge.call.return_value = {"result": {}}
        server = create_server(bridge)
        for name in ("place_entity", "check_placement"):
            for item in ("underground-belt", "splitter", "long-handed-inserter", "pipe-to-ground"):
                params = {"item": item, "surface": "nauvis", "x": 1, "y": 2, "direction": 4}
                for optional in ({}, {"underground_type": None}):
                    await server.call_tool(name, {**params, **optional})
                    bridge.call.assert_called_with(name, params)
                if item == "underground-belt":
                    for underground_type in ("input", "output"):
                        supplied = {**params, "underground_type": underground_type}
                        await server.call_tool(name, supplied)
                        bridge.call.assert_called_with(name, supplied)

    async def test_underground_type_validation(self):
        bridge = Mock()
        server = create_server(bridge)
        for name in ("place_entity", "check_placement"):
            params = {"item": "underground-belt", "surface": "nauvis", "x": 1, "y": 2}
            for invalid in ("invalid", "INPUT", "", 1, False, {}, []):
                with (
                    self.subTest(tool=name, invalid=invalid),
                    self.assertRaises(ToolError) as caught,
                ):
                    await server.call_tool(name, {**params, "underground_type": invalid})
                self.assertEqual(json.loads(str(caught.exception))["error"], "invalid_input")
            for item in ("transport-belt", "splitter", "long-handed-inserter", "pipe-to-ground"):
                for underground_type in ("input", "output"):
                    result = await server.call_tool(
                        name, {**params, "item": item, "underground_type": underground_type}
                    )
                    self.assertTrue(result.is_error)
                    self.assertEqual(payload(result)["error"], "invalid_input")
        bridge.call.assert_not_called()

    async def test_belt_items_normalize_and_observation_fields_survive(self):
        cargo = [{"name": "iron-plate", "quality": "normal", "count": 3}]
        for belt_type in ("transport-belt", "underground-belt", "splitter"):
            for fields, expected in (
                ({}, []),
                ({"belt_items": {}}, []),
                ({"belt_items": []}, []),
                ({"belt_items": cargo}, cargo),
            ):
                entity = {"type": belt_type, **fields}
                if belt_type == "underground-belt":
                    entity.update(underground_type="output", neighbour={"x": 1.5, "y": 2.5})
                expected_entity = {**entity}
                if "belt_items" in fields:
                    expected_entity["belt_items"] = expected
                result = _normalize_result("observe_nearby", {"entities": [entity]})
                self.assertEqual(result["entities"][0], expected_entity)
                self.assertEqual(result["entities"][0].get("belt_items", []), expected)

    async def test_m13_descriptions_and_role_hint(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name in ("mine_entity", "wait_for_action"):
            for text in (
                "estimated_seconds",
                "estimate that can change",
                "mining is interrupted or the target is depleted",
                "timeout_seconds",
            ):
                with self.subTest(name=name, text=text):
                    self.assertIn(text, tools[name].description)
        self.assertIn("detail.estimated_seconds", tools["get_action_status"].description)
        for name in ("check_placement", "place_entity"):
            for text in (
                "offshore-pump",
                "water_direction side must face water",
                "pipes/boilers",
                "connect on the output_direction side",
            ):
                with self.subTest(name=name, text=text):
                    self.assertIn(text, tools[name].description)
        for text in ("input -> source", "output -> result", "source -> input"):
            self.assertIn(text, tools["transfer_item"].description)
            self.assertIn(text, HINTS["invalid_target"])
        for text in ("stone-furnace", "assembling-machine-1", "lab", "canonical"):
            self.assertIn(text, tools["transfer_item"].description)
        self.assertIn("role_aliases", HINTS["invalid_target"])
        self.assertIn("valid_roles", HINTS["invalid_target"])

    async def test_role_alias_maps_normalize_and_keep_canonical_roles(self):
        bridge = Mock()
        server = create_server(bridge)
        for aliases in ({}, [], {"input": "source", "output": "result"}):
            with self.subTest(aliases=aliases):
                detail = {
                    "entity": "stone-furnace",
                    "valid_roles": ["fuel", "result", "source"],
                    "role_aliases": aliases,
                }
                bridge.call.side_effect = BridgeError("invalid_target", detail=detail)
                result = await server.call_tool(
                    "transfer_item",
                    {
                        "target_ref": "42",
                        "direction": "to_entity",
                        "inventory_role": "unknown",
                        "item": "stone",
                        "count": 1,
                    },
                )
                value = payload(result)
                self.assertTrue(result.is_error)
                self.assertEqual(value["detail"], {**detail, "role_aliases": aliases or {}})
                self.assertIsInstance(value["detail"]["role_aliases"], dict)
                self.assertEqual(detail["role_aliases"], aliases)
                self.assertEqual(
                    _normalize_result("get_entity_inventory", {"role_aliases": aliases}),
                    {"role_aliases": aliases or {}},
                )
        bridge.call.side_effect = None
        for name, params, result in (
            ("get_entity_inventory", {"target_ref": "42"}, {"inventories": {"source": {}}}),
            (
                "transfer_item",
                {
                    "target_ref": "42",
                    "direction": "to_entity",
                    "inventory_role": "input",
                    "item": "stone",
                    "count": 1,
                },
                {"moved": 1, "inventory_role": "source"},
            ),
        ):
            bridge.call.return_value = {"result": result}
            value = payload(await server.call_tool(name, params))
            if name == "get_entity_inventory":
                self.assertEqual(value["inventories"], {"source": []})
            else:
                self.assertEqual(value["inventory_role"], "source")
                bridge.call.assert_called_with("transfer_item", params)

    async def test_mining_eta_survives_start_status_and_wait(self):
        bridge = Mock()
        server = create_server(bridge)
        bridge.call.return_value = {
            "result": {"action_id": "42", "status": "running", "estimated_seconds": 6.0}
        }
        started = payload(await server.call_tool("mine_entity", {"target_ref": "7", "count": 3}))
        self.assertEqual(started["estimated_seconds"], 6.0)
        for eta in (None, 0.0, 4.5):
            detail = {"gained": 1, "count": 3}
            if eta is not None:
                detail["estimated_seconds"] = eta
            # A timed-out wait must preserve the latest running detail from the mod.
            bridge.call.return_value = {
                "result": {"action_id": "42", "status": "running", "detail": detail}
            }
            status = payload(await server.call_tool("get_action_status", {"action_id": "42"}))
            with patch("factorio_mcp.server.time") as clock:
                clock.monotonic.side_effect = [0, 1]
                waited = payload(
                    await server.call_tool(
                        "wait_for_action", {"action_id": "42", "timeout_seconds": 1}
                    )
                )
            self.assertTrue(waited["timed_out"])
            self.assertEqual(status["detail"], detail)
            self.assertEqual(waited["detail"], detail)

    async def test_pump_sides_survive_placeable_blocked_and_observed_normalization(self):
        sides = {"water_direction": 12, "output_direction": 4}
        for placeable in (False, True):
            result = _normalize_result("check_placement", {"placeable": placeable, **sides})
            self.assertEqual(result, {"placeable": placeable, **sides})
        result = _normalize_result(
            "observe_nearby", {"entities": [{"name": "offshore-pump", **sides}]}
        )
        self.assertEqual(result["entities"], [{"name": "offshore-pump", **sides}])

    async def test_explored_map_description_schema_and_forwarding(self):
        bridge = Mock()
        bridge.call.return_value = {"tick": 42, "result": {}}
        server = create_server(bridge)
        tools = {tool.name: tool for tool in await server.list_tools()}
        tool = tools["get_explored_map"]
        for text in (
            "only what this character saw with observe_nearby",
            "current surface",
            "8x8 tiles",
            "fully inside an observation circle",
            "as of observed_tick",
            "update when the area is observed again",
            "resource patches",
            "shore tile for offshore pumps",
            "forests and rocks nearest first",
        ):
            with self.subTest(text=text):
                self.assertIn(text, tool.description)
        self.assertIn("fully observed 8x8 cells", tools["observe_nearby"].description)
        self.assertIn("get_explored_map", tools["observe_nearby"].description)
        schema = tool.input_schema
        self.assertFalse(schema.get("required"))
        props = schema["properties"]
        self.assertEqual(props["limit"]["type"], "integer")
        self.assertEqual(props["limit"]["minimum"], 1)
        self.assertEqual(props["limit"]["maximum"], 50)
        self.assertEqual(props["limit"]["default"], 10)
        array = next(item for item in props["resource_names"]["anyOf"] if item["type"] == "array")
        self.assertEqual(array["maxItems"], 32)
        self.assertEqual(array["items"]["minLength"], 1)
        self.assertEqual(array["items"]["maxLength"], 200)
        await server.call_tool("get_explored_map", {})
        bridge.call.assert_called_with("get_explored_map", {"limit": 10})
        for names in (None, [], ["iron-ore", "coal"]):
            params = {"limit": 50, "resource_names": names}
            result = await server.call_tool("get_explored_map", params)
            self.assertFalse(result.is_error)
            expected = {"limit": 50}
            if names is not None:
                expected["resource_names"] = names
            bridge.call.assert_called_with("get_explored_map", expected)

    async def test_explored_map_normalizes_empty_lists_and_preserves_records(self):
        bridge = Mock()
        server = create_server(bridge)
        lists = ("resources", "water", "trees", "rocks")
        for field in lists:
            with self.subTest(field=field):
                entry = {
                    "cells": 2,
                    "distance": 10,
                    "observed_tick": 30,
                    "left_top": {"x": -16, "y": 0},
                    "right_bottom": {"x": 0, "y": 8},
                }
                expected = {
                    "position": {"surface": "nauvis", "x": 1, "y": 2},
                    "cell_size": 8,
                    "explored": {"cells": 0},
                    **{name: [entry] if name == field else [] for name in lists},
                    **{name + "_total": int(name == field) for name in lists},
                }
                raw = {key: {} if value == [] else value for key, value in expected.items()}
                bridge.call.return_value = {"tick": 42, "result": raw}
                result = payload(await server.call_tool("get_explored_map", {}))
                self.assertEqual(result, {"ok": True, "tick": 42, **expected})

    async def test_invalid_explored_map_params_are_rejected_before_bridge(self):
        bridge = Mock()
        server = create_server(bridge)
        cases = [{"limit": value} for value in (0, 51, -1, 1.5, True, "10", float("nan"))]
        cases += [
            {"resource_names": value} for value in ([""], ["x" * 201], ["coal"] * 33, [1], "coal")
        ]
        for params in cases:
            with self.subTest(params=params), self.assertRaises(ToolError):
                await server.call_tool("get_explored_map", params)
        bridge.call.assert_not_called()

    async def test_mining_description_explains_conflicting_operations(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        description = tools["mine_entity"].description
        self.assertIn("Crafting runs alongside mining", description)
        self.assertIn(
            "While mining runs, place_entity, transfer_item, rotate_entity, and set_recipe "
            "return action_conflict",
            description,
        )

    async def test_move_description_explains_distance_limit_and_intermediate_points(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        description = tools["move_to"].description
        self.assertIn("within 128 tiles in a straight line from the character", description)
        self.assertIn("For farther goals, move via intermediate points", description)

    async def test_input_errors_through_mcp_client(self):
        cases = [
            ("wait_for_action", {}, {"action_id": "missing"}),
            (
                "mine_entity",
                {"target_ref": "42", "count": 160},
                {"count": "less_than_equal"},
            ),
            (
                "mine_entity",
                {"target_ref": "42", "count": "2"},
                {"count": "int_type"},
            ),
            (
                "move_to",
                {"surface": "nauvis", "x": "a", "y": 0},
                {"x": "float_parsing"},
            ),
            ("move_to", {}, {"surface": "missing", "x": "missing", "y": "missing"}),
            ("observe_nearby", {"types": [1]}, {"types.0": "string_type"}),
        ]
        # Exercise both the modern request dispatcher and the stream/session path.
        for mode in ("auto", "legacy"):
            async with Client(create_server(None), mode=mode) as client:
                for name, arguments, expected in cases:
                    with self.subTest(mode=mode, tool=name, arguments=arguments):
                        result = await client.call_tool(name, arguments)
                        self.assertTrue(result.is_error)
                        self.assertTrue(result.model_dump(by_alias=True)["isError"])
                        self.assertEqual(len(result.content), 1)
                        value = payload(result)
                        self.assertIs(value["ok"], False)
                        self.assertEqual(value["error"], "invalid_input")
                        self.assertTrue(value["message"])
                        self.assertEqual(value["hint"], HINTS["invalid_input"])
                        errors = value["detail"]["errors"]
                        self.assertEqual({e["field"]: e["type"] for e in errors}, expected)
                        for error in errors:
                            self.assertEqual(set(error), {"field", "type", "message"})
                            self.assertTrue(error["message"])
                result = await client.call_tool("wait_for_action", {"action_id": "42"})
                self.assertTrue(result.is_error)
                self.assertEqual(payload(result)["error"], "not_configured")

    async def test_input_validation_covers_every_tool_schema(self):
        bridge = Mock()
        server = create_server(bridge)
        async with Client(server) as client:
            for tool in await server.list_tools():
                properties = tool.input_schema.get("properties", {})
                if not properties:
                    continue  # No-argument tools have no fields to validate.
                with self.subTest(tool=tool.name):
                    # None violates every current tool's first parameter schema.
                    field = next(iter(properties))
                    result = await client.call_tool(tool.name, {field: None})
                    self.assertTrue(result.is_error)
                    value = payload(result)
                    self.assertEqual(value["error"], "invalid_input")
                    self.assertEqual(value["hint"], HINTS["invalid_input"])
                    self.assertIn(field, [e["field"] for e in value["detail"]["errors"]])
        bridge.call.assert_not_called()

    async def test_extra_arguments_keep_sdk_behavior(self):
        # The installed SDK ignores extra arguments rather than rejecting them.
        async with Client(create_server(None)) as client:
            result = await client.call_tool("wait_for_action", {"action_id": "42", "extra": 1})
        self.assertTrue(result.is_error)
        self.assertEqual(payload(result)["error"], "not_configured")

    async def test_mod_invalid_input_gets_hint_without_validation_detail(self):
        bridge = Mock()
        bridge.call.side_effect = BridgeError("invalid_input")
        async with Client(create_server(bridge)) as client:
            result = await client.call_tool("factorio_ping", {})
        self.assertTrue(result.is_error)
        value = payload(result)
        self.assertEqual(value["error"], "invalid_input")
        self.assertEqual(value["hint"], HINTS["invalid_input"])
        self.assertNotIn("detail", value)

    async def test_tool_body_validation_errors_keep_sdk_behavior(self):
        class BodyModel(BaseModel):
            count: int

        async def body_validation_error() -> str:
            BodyModel.model_validate({})
            return "unreachable"

        async def deliberate_body_error() -> str:
            try:
                BodyModel.model_validate({})
            except ValidationError as exc:
                raise ToolError("body failed") from exc
            return "unreachable"

        results = []
        for server in (MCPServer("baseline"), create_server(None)):
            server.add_tool(body_validation_error)
            server.add_tool(deliberate_body_error)
            async with Client(server) as client:
                for name in ("body_validation_error", "deliberate_body_error", "unknown_tool"):
                    with self.subTest(server=server.name, tool=name):
                        result = await client.call_tool(name, {})
                        self.assertTrue(result.is_error)
                        results.append(result.content[0].text)
        self.assertEqual(results[:3], results[3:])

    async def test_success_through_mcp_client_is_unchanged(self):
        bridge = Mock()
        bridge.call.return_value = {"tick": 12, "result": {"connected": True}}
        async with Client(create_server(bridge)) as client:
            result = await client.call_tool("factorio_ping", {})
        self.assertFalse(result.is_error)
        self.assertEqual(payload(result), {"ok": True, "tick": 12, "connected": True})
        bridge.call.assert_called_once_with("ping", None)

    async def test_count_descriptions_state_input_limits(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name in ("mine_entity", "craft", "transfer_item"):
            with self.subTest(name=name):
                self.assertIn("count is 1-100", tools[name].description)

    async def test_action_conflict_hint_explains_immediate_operations(self):
        hint = HINTS["action_conflict"]
        for text in (
            "detail.running_action",
            "wait_for_action",
            "cancel_action",
            "one craft at a time",
            "one walk/mine at a time",
            "crafting can run alongside either",
            "place_entity, transfer_item, rotate_entity, and set_recipe",
            "also return action_conflict while a walk or mine is running",
            "they are allowed while crafting",
        ):
            with self.subTest(text=text):
                self.assertIn(text, hint)

    async def test_observe_description_explains_distance_reach_and_resource_summary(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        description = tools["observe_nearby"].description
        for text in (
            "distance from the character centre to the entity centre",
            "reach gives build (placing)",
            "interact (transferring, rotating, and setting recipes)",
            "mine_resource (resource/tree/rock mining)",
            "reach in tiles",
            "Resources, trees, and rocks are judged to their collision edge",
            "reachable at a slightly larger distance than mine_resource",
            "include_resources = false",
            "individual resource entities are omitted",
            "resources summary still returns per-resource tile count, total amount, "
            "and bounding area",
        ):
            with self.subTest(text=text):
                self.assertIn(text, description)

    async def test_observe_normalization_preserves_distance_and_reach_object(self):
        bridge = Mock()
        server = create_server(bridge)
        entity = {"x": 3, "y": 4, "distance": 5.0}
        for reach in ({"build": 10, "interact": 10, "mine_resource": 2.7}, {}):
            with self.subTest(reach=reach):
                bridge.call.return_value = {
                    "result": {"entities": [entity.copy()], "resources": {}, "reach": reach.copy()}
                }
                result = await server.call_tool("observe_nearby", {})
                self.assertFalse(result.is_error)
                value = payload(result)
                self.assertIsInstance(value["reach"], dict)
                self.assertEqual(value["reach"], reach)
                self.assertEqual(value["entities"], [entity])
                self.assertEqual(value["resources"], [])

    async def test_action_descriptions_explain_concurrency(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name, alongside in (
            ("craft", "Crafting runs alongside walking and mining"),
            ("move_to", "Crafting runs alongside walking"),
            ("mine_entity", "Crafting runs alongside mining"),
        ):
            with self.subTest(name=name):
                self.assertIn(alongside, tools[name].description)
                limit = "one craft at a time" if name == "craft" else "one walk/mine at a time"
                self.assertIn(limit, tools[name].description)
        for field in ("action", "craft_action", "walk/mine slot", "crafting slot"):
            self.assertIn(field, tools["get_agent_status"].description)

    async def test_agent_status_preserves_both_action_slots(self):
        bridge = Mock()
        server = create_server(bridge)
        slots = {
            "action": {"id": "10", "type": "move", "status": "running"},
            "craft_action": {"id": "11", "type": "craft", "status": "running"},
        }
        for running in (
            {},
            {"action": slots["action"]},
            {"craft_action": slots["craft_action"]},
            slots,
        ):
            with self.subTest(running=running):
                bridge.call.return_value = {"result": {"inventory": {}, **running}}
                value = payload(await server.call_tool("get_agent_status", {}))
                self.assertEqual(value["inventory"], [])
                for field in slots:
                    self.assertEqual(value.get(field), running.get(field))

    async def test_craft_conflict_preserves_the_conflicting_slot(self):
        bridge = Mock()
        detail = {"running_action": {"id": "11", "type": "craft"}}
        bridge.call.side_effect = BridgeError("action_conflict", detail=detail)
        result = await create_server(bridge).call_tool(
            "craft", {"recipe": "iron-gear-wheel", "count": 1}
        )
        self.assertTrue(result.is_error)
        value = payload(result)
        self.assertEqual(value["detail"], detail)
        self.assertEqual(value["hint"], HINTS["action_conflict"])
        for text in ("detail.running_action", "one craft at a time", "one walk/mine at a time"):
            self.assertIn(text, value["hint"])

    async def test_action_status_descriptions_explain_running_progress(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name in ("get_action_status", "wait_for_action"):
            with self.subTest(name=name):
                for field in ("detail.gained", "detail.count", "detail.remaining"):
                    self.assertIn(field, tools[name].description)

    async def test_research_wait_returns_immediately_when_researched(self):
        bridge = Mock()
        bridge.call.return_value = {"result": {"researched": ["automation"], "queue": {}}}
        with patch("factorio_mcp.server.asyncio.sleep") as sleep:
            result = await create_server(bridge).call_tool(
                "wait_for_research", {"technology": "automation"}
            )
        self.assertFalse(result.is_error)
        value = payload(result)
        waited = value.pop("waited_seconds")
        self.assertGreaterEqual(waited, 0)
        self.assertEqual(
            value,
            {
                "technology": "automation",
                "researched": True,
                "researching": False,
                "progress": None,
                "timed_out": False,
            },
        )
        bridge.call.assert_called_once_with("get_research_status", None)
        sleep.assert_not_awaited()

    async def test_research_wait_returns_immediately_when_not_queued(self):
        bridge = Mock()
        bridge.call.return_value = {
            "result": {
                "researched": {},
                "queue": ["other-technology"],
                "current": "other-technology",
                "progress": 0.5,
            }
        }
        server = create_server(bridge)
        for technology in ("automation", "unknown-technology"):
            with self.subTest(technology=technology):
                bridge.call.reset_mock()
                with patch("factorio_mcp.server.asyncio.sleep") as sleep:
                    result = await server.call_tool("wait_for_research", {"technology": technology})
                self.assertFalse(result.is_error)
                value = payload(result)
                self.assertEqual(value["technology"], technology)
                self.assertFalse(value["researched"])
                self.assertFalse(value["researching"])
                self.assertIsNone(value["progress"])
                self.assertFalse(value["timed_out"])
                bridge.call.assert_called_once_with("get_research_status", None)
                sleep.assert_not_awaited()

    async def test_research_wait_follows_queue_until_researched(self):
        bridge = Mock()
        bridge.call.side_effect = [
            {"result": {"queue": ["automation"], "current": "other", "progress": 0.9}},
            {"result": {"current": "automation", "progress": 0.5}},
            {"result": {"researched": ["automation"]}},
        ]
        with patch("factorio_mcp.server.asyncio.sleep") as sleep:
            result = await create_server(bridge).call_tool(
                "wait_for_research", {"technology": "automation"}
            )
        self.assertFalse(result.is_error)
        self.assertTrue(payload(result)["researched"])
        self.assertFalse(payload(result)["timed_out"])
        self.assertEqual(bridge.call.call_count, 3)
        self.assertEqual(sleep.await_count, 2)
        sleep.assert_awaited_with(0.25)

    async def test_research_wait_returns_when_removed_from_queue(self):
        bridge = Mock()
        bridge.call.side_effect = [
            {"result": {"queue": ["automation"]}},
            {"result": {"queue": {}}},
        ]
        with patch("factorio_mcp.server.asyncio.sleep") as sleep:
            result = await create_server(bridge).call_tool(
                "wait_for_research", {"technology": "automation"}
            )
        self.assertFalse(result.is_error)
        self.assertFalse(payload(result)["researched"])
        self.assertFalse(payload(result)["researching"])
        self.assertIsNone(payload(result)["progress"])
        self.assertFalse(payload(result)["timed_out"])
        self.assertEqual(bridge.call.call_count, 2)
        sleep.assert_awaited_once_with(0.25)

    async def test_research_wait_timeout_preserves_current_progress(self):
        # Current research must be recognized even when the queue is empty.
        for current, progress in (("automation", 0.375), ("other", None)):
            with self.subTest(current=current):
                bridge = Mock()
                bridge.call.return_value = {
                    "result": {
                        "current": current,
                        "progress": 0.375,
                        "queue": ["automation"] if current == "other" else {},
                    }
                }
                with (
                    patch("factorio_mcp.server.time") as clock,
                    patch("factorio_mcp.server.asyncio.sleep") as sleeper,
                ):
                    clock.monotonic.side_effect = [0, 0, 0.25, 0.5, 0.75, 1]
                    result = await create_server(bridge).call_tool(
                        "wait_for_research", {"technology": "automation", "timeout_seconds": 1}
                    )
                self.assertFalse(result.is_error)
                self.assertEqual(
                    payload(result),
                    {
                        "technology": "automation",
                        "researched": False,
                        "researching": True,
                        "progress": progress,
                        "timed_out": True,
                        "waited_seconds": 1,
                    },
                )
                self.assertEqual(sleeper.await_count, 4)
                self.assertEqual(bridge.call.call_count, 5)

    async def test_research_wait_handles_bridge_errors_and_unconfigured(self):
        bridge = Mock()
        bridge.call.side_effect = [
            {"result": {"current": "automation"}},
            BridgeError("not_connected", "Disconnected", {"request_id": "req"}),
        ]
        with patch("factorio_mcp.server.asyncio.sleep") as sleep:
            result = await create_server(bridge).call_tool(
                "wait_for_research", {"technology": "automation"}
            )
        self.assertTrue(result.is_error)
        self.assertEqual(payload(result)["error"], "not_connected")
        self.assertEqual(payload(result)["request_id"], "req")
        sleep.assert_awaited_once_with(0.25)
        result = await create_server(None).call_tool(
            "wait_for_research", {"technology": "automation"}
        )
        self.assertTrue(result.is_error)
        self.assertEqual(payload(result)["error"], "not_configured")

    async def test_research_wait_schema_and_timeout_bounds(self):
        bridge = Mock()
        bridge.call.return_value = {"result": {"researched": ["automation"]}}
        server = create_server(bridge)
        tools = {tool.name: tool for tool in await server.list_tools()}
        tool = tools["wait_for_research"]
        schema = tool.input_schema
        self.assertEqual(schema["required"], ["technology"])
        self.assertEqual(schema["properties"]["technology"]["minLength"], 1)
        self.assertEqual(schema["properties"]["timeout_seconds"]["minimum"], 1)
        self.assertEqual(schema["properties"]["timeout_seconds"]["maximum"], 50)
        self.assertEqual(schema["properties"]["timeout_seconds"]["default"], 20)
        for text in ("not queued", "Prefer this", "50 seconds", "60-second"):
            self.assertIn(text, tool.description)
        result = await server.call_tool(
            "wait_for_research", {"technology": "automation", "timeout_seconds": 50}
        )
        self.assertFalse(result.is_error)
        self.assertTrue(payload(result)["researched"])
        bridge.call.reset_mock()
        for timeout in (0, 51, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ToolError):
                await server.call_tool(
                    "wait_for_research", {"technology": "automation", "timeout_seconds": timeout}
                )
        with self.assertRaises(ToolError):
            await server.call_tool("wait_for_research", {"technology": ""})
        bridge.call.assert_not_called()

    async def test_move_tool_description_explains_arrival_and_failure_distance(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        description = tools["move_to"].description
        for text in (
            "within 0.5 tiles",
            "goal is blocked",
            "succeeds within 1.5 tiles",
            "detail.goal_blocked = true",
            "Blocked or timeout failures include detail.distance",
            "detail.goal",
        ):
            with self.subTest(text=text):
                self.assertIn(text, description)

    async def test_move_tool_description_explains_path_not_found_detail(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        description = tools["move_to"].description
        for text in (
            "path_not_found",
            "goal_tiles",
            "goal_entities",
            "nearby_free_position",
            "standable spot",
            "no path guarantee",
        ):
            with self.subTest(text=text):
                self.assertIn(text, description)

    async def test_path_not_found_hint_and_detail(self):
        bridge = Mock()
        detail = {
            "goal": {"x": -10, "y": -10},
            "distance": 20,
            "goal_tiles": ["water"],
            "goal_entities": [{"name": "stone-wall", "type": "wall", "x": -10, "y": -10}],
            "nearby_free_position": {"x": -7, "y": -10},
        }
        bridge.call.side_effect = BridgeError("path_not_found", detail=detail)
        result = await create_server(bridge).call_tool(
            "move_to", {"surface": "nauvis", "x": -10, "y": -10}
        )
        self.assertTrue(result.is_error)
        value = payload(result)
        self.assertEqual(value["error"], "path_not_found")
        self.assertEqual(value["detail"], detail)
        self.assertEqual(value["hint"], HINTS["path_not_found"])
        for text in (
            "goal_tiles",
            "goal_entities",
            "explaining the failure",
            "nearby_free_position",
            "standable spot to try with move_to",
            "no path guarantee",
        ):
            with self.subTest(text=text):
                self.assertIn(text, value["hint"])

    async def test_placement_tool_descriptions_explain_preview_metadata(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name in ("check_placement", "place_entity", "observe_nearby"):
            with self.subTest(name=name):
                for field in ("snapped position", "fluid_connections", "valid_directions"):
                    self.assertIn(field, tools[name].description)
        self.assertIn("connected", tools["observe_nearby"].description)
        self.assertIn("blocked_reason", tools["check_placement"].description)

    async def test_placement_descriptions_and_hint_explain_recent_observations(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name, description in (
            ("check_placement", tools["check_placement"].description),
            ("place_entity", tools["place_entity"].description),
            ("not_visible hint", HINTS["not_visible"]),
        ):
            with self.subTest(name=name):
                for text in ("any of the last 8 observations", "within 60 seconds"):
                    self.assertIn(text, description.lower())

    async def test_placement_tool_descriptions_explain_blocked_reasons(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name, field in (
            ("check_placement", "blocked_reason"),
            ("place_entity", "detail.reason"),
        ):
            with self.subTest(name=name):
                description = tools[name].description
                for text in (
                    field,
                    "character_in_the_way",
                    "move the character away first",
                    "placement_rules",
                    "no blocking entities, tiles, or character",
                ):
                    self.assertIn(text, description)

    async def test_blocking_descriptions_explain_mining_refs(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name in ("check_placement", "place_entity"):
            with self.subTest(name=name):
                for text in ("blocking", "target_ref", "mine_entity", "60 seconds"):
                    self.assertIn(text, tools[name].description)

    async def test_placement_descriptions_and_hint_explain_nearby_positions(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name, text in (
            ("check_placement", tools["check_placement"].description),
            ("place_entity", tools["place_entity"].description),
            ("blocked hint", HINTS["blocked"]),
        ):
            with self.subTest(name=name):
                for advice in (
                    "placement_rules",
                    "nearby_positions",
                    "up to 3 placeable centres and directions",
                    "within 8 tiles of the requested position",
                    "may be out of reach",
                    "move closer and check again with check_placement",
                ):
                    self.assertIn(advice, text)

    async def test_nearby_positions_survive_placement_results_and_errors(self):
        nearby = [{"x": -9.5, "y": -6.5, "direction": 0}]
        bridge = Mock()
        server = create_server(bridge)
        params = {"item": "offshore-pump", "surface": "nauvis", "x": -10, "y": -4}
        bridge.call.return_value = {
            "result": {
                "placeable": False,
                "blocked_reason": "placement_rules",
                "nearby_positions": nearby,
            }
        }
        checked = payload(await server.call_tool("check_placement", params))
        self.assertFalse(checked["placeable"])
        self.assertEqual(checked["blocked_reason"], "placement_rules")
        self.assertEqual(checked["nearby_positions"], nearby)
        detail = {"reason": "placement_rules", "nearby_positions": nearby}
        bridge.call.side_effect = BridgeError("blocked", detail=detail)
        placed = payload(await server.call_tool("place_entity", params))
        self.assertEqual(placed["detail"], detail)
        self.assertEqual(placed["hint"], HINTS["blocked"])

    async def test_craft_shortage_description_and_hint(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for text in (
            "detail.missing_raw",
            "raw items such as plates",
            "hand-craftable intermediates",
            "held materials first",
        ):
            with self.subTest(text=text):
                self.assertIn(text, tools["craft"].description)
                self.assertIn(text, HINTS["missing_materials"])
        self.assertIn("missing_materials", tools["craft"].description)

    async def test_craft_shortage_normalizes_only_present_empty_raw_materials(self):
        bridge = Mock()
        server = create_server(bridge)
        missing = [{"name": "iron-gear-wheel", "count": 10}]
        for raw in (None, {}, [], [{"name": "iron-plate", "count": 20}]):
            with self.subTest(raw=raw):
                detail = {
                    "recipe": "automation-science-pack",
                    "count": 10,
                    "missing": missing,
                    "best_effort": True,
                }
                if raw is not None:
                    detail["missing_raw"] = raw
                bridge.call.side_effect = BridgeError("missing_materials", detail=detail)
                result = await server.call_tool(
                    "craft", {"recipe": "automation-science-pack", "count": 10}
                )
                self.assertTrue(result.is_error)
                value = payload(result)
                self.assertEqual(value["error"], "missing_materials")
                self.assertEqual(value["hint"], HINTS["missing_materials"])
                expected = dict(detail)
                if raw == {}:
                    expected["missing_raw"] = []
                self.assertEqual(value["detail"], expected)
                self.assertEqual(detail.get("missing_raw"), raw)

    async def test_transfer_shortage_description_hint_and_detail(self):
        bridge = Mock()
        server = create_server(bridge)
        tools = {tool.name: tool for tool in await server.list_tools()}
        for text in ("missing_materials", "requested", "available", "contents", "source inventory"):
            self.assertIn(text, tools["transfer_item"].description)
        for direction in ("from_entity", "to_entity"):
            for contents in (None, {}, [{"name": "copper-plate", "count": 3, "quality": "normal"}]):
                with self.subTest(direction=direction, contents=contents):
                    detail = {"item": "iron-plate", "requested": 5, "available": 0}
                    if direction == "from_entity":
                        detail["inventory_role"] = "result"
                    if contents is not None:
                        detail["contents"] = contents
                    bridge.call.side_effect = BridgeError("missing_materials", detail=detail)
                    result = await server.call_tool(
                        "transfer_item",
                        {
                            "target_ref": "42",
                            "direction": direction,
                            "inventory_role": "result",
                            "item": "iron-plate",
                            "count": 5,
                        },
                    )
                    self.assertTrue(result.is_error)
                    value = payload(result)
                    expected = dict(detail)
                    if contents == {}:
                        expected["contents"] = []
                    self.assertEqual(value["detail"], expected)
                    self.assertEqual(value["hint"], HINTS["missing_materials"])
                    self.assertIn("detail.contents", value["hint"])
                    self.assertIn("transfers", value["hint"])
                    self.assertEqual(detail.get("contents"), contents)

    async def test_blocking_refs_survive_success_and_error_normalization(self):
        blocking = [{"name": "item-on-ground", "type": "item-entity", "target_ref": "42"}]
        bridge = Mock()
        server = create_server(bridge)
        params = {"item": "stone-furnace", "surface": "nauvis", "x": 1, "y": 2}
        bridge.call.return_value = {"result": {"placeable": False, "blocking": blocking}}
        checked = payload(await server.call_tool("check_placement", params))
        self.assertEqual(checked["blocking"], blocking)
        bridge.call.side_effect = BridgeError("blocked", detail={"blocking": blocking})
        placed = payload(await server.call_tool("place_entity", params))
        self.assertEqual(placed["detail"]["blocking"], blocking)

    async def test_blocked_character_hint_explains_moving_away(self):
        bridge = Mock()
        detail = {"reason": "character_in_the_way", "blocking": {}, "tiles": {}}
        bridge.call.side_effect = BridgeError("blocked", detail=detail)
        result = await create_server(bridge).call_tool(
            "place_entity", {"item": "stone-furnace", "surface": "nauvis", "x": 1, "y": 2}
        )
        self.assertTrue(result.is_error)
        value = payload(result)
        self.assertEqual(
            value["detail"], {"reason": "character_in_the_way", "blocking": [], "tiles": []}
        )
        self.assertEqual(value["hint"], HINTS["blocked"])
        self.assertIn("character_in_the_way", value["hint"])
        self.assertIn("move the character away first", value["hint"])

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
