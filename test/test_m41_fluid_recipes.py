import copy
import json
import unittest
from unittest.mock import Mock

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import create_server


def payload(response):
    return json.loads(response.content[0].text)


PORT = {
    "position": {"x": 0.5, "y": 1.5},
    "target_position": {"x": 0.5, "y": 2.5},
    "direction": 8,
    "flow": "input",
    "fluidbox_index": 1,
    "connected": True,
    "connection_type": "normal",
    "fluid": "crude-oil",
    "production_type": "input",
    "connected_to": {"target_ref": "adjacent", "name": "pipe"},
}
MISSING = [{"name": "crude-oil", "amount": 75.5}]


class FluidRecipeTests(unittest.IsolatedAsyncioTestCase):
    async def test_descriptions_explain_ports_diagnostics_and_mixing(self):
        tools = {t.name: t for t in await create_server(None).list_tools()}
        for operation in (
            "place_entity",
            "check_placement",
            "set_recipe",
            "get_entity_inventory",
            "get_entity_inventories",
            "observe_nearby",
        ):
            for phrase in ("fluid_connections", "fluid", "production_type"):
                self.assertIn(phrase, tools[operation].description)
        for operation in ("get_entity_inventory", "get_entity_inventories", "observe_nearby"):
            for phrase in ("missing_fluids", "full_fluids", "connected_to"):
                self.assertIn(phrase, tools[operation].description)
        for operation in ("get_entity_inventory", "get_entity_inventories"):
            self.assertIn("unconnected_ports", tools[operation].description)
        for operation in ("place_entity", "check_placement"):
            self.assertIn("fluid_mixing", tools[operation].description)
        self.assertIn("missing_fluids", tools["observe_nearby"].description)
        self.assertIn("recipe-dependent fluid is omitted", tools["check_placement"].description)
        self.assertNotIn("recipe", tools["check_placement"].input_schema["properties"])

    async def test_ports_survive_placement_recipe_and_inventory_reads(self):
        bridge = Mock()
        server = create_server(bridge)
        value = {
            "name": "oil-refinery",
            "fluid_connections": [PORT],
            "missing_fluids": MISSING,
            "full_fluids": ["petroleum-gas"],
        }
        for operation, args in (
            ("place_entity", {"item": "oil-refinery", "surface": "nauvis", "x": 0, "y": 0}),
            ("check_placement", {"item": "oil-refinery", "surface": "nauvis", "x": 0, "y": 0}),
            ("set_recipe", {"target_ref": "refinery", "recipe": "basic-oil-processing"}),
            ("get_entity_inventory", {"target_ref": "refinery"}),
            ("get_entity_inventories", {"target_refs": ["refinery"]}),
        ):
            with self.subTest(operation=operation):
                measured = (
                    value
                    if operation != "get_entity_inventories"
                    else {"results": [{"index": 1, "ok": True, "result": value}]}
                )
                bridge.call.return_value = {"result": copy.deepcopy(measured)}
                response = await server.call_tool(operation, args)
                self.assertFalse(response.is_error, payload(response))
                result = payload(response)
                if operation == "get_entity_inventories":
                    result = result["results"][0]["result"]
                for field in ("fluid_connections", "missing_fluids", "full_fluids"):
                    self.assertEqual(result[field], value[field])

    async def test_pipe_box_connectivity_and_empty_lua_lists(self):
        bridge = Mock()
        server = create_server(bridge)
        for name in ("pipe", "pipe-to-ground", "pump", "storage-tank"):
            for connected in (True, False):
                with self.subTest(name=name, connected=connected):
                    targets = [PORT["connected_to"]] if connected else {}
                    box = {
                        "index": 1,
                        "amount": 0,
                        "capacity": 100,
                        "connected_to": targets,
                        "unconnected_ports": 3 if connected else 4,
                    }
                    value = {"name": name, "fluids": [box], "fluid_connections": [PORT]}
                    for operation, args, measured in (
                        ("get_entity_inventory", {"target_ref": "pipe"}, value),
                        (
                            "get_entity_inventories",
                            {"target_refs": ["pipe"]},
                            {"results": [{"index": 1, "ok": True, "result": value}]},
                        ),
                    ):
                        bridge.call.return_value = {"result": copy.deepcopy(measured)}
                        response = await server.call_tool(operation, args)
                        self.assertFalse(response.is_error, payload(response))
                        result = payload(response)
                        if operation == "get_entity_inventories":
                            result = result["results"][0]["result"]
                        self.assertEqual(result["fluids"][0]["connected_to"], targets or [])
                        self.assertEqual(
                            result["fluids"][0]["unconnected_ports"], box["unconnected_ports"]
                        )

    async def test_compact_and_full_observation_preserve_missing_fluids(self):
        bridge = Mock()
        server = create_server(bridge)
        for compact in (False, True):
            value = {
                "target_ref": "starved",
                "name": "oil-refinery",
                "status": "fluid_ingredient_shortage",
                "missing_fluids": MISSING,
            }
            if not compact:
                value.update(fluid_connections=[PORT], full_fluids=["petroleum-gas"])
            bridge.call.return_value = {"result": {"entities": [copy.deepcopy(value)]}}
            result = payload(await server.call_tool("observe_nearby", {"compact": compact}))
            self.assertEqual(result["entities"], [value])

    async def test_mixing_reason_names_and_hint_survive_errors_and_preview(self):
        detail = {
            "reason": "fluid_mixing",
            "fluids": ["crude-oil", "water"],
            "hint": "Keep different fluids in separate pipe networks.",
        }
        bridge = Mock()
        server = create_server(bridge)
        args = {"item": "pipe", "surface": "nauvis", "x": 0, "y": 0}
        bridge.call.return_value = {
            "result": {"placeable": False, "blocked_reason": "fluid_mixing", **detail}
        }
        result = payload(await server.call_tool("check_placement", args))
        self.assertEqual(result["fluids"], detail["fluids"])
        self.assertEqual(result["blocked_reason"], "fluid_mixing")
        bridge.call.side_effect = BridgeError("blocked", detail=detail)
        response = await server.call_tool("place_entity", args)
        self.assertTrue(response.is_error)
        self.assertEqual(payload(response)["detail"], detail)
        self.assertEqual(payload(response)["hint"], detail["hint"])

    async def test_unknown_fluid_and_absent_diagnostics_are_not_invented(self):
        bridge = Mock()
        port = {k: v for k, v in PORT.items() if k not in ("fluid", "connected_to")}
        port["connected"] = False
        bridge.call.return_value = {"result": {"fluid_connections": [port]}}
        result = payload(
            await create_server(bridge).call_tool(
                "check_placement", {"item": "oil-refinery", "surface": "nauvis", "x": 0, "y": 0}
            )
        )
        self.assertNotIn("fluid", result["fluid_connections"][0])
        self.assertNotIn("connected_to", result["fluid_connections"][0])
        self.assertNotIn("missing_fluids", result)
        self.assertNotIn("full_fluids", result)
