"""Manual failure-path checks for a disposable save on RCON port 27277 only.

Run right after production_line.py on the same server: it relies on the
fixture's stone patch and the running stone furnace at (6, 4).
"""

import asyncio
import json
import math
import time

from rcon.source import Client

from factorio_mcp.bridge import RCON_FRAG_THRESHOLD, BridgeError, parse_bridge_response
from factorio_mcp.server import create_server
from production_line import action, bridge, password, reference

results = []
errors = {}


def record(label, passed, info=""):
    results.append((label, bool(passed), info))
    print(("PASS " if passed else "FAIL ") + label + (f"  [{info}]" if info else ""), flush=True)


def expect_error(label, operation, params, codes):
    try:
        result = bridge.call(operation, params)["result"]
    except BridgeError as error:
        errors[label] = error
        record(label, error.code in codes, error.code)
        return
    record(label, False, "unexpected success: " + json.dumps(result, ensure_ascii=False)[:200])


def items():
    result = bridge.call("get_inventory")["result"]
    return {item["name"]: item["count"] for item in result["items"]}


def count_visible(name):
    entities = bridge.call("observe_nearby")["result"]["entities"]
    return sum(1 for entity in entities if entity["name"] == name)


def send_with_id(operation, params, request_id):
    # Bypass FactorioBridge.call so the same request_id can be sent twice.
    request = {
        "protocol_version": 1,
        "request_id": request_id,
        "agent_id": "agent-1",
        "operation": operation,
        "params": params,
    }
    command = "/ai_mcp " + json.dumps(request, separators=(",", ":"))
    raw = bridge.send(bridge.host, bridge.port, bridge.password, command)
    return parse_bridge_response(raw, request_id)


def main():
    before = items()
    expect_error(
        "craft with missing materials",
        "craft",
        {"recipe": "burner-mining-drill", "count": 1},
        {"missing_materials"},
    )
    record("failed craft keeps inventory", items() == before)

    # A spare furnace is needed for the occupied-tile check; the drill recipe consumed the last one.
    action("move_to", {"surface": "nauvis", "x": 2, "y": 2.5})
    action("mine_entity", {"target_ref": reference("stone"), "count": 5})
    action("craft", {"recipe": "stone-furnace", "count": 1})
    action("move_to", {"surface": "nauvis", "x": 4, "y": 4})

    # place_entity requires an observation from the last 60 seconds.
    before = items()
    bridge.call("observe_nearby")
    expect_error(
        "place an item not in inventory",
        "place_entity",
        {"item": "wooden-chest", "surface": "nauvis", "x": 0, "y": 8, "direction": 0},
        {"missing_materials"},
    )
    furnaces = count_visible("stone-furnace")
    expect_error(
        "place on an occupied tile",
        "place_entity",
        {"item": "stone-furnace", "surface": "nauvis", "x": 6, "y": 4, "direction": 0},
        {"blocked"},
    )
    record(
        "failed placements keep inventory and entities",
        items() == before and count_visible("stone-furnace") == furnaces,
        f"furnaces {furnaces}",
    )

    expect_error(
        "unknown target_ref", "mine_entity", {"target_ref": "999999", "count": 1}, {"target_gone"}
    )

    furnace = reference("stone-furnace")
    request_id = f"negative-resend-{int(time.time())}"
    params = {
        "target_ref": furnace,
        "direction": "from_entity",
        "inventory_role": "result",
        "item": "iron-plate",
        "count": 1,
    }
    plates = items().get("iron-plate", 0)
    first = send_with_id("transfer_item", params, request_id)
    after_first = items().get("iron-plate", 0)
    second = send_with_id("transfer_item", params, request_id)
    after_second = items().get("iron-plate", 0)
    record("first transfer moves one plate", after_first - plates == 1, f"{plates}->{after_first}")
    record(
        "resent request is not executed again",
        after_second == after_first,
        f"{after_first}->{after_second}",
    )
    record("resent request returns the cached response", first == second)
    stored = bridge.call("get_request_result", {"request_id": request_id})["result"]
    record(
        "get_request_result returns the original response", stored.get("original_response") == first
    )

    action("move_to", {"surface": "nauvis", "x": 13, "y": 4})
    before = items()
    expect_error(
        "mine out of reach",
        "mine_entity",
        {"target_ref": reference("stone"), "count": 1},
        {"out_of_range"},
    )
    record("out-of-reach mining keeps inventory", items() == before)

    time.sleep(61)
    expect_error(
        "expired target_ref", "get_entity_inventory", {"target_ref": furnace}, {"target_gone"}
    )

    # M6 checks are appended so the original checks and their output order stay intact.
    action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
    furnace = reference("stone-furnace")
    try:
        bridge.call(
            "transfer_item",
            {
                "target_ref": furnace,
                "direction": "to_entity",
                "inventory_role": "input",
                "item": "iron-ore",
                "count": 1,
            },
        )
    except BridgeError as error:
        detail = error.detail or {}
        record(
            "wrong furnace role reports valid_roles",
            error.code == "invalid_target"
            and detail.get("entity") == "stone-furnace"
            and detail.get("valid_roles") == ["fuel", "result", "source"],
        )
    else:
        record("wrong furnace role reports valid_roles", False, "unexpected success")

    blocking = (getattr(errors.get("place on an occupied tile"), "detail", None) or {}).get(
        "blocking", []
    )
    checked = bridge.call(
        "check_placement", {"item": "stone-furnace", "surface": "nauvis", "x": 6, "y": 4}
    )["result"]
    record(
        "occupied placement reports blocking furnace",
        any(entity["name"] == "stone-furnace" for entity in blocking)
        and checked["placeable"] is False
        and checked["reason"] == "blocked"
        and any(entity["name"] == "stone-furnace" for entity in checked.get("blocking", [])),
    )
    distance = getattr(errors.get("mine out of reach"), "detail", None) or {}
    record(
        "out-of-reach mine reports distance and reach",
        isinstance(distance.get("distance"), (int, float))
        and isinstance(distance.get("reach"), (int, float))
        and distance["distance"] > distance["reach"],
    )
    expired = getattr(errors.get("expired target_ref"), "detail", None) or {}
    record("expired target_ref reports reason", expired.get("reason") == "expired")

    all_nearby = bridge.call("observe_nearby", {"radius": 8})["result"]
    filtered = bridge.call(
        "observe_nearby",
        {
            "types": ["furnace"],
            "owned_only": True,
            "include_resources": False,
            "include_tiles": False,
            "radius": 8,
        },
    )["result"]
    entities = filtered["entities"]
    filters = filtered.get("filters", {})
    # The running drill can change total_amount between the two observations.
    resource_names = {resource["name"] for resource in all_nearby["resources"]}
    record(
        "observation filters preserve resource summary",
        bool(entities)
        and all(entity["type"] == "furnace" and entity["owned"] for entity in entities)
        and all(
            (entity["x"] - filtered["position"]["x"]) ** 2
            + (entity["y"] - filtered["position"]["y"]) ** 2
            <= 8**2
            for entity in entities
        )
        and filtered["entity_total"] == len(entities)
        and filtered["radius"] == 8
        and filters.get("types") == ["furnace"]
        and filters.get("owned_only") is True
        and filters.get("include_resources") is False
        and filters.get("include_tiles") is False
        and "resources" in filtered
        and bool(filtered["resources"])
        and {resource["name"] for resource in filtered["resources"]} == resource_names
        and all(resource["total_amount"] > 0 for resource in filtered["resources"])
        and not filtered["blocked_tiles"]
        and filtered["blocked_tile_total"] == 0
        and "next_tile_offset" not in filtered,
    )

    started = bridge.call("move_to", {"surface": "nauvis", "x": 4, "y": 3})["result"]
    waited = asyncio.run(
        create_server(bridge).call_tool("wait_for_action", {"action_id": started["action_id"]})
    )
    value = json.loads(waited.content[0].text)
    record(
        "MCP wait_for_action completes a short move",
        not waited.is_error
        and value.get("status") == "succeeded"
        and value.get("timed_out") is False
        and value.get("action_id") == started["action_id"],
    )

    # M7-B checks run within reach of the furnace at (6, 4).
    action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
    observed = bridge.call("observe_nearby", {"types": ["furnace"], "owned_only": True})["result"]
    record(
        "observation lists furnace inventory_roles",
        any(
            entity["name"] == "stone-furnace"
            and entity.get("inventory_roles") == ["fuel", "result", "source"]
            for entity in observed["entities"]
        ),
    )
    furnace = reference("stone-furnace")
    time.sleep(30)
    try:
        bridge.call("get_entity_inventory", {"target_ref": furnace})
    except BridgeError as error:
        record("target_ref survives 30 seconds", False, error.code)
    else:
        record("target_ref survives 30 seconds", True)

    # M7-C: a north-facing 3x2 boiler snaps to half-tile x and integer y.
    current = bridge.call("get_agent_status")["result"]["position"]
    bridge.call("observe_nearby")
    requested = {"x": current["x"] + 0.3, "y": current["y"] + 0.3}
    checked = bridge.call(
        "check_placement",
        {"item": "boiler", "surface": current["surface"], **requested, "direction": 0},
    )["result"]
    expected = {"x": math.floor(requested["x"]) + 0.5, "y": math.floor(requested["y"] + 0.5)}
    record(
        "boiler placement snaps position",
        checked.get("position") == expected,
        json.dumps({"requested": requested, "position": checked.get("position")}),
    )

    # M8-D: a tree trunk allows a path but blocks arrival at its exact center.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    goal = {"x": 0, "y": -4}
    fixture = (
        "/c local s=game.surfaces.nauvis;"
        f"local p={{x={goal['x']},y={goal['y']}}};local e;"
        'if s.can_place_entity{name="tree-01",position=p} then '
        'e=s.create_entity{name="tree-01",position=p} end;'
        'rcon.print("m8d_tree_fixture:"..(e and "created" or "blocked"))'
    )
    with Client(
        "127.0.0.1", 27277, passwd=password, timeout=5, frag_threshold=RCON_FRAG_THRESHOLD
    ) as client:
        result = client.run(fixture)
        if "m8d_tree_fixture:" not in result:
            result = client.run(fixture)
        print(result.strip(), flush=True)
        if "m8d_tree_fixture:created" not in result:
            raise RuntimeError("Could not create M8-D tree fixture: " + result)

    action("move_to", {"surface": return_position["surface"], "x": 0, "y": 2})
    started = bridge.call(
        "move_to",
        {"surface": return_position["surface"], **goal},
    )["result"]
    wait_started = time.monotonic()
    waited = asyncio.run(
        create_server(bridge).call_tool(
            "wait_for_action", {"action_id": started["action_id"], "timeout_seconds": 15}
        )
    )
    elapsed = time.monotonic() - wait_started
    value = json.loads(waited.content[0].text)
    detail = value.get("detail") or {}
    record(
        "move to a blocked goal finishes nearby",
        not waited.is_error
        and value.get("status") == "succeeded"
        and detail.get("goal_blocked") is True
        and isinstance(detail.get("distance"), (int, float))
        and detail["distance"] <= 1.5
        and elapsed < 10,
        json.dumps({"goal": goal, "result": value, "elapsed": elapsed}),
    )
    if value.get("status") == "running":
        bridge.call("cancel_action", {"action_id": started["action_id"]})
    action("move_to", return_position)

    # M8-E: preview collisions without consuming the spare furnace or moving the character.
    before = items()
    if before.get("stone-furnace", 0) < 1:
        raise RuntimeError(
            "M8-E checks require the spare stone furnace from the occupied-tile check"
        )
    current = bridge.call("get_agent_status")["result"]["position"]
    bridge.call("observe_nearby")
    snapped = {axis: math.floor(current[axis] + 0.5) for axis in ("x", "y")}
    response = bridge.call(
        "check_placement",
        {"item": "stone-furnace", "surface": current["surface"], **snapped},
    )
    checked = response["result"]
    record(
        "placement under the character reports character_in_the_way",
        response.get("ok") is True
        and checked.get("placeable") is False
        and checked.get("reason") == "blocked"
        and checked.get("blocked_reason") == "character_in_the_way"
        and checked.get("has_item") is True
        and not any(entity["type"] == "character" for entity in checked.get("blocking", []))
        and items() == before
        and bridge.call("get_agent_status")["result"]["position"] == current,
        json.dumps(checked),
    )

    # The production fixture's drill at (6, 6) overlaps iron ore; (4, 4) stays clear of it.
    action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
    observed = bridge.call("observe_nearby", {"types": ["mining-drill", "resource"], "radius": 8})[
        "result"
    ]
    drill = next(
        entity
        for entity in observed["entities"]
        if entity["name"] == "burner-mining-drill"
        and entity["owned"]
        and entity["x"] == 6
        and entity["y"] == 6
    )
    ore_under_drill = any(
        entity["type"] == "resource"
        and abs(entity["x"] - drill["x"]) < 1
        and abs(entity["y"] - drill["y"]) < 1
        for entity in observed["entities"]
    )
    response = bridge.call(
        "check_placement",
        {"item": "stone-furnace", "surface": "nauvis", "x": drill["x"], "y": drill["y"]},
    )
    checked = response["result"]
    blocking = checked.get("blocking", [])
    record(
        "placement on ore lists no resources",
        ore_under_drill
        and response.get("ok") is True
        and checked.get("placeable") is False
        and checked.get("reason") == "blocked"
        and checked.get("blocked_reason") != "character_in_the_way"
        and any(entity["name"] == "burner-mining-drill" for entity in blocking)
        and all(entity["type"] != "resource" for entity in blocking),
        json.dumps(checked),
    )

    # M8-F: coal starts at 100, with only 3 hand-mined earlier and no drill on this tile.
    action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
    observed = bridge.call("observe_nearby", {"names": ["coal"]})["result"]
    coal = next(
        entity
        for entity in observed["entities"]
        if entity["name"] == "coal" and entity["x"] == 3.5 and entity["y"] == 5.5
    )
    started = bridge.call("mine_entity", {"target_ref": coal["target_ref"], "count": 10})["result"]
    waited = asyncio.run(
        create_server(bridge).call_tool(
            "wait_for_action", {"action_id": started["action_id"], "timeout_seconds": 1}
        )
    )
    value = json.loads(waited.content[0].text)
    detail = value.get("detail") or {}
    record(
        "running mining reports progress",
        not waited.is_error
        and value.get("status") == "running"
        and value.get("timed_out") is True
        and detail.get("count") == 10
        and isinstance(detail.get("gained"), (int, float))
        and 0 <= detail["gained"] < 10,
        json.dumps(value),
    )
    finished = asyncio.run(
        create_server(bridge).call_tool("wait_for_action", {"action_id": started["action_id"]})
    )
    value = json.loads(finished.content[0].text)
    record("progress mining finishes", not finished.is_error and value.get("status") == "succeeded")
    if value.get("status") == "running":
        bridge.call("cancel_action", {"action_id": started["action_id"]})

    # M9-G: a water goal reports its blocking tile and a nearby standable position.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    goal = {"x": -10, "y": -10}

    def set_goal_tiles(name):
        fixture = (
            "/c local s=game.surfaces.nauvis;local tiles={};"
            "for x=-12,-8 do for y=-12,-8 do "
            f'tiles[#tiles+1]={{name="{name}",position={{x=x,y=y}}}} end end;'
            "s.set_tiles(tiles);local ok=true;"
            "for _,t in ipairs(tiles) do "
            "if s.get_tile(t.position).name~=t.name then ok=false end end;"
            f'rcon.print("m9g_tiles_{name}:"..(ok and "success" or "failed"))'
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=5, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            result = client.run(fixture)
            if f"m9g_tiles_{name}:" not in result:
                result = client.run(fixture)
            print(result.strip(), flush=True)
            if f"m9g_tiles_{name}:success" not in result:
                raise RuntimeError("Could not set M9-G tile fixture: " + result)

    started = None
    try:
        set_goal_tiles("water")
        started = bridge.call("move_to", {"surface": return_position["surface"], **goal})["result"]
        waited = asyncio.run(
            create_server(bridge).call_tool(
                "wait_for_action", {"action_id": started["action_id"], "timeout_seconds": 15}
            )
        )
        value = json.loads(waited.content[0].text)
        detail = value.get("detail") or {}
        free = detail.get("nearby_free_position")
        record(
            "unreachable water goal explains path_not_found",
            value.get("status") == "failed"
            and value.get("error") == "path_not_found"
            and "water" in detail.get("goal_tiles", [])
            and isinstance(free, dict)
            and isinstance(free.get("x"), (int, float))
            and isinstance(free.get("y"), (int, float))
            and math.hypot(free["x"] - goal["x"], free["y"] - goal["y"]) >= 2,
            json.dumps(value),
        )
    finally:
        if started is not None:
            current = bridge.call("get_action_status", {"action_id": started["action_id"]})[
                "result"
            ]
            if current.get("status") == "running":
                bridge.call("cancel_action", {"action_id": started["action_id"]})
        set_goal_tiles("grass-1")
        action("move_to", return_position)

    # M9-H: use the spare furnace without fuel so production cannot race the empty-result check.
    # logistics_line.py needs the original furnace and drill at (6, 4) and (6, 6) for pickup.
    action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
    before = items()
    bridge.call("observe_nearby")
    spare_position = {"surface": "nauvis", "x": 1, "y": 6}
    bridge.call("place_entity", {"item": "stone-furnace", **spare_position})
    observed = bridge.call("observe_nearby", {"names": ["stone-furnace"], "owned_only": True})[
        "result"
    ]
    spare = next(
        entity
        for entity in observed["entities"]
        if entity["x"] == spare_position["x"] and entity["y"] == spare_position["y"]
    )
    inventories = bridge.call("get_entity_inventory", {"target_ref": spare["target_ref"]})[
        "result"
    ]["inventories"]
    if any(item["name"] == "iron-plate" for item in inventories["result"]):
        raise RuntimeError("M9-H spare furnace unexpectedly has iron plates")
    try:
        bridge.call(
            "transfer_item",
            {
                "target_ref": spare["target_ref"],
                "direction": "from_entity",
                "inventory_role": "result",
                "item": "iron-plate",
                "count": 5,
            },
        )
    except BridgeError as error:
        detail = error.detail or {}
        record(
            "transfer from an empty result explains shortage",
            error.code == "missing_materials"
            and detail.get("item") == "iron-plate"
            and detail.get("requested") == 5
            and detail.get("available") == 0
            and detail.get("inventory_role") == "result"
            and ("contents" not in detail or isinstance(detail["contents"], list)),
            json.dumps(detail),
        )
    else:
        record("transfer from an empty result explains shortage", False, "unexpected success")

    checked = bridge.call("check_placement", {"item": "stone-furnace", **spare_position})["result"]
    blocking = checked.get("blocking", [])
    refs_present = bool(blocking) and all(
        isinstance(entity.get("target_ref"), str) and bool(entity["target_ref"])
        for entity in blocking
    )
    target = next(
        (
            entity
            for entity in blocking
            if entity["name"] == "stone-furnace"
            and entity["x"] == spare_position["x"]
            and entity["y"] == spare_position["y"]
        ),
        None,
    )
    # Recover only the temporary furnace; a missing blocking ref still fails the check.
    mining_ref = target["target_ref"] if refs_present and target else spare["target_ref"]
    action("mine_entity", {"target_ref": mining_ref, "count": 1})
    remaining = bridge.call("observe_nearby", {"names": ["stone-furnace"], "owned_only": True})[
        "result"
    ]["entities"]
    record(
        "blocking target_ref can be mined",
        checked.get("placeable") is False
        and checked.get("reason") == "blocked"
        and len(blocking) <= 10
        and refs_present
        and target is not None
        and not any(
            entity["x"] == spare_position["x"] and entity["y"] == spare_position["y"]
            for entity in remaining
        )
        and items() == before,
        json.dumps(checked),
    )

    # M10-I: use real plates from the running furnace; keep wood for later scripts.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    craft_count = 4  # Two seconds of crafting leaves time to start walking and test the conflict.
    before = items()
    needed = 2 * craft_count - before.get("iron-plate", 0)
    if needed > 0:
        bridge.call(
            "transfer_item",
            {
                "target_ref": reference("stone-furnace"),
                "direction": "from_entity",
                "inventory_role": "result",
                "item": "iron-plate",
                "count": needed,
            },
        )
    before = items()
    if before.get("iron-plate", 0) < 2 * craft_count:
        raise RuntimeError("M10-I needs eight normally smelted iron plates")
    started_actions = []
    server = create_server(bridge)
    try:
        crafting = bridge.call("craft", {"recipe": "iron-gear-wheel", "count": craft_count})[
            "result"
        ]
        started_actions.append(crafting["action_id"])
        # The production fixture cleared this route; the drill and furnace are east of it.
        walking = bridge.call(
            "move_to",
            {**return_position, "y": return_position["y"] - 6},
        )["result"]
        started_actions.append(walking["action_id"])
        record(
            "crafting runs while walking",
            crafting.get("status") == "running"
            and walking.get("status") == "running"
            and crafting["action_id"] != walking["action_id"],
        )
        try:
            bridge.call("craft", {"recipe": "iron-gear-wheel", "count": 1})
        except BridgeError as error:
            running = (error.detail or {}).get("running_action") or {}
            record(
                "second craft conflicts with the running craft",
                error.code == "action_conflict"
                and running.get("type") == "craft"
                and running.get("id") == crafting["action_id"],
                json.dumps(error.detail),
            )
        else:
            record("second craft conflicts with the running craft", False, "unexpected success")
        for label, started in (("concurrent craft", crafting), ("concurrent walk", walking)):
            waited = asyncio.run(
                server.call_tool("wait_for_action", {"action_id": started["action_id"]})
            )
            value = json.loads(waited.content[0].text)
            record(
                label + " succeeds",
                not waited.is_error and value.get("status") == "succeeded",
                json.dumps(value),
            )
        after = items()
        record(
            "concurrent craft produces gears and consumes plates",
            after.get("iron-gear-wheel", 0) == before.get("iron-gear-wheel", 0) + craft_count
            and after.get("iron-plate", 0) == before.get("iron-plate", 0) - 2 * craft_count,
        )
    finally:
        for action_id in started_actions:
            current = bridge.call("get_action_status", {"action_id": action_id})["result"]
            if current.get("status") == "running":
                bridge.call("cancel_action", {"action_id": action_id})
        action("move_to", return_position)

    # M10-J: check_placement uses require_item=false, so no pump item is needed.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    land = {"surface": "nauvis", "x": -10, "y": -4}

    def water_edge_distance(position):
        # The fixture's tiles (-12..-8) occupy the square [-12, -7] on each axis.
        x, y = position["x"], position["y"]
        nearest_x, nearest_y = max(-12, min(-7, x)), max(-12, min(-7, y))
        return min(
            math.hypot(x - nearest_x, y + 12),
            math.hypot(x - nearest_x, y + 7),
            math.hypot(x + 12, y - nearest_y),
            math.hypot(x + 7, y - nearest_y),
        )

    try:
        set_goal_tiles("water")
        action("move_to", {**land, "y": -2})
        bridge.call("observe_nearby")
        checked = bridge.call("check_placement", {"item": "offshore-pump", **land})["result"]
        nearby = checked.get("nearby_positions", [])
        record(
            "offshore pump on land suggests shoreline positions",
            checked.get("placeable") is False
            and checked.get("blocked_reason") == "placement_rules"
            and isinstance(nearby, list)
            and 1 <= len(nearby) <= 3
            and all(
                position.get("direction") in (0, 4, 8, 12)
                and math.hypot(position["x"] - land["x"], position["y"] - land["y"]) <= 8
                and water_edge_distance(position) <= 2
                for position in nearby
            ),
            json.dumps(checked),
        )
    finally:
        set_goal_tiles("grass-1")
        action("move_to", return_position)

    # M10-K: the drill is unlocked from the start; use held intermediates before raw items.
    ingredients = {}
    for recipe_name in ("burner-mining-drill", "iron-gear-wheel", "stone-furnace"):
        recipe = bridge.call("get_recipe_info", {"recipe": recipe_name})["result"]
        ingredients[recipe_name] = {item["name"]: item["amount"] for item in recipe["ingredients"]}
    drill = ingredients["burner-mining-drill"]
    gear_iron = ingredients["iron-gear-wheel"]["iron-plate"]
    furnace_stone = ingredients["stone-furnace"]["stone"]
    before = items()
    iron = before.get("iron-plate", 0)
    stone = before.get("stone", 0)
    gears = before.get("iron-gear-wheel", 0)
    furnaces = before.get("stone-furnace", 0)
    for craft_count in range(1, 101):
        needed_gears = max(0, drill["iron-gear-wheel"] * craft_count - gears)
        needed_furnaces = max(0, drill["stone-furnace"] * craft_count - furnaces)
        shortfalls = {
            "iron-plate": max(
                0, drill["iron-plate"] * craft_count + gear_iron * needed_gears - iron
            ),
            "stone": max(0, furnace_stone * needed_furnaces - stone),
        }
        expected_raw = [
            {"name": name, "count": count}
            for name, count in sorted(shortfalls.items())
            if count > 0
        ]
        if expected_raw and (needed_gears > 0 or needed_furnaces > 0):
            break
    else:
        raise RuntimeError("M10-K needs a shortage within the 100-craft request limit")
    try:
        bridge.call("craft", {"recipe": "burner-mining-drill", "count": craft_count})
    except BridgeError as error:
        detail = error.detail or {}
        record(
            "craft shortage lists raw materials",
            error.code == "missing_materials" and detail.get("missing_raw") == expected_raw,
            json.dumps({"error": error.code, "expected": expected_raw, "detail": detail}),
        )
    else:
        record("craft shortage lists raw materials", False, "unexpected success")
    record("raw shortage check keeps inventory", items() == before)

    failed = [label for label, passed, _ in results if not passed]
    if failed:
        raise RuntimeError("Failed checks: " + ", ".join(failed))
    print("NEGATIVE_CHECKS_SUCCEEDED", len(results), flush=True)


if __name__ == "__main__":
    main()
