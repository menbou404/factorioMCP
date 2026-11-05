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
                "inventory_role": "not-a-role",
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

    # M11-L: the cleared area west of the character is free and within build reach.
    before = items()
    item = next(
        (name for name in ("stone-furnace", "wooden-chest") if before.get(name, 0) > 0),
        None,
    )
    if item is None:
        raise RuntimeError("M11-L requires a held stone furnace or wooden chest")
    current = bridge.call("get_agent_status")["result"]["position"]
    offset = 0.5 if item == "wooden-chest" else 0
    free_position = {
        "surface": current["surface"],
        "x": math.floor(current["x"]) - 5 + offset,
        "y": math.floor(current["y"]) + offset,
    }
    bridge.call("observe_nearby")  # Default radius 16 covers the free point.
    bridge.call("observe_nearby", {"radius": 2})
    checked = bridge.call("check_placement", {"item": item, **free_position})["result"]
    record(
        "older wide observation still allows placement",
        checked.get("placeable") is True
        and 4
        <= math.hypot(
            checked["position"]["x"] - current["x"], checked["position"]["y"] - current["y"]
        )
        <= 6
        and items() == before
        and bridge.call("get_agent_status")["result"]["position"] == current,
        json.dumps(checked),
    )

    # M11-N: the character remains near the owned furnace/drill and the ore fixture.
    observed = bridge.call("observe_nearby", {"owned_only": True, "include_tiles": False})["result"]
    entities = observed["entities"]
    position = observed["position"]
    reach = observed.get("reach")
    record(
        "observation reports distance and reach",
        bool(entities)
        and all(
            type(entity.get("distance")) in (int, float)
            and math.isclose(
                entity["distance"],
                math.hypot(entity["x"] - position["x"], entity["y"] - position["y"]),
                rel_tol=0,
                abs_tol=1e-6,
            )
            for entity in entities
        )
        and isinstance(reach, dict)
        and all(
            type(reach.get(key)) in (int, float) and reach[key] > 0
            for key in ("build", "interact", "mine_resource")
        )
        and reach["mine_resource"] < reach["interact"],
        json.dumps(observed),
    )
    observed = bridge.call("observe_nearby", {"include_resources": False, "include_tiles": False})[
        "result"
    ]
    record(
        "summary without resource entities",
        all(entity["type"] != "resource" for entity in observed["entities"])
        and isinstance(observed.get("resources"), list)
        and bool(observed["resources"]),
        json.dumps(observed),
    )

    # M12-P: remember the resource fixture even after the most recent observation moves away.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    try:
        action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
        observed = bridge.call("observe_nearby")["result"]
        fixture = next(
            entity
            for entity in observed["entities"]
            if entity["name"] == coal["name"]
            and entity["x"] == coal["x"]
            and entity["y"] == coal["y"]
        )
        action("move_to", {"surface": "nauvis", "x": -18, "y": 4})
        observed = bridge.call("observe_nearby")["result"]
        mapped = bridge.call("get_explored_map", {"resource_names": [fixture["name"]]})["result"]
        remembered = [
            patch
            for patch in mapped["resources"]
            if patch["name"] == fixture["name"]
            and patch["left_top"]["x"] <= fixture["x"] < patch["right_bottom"]["x"]
            and patch["left_top"]["y"] <= fixture["y"] < patch["right_bottom"]["y"]
        ]
        record(
            "explored map remembers resources",
            math.hypot(
                fixture["x"] - observed["position"]["x"],
                fixture["y"] - observed["position"]["y"],
            )
            > observed["radius"]
            and bool(remembered)
            and all(
                math.isclose(
                    patch["distance"],
                    math.hypot(
                        patch["nearest"]["x"] - mapped["position"]["x"],
                        patch["nearest"]["y"] - mapped["position"]["y"],
                    ),
                    rel_tol=0,
                    abs_tol=1e-6,
                )
                for patch in remembered
            ),
            json.dumps(mapped),
        )

        before_cells = bridge.call("get_explored_map")["result"]["explored"]["cells"]
        bridge.call("observe_nearby", {"radius": 1})
        after_cells = bridge.call("get_explored_map")["result"]["explored"]["cells"]
        record("explored map only covers observed cells", after_cells == before_cells)
    finally:
        action("move_to", return_position)

    def assert_water_fixture_covered(observation):
        position, radius = observation["position"], observation["radius"]
        for cx in (-2, -1):
            for cy in (-2, -1):
                for x in (8 * cx, 8 * cx + 8):
                    for y in (8 * cy, 8 * cy + 8):
                        if math.hypot(x - position["x"], y - position["y"]) > radius:
                            raise RuntimeError(
                                "M12-P walk ended too far from (-6, -6) to fully observe "
                                f"water fixture cell ({cx}, {cy}): {position}, radius={radius}"
                            )

    # All four cells containing the water fixture must be refreshed, including empty ones.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    try:
        try:
            set_goal_tiles("water")
            action("move_to", {"surface": "nauvis", "x": -6, "y": -6})
            observed = bridge.call("observe_nearby")["result"]
            assert_water_fixture_covered(observed)
            water_position = observed["position"]
            mapped = bridge.call("get_explored_map", {"limit": 50})["result"]
            shores = [
                group["nearest_shore"]
                for group in mapped["water"]
                if group.get("tiles", 0) >= 1 and group.get("nearest_shore")
            ]
            record(
                "explored map records water",
                any(
                    -12 <= shore["x"] < -7
                    and -12 <= shore["y"] < -7
                    and math.isclose(shore["x"] % 1, 0.5, abs_tol=1e-6)
                    and math.isclose(shore["y"] % 1, 0.5, abs_tol=1e-6)
                    and (math.floor(shore["x"]) in (-12, -8) or math.floor(shore["y"]) in (-12, -8))
                    for shore in shores
                ),
                json.dumps(mapped),
            )
        finally:
            set_goal_tiles("grass-1")
        observed = bridge.call("observe_nearby")["result"]
        assert_water_fixture_covered(observed)
        mapped = bridge.call("get_explored_map", {"limit": 50})["result"]
        record(
            "explored map forgets water after re-observation",
            observed["position"] == water_position
            and not any(
                group["left_top"]["x"] < -7
                and group["right_bottom"]["x"] > -12
                and group["left_top"]["y"] < -7
                and group["right_bottom"]["y"] > -12
                for group in mapped["water"]
            ),
            json.dumps(mapped),
        )
    finally:
        action("move_to", return_position)

    # M13-Q: the stone fixture still has ample ore. Cancel before it can deplete;
    # this only adds normally mined stone and never spends later scripts' materials.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    try:
        action("move_to", {"surface": "nauvis", "x": 2, "y": 2.5})
        observed = bridge.call("observe_nearby", {"names": ["stone"]})["result"]
        stone = next(entity for entity in observed["entities"] if entity.get("amount", 0) >= 2)
        started = bridge.call("mine_entity", {"target_ref": stone["target_ref"], "count": 2})[
            "result"
        ]
        try:
            time.sleep(0.25)  # Exercise progress in the current operation, before it completes.
            running = bridge.call("get_action_status", {"action_id": started["action_id"]})[
                "result"
            ]
            initial_eta = started.get("estimated_seconds")
            running_eta = (running.get("detail") or {}).get("estimated_seconds")
            record(
                "mining reports estimated seconds",
                running.get("status") == "running"
                and type(initial_eta) in (int, float)
                and type(running_eta) in (int, float)
                and initial_eta > 0
                and 0 < running_eta <= initial_eta + 0.5
                and math.isclose(initial_eta * 10, round(initial_eta * 10), abs_tol=1e-6)
                and math.isclose(running_eta * 10, round(running_eta * 10), abs_tol=1e-6),
                json.dumps({"started": started, "running": running}),
            )
        finally:
            current = bridge.call("get_action_status", {"action_id": started["action_id"]})[
                "result"
            ]
            if current.get("status") == "running":
                bridge.call("cancel_action", {"action_id": started["action_id"]})

        # M13-R: reuse the spare furnace without fuel, so its input cannot be consumed.
        action("mine_entity", {"target_ref": reference("stone"), "count": 1})
        action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
        before = items()
        bridge.call("observe_nearby")
        bridge.call("place_entity", {"item": "stone-furnace", **spare_position})
        spare = next(
            entity
            for entity in bridge.call("observe_nearby", {"names": ["stone-furnace"]})["result"][
                "entities"
            ]
            if entity["x"] == spare_position["x"] and entity["y"] == spare_position["y"]
        )
        params = {"target_ref": spare["target_ref"], "item": "stone", "count": 1}
        try:
            inventories = bridge.call("get_entity_inventory", {"target_ref": spare["target_ref"]})[
                "result"
            ]["inventories"]
            inserted = bridge.call(
                "transfer_item", {**params, "direction": "to_entity", "inventory_role": "input"}
            )["result"]
            stocked = bridge.call("get_entity_inventory", {"target_ref": spare["target_ref"]})[
                "result"
            ]["inventories"]
            removed = bridge.call(
                "transfer_item", {**params, "direction": "from_entity", "inventory_role": "source"}
            )["result"]
            # An empty output must reach the canonical result inventory's shortage path.
            expect_error(
                "furnace output alias uses result inventory",
                "transfer_item",
                {**params, "direction": "from_entity", "inventory_role": "output"},
                {"missing_materials"},
            )
            shortage = errors.get("furnace output alias uses result inventory")
            record(
                "furnace accepts input/output role aliases",
                set(inventories) == {"fuel", "source", "result"}
                and spare.get("inventory_roles") == ["fuel", "result", "source"]
                and stocked["source"] == [{"name": "stone", "count": 1, "quality": "normal"}]
                and inserted.get("moved") == removed.get("moved") == 1
                and inserted.get("inventory_role") == removed.get("inventory_role") == "source"
                and shortage is not None
                and (shortage.detail or {}).get("inventory_role") == "result",
            )
            expect_error(
                "invalid furnace role reports aliases",
                "transfer_item",
                {**params, "direction": "to_entity", "inventory_role": "not-a-role"},
                {"invalid_target"},
            )
            error = errors.get("invalid furnace role reports aliases")
            detail = (error.detail or {}) if error else {}
            record(
                "invalid role keeps canonical roles and aliases",
                detail.get("valid_roles") == ["fuel", "result", "source"]
                and detail.get("role_aliases") == {"input": "source", "output": "result"},
                json.dumps(detail),
            )
        finally:
            action("mine_entity", {"target_ref": spare["target_ref"], "count": 1})
        record("role alias checks preserve held items", items() == before)
    finally:
        action("move_to", return_position)

    # M13-S: reuse M10-J's suggestions and prove the reported sides against actual tiles.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    try:
        set_goal_tiles("water")
        action("move_to", {**land, "y": -2})
        bridge.call("observe_nearby")
        checked = bridge.call("check_placement", {"item": "offshore-pump", **land})["result"]
        record(
            "blocked offshore pump reports requested sides",
            checked.get("placeable") is False
            and checked.get("water_direction") == 0
            and checked.get("output_direction") == 8,
            json.dumps(checked),
        )
        nearby = checked.get("nearby_positions", [])
        if not nearby:
            raise RuntimeError("M13-S requires a shoreline suggestion from M10-J")
        candidate = nearby[0]
        # The build reach check is repeated after walking nearer the candidate.
        action("move_to", {"surface": "nauvis", "x": -6, "y": -6})
        observed = bridge.call("observe_nearby")["result"]
        checked = bridge.call(
            "check_placement", {"item": "offshore-pump", "surface": "nauvis", **candidate}
        )["result"]
        water_direction = checked.get("water_direction")
        output_direction = checked.get("output_direction")
        vectors = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}
        water_tiles = {
            (tile["x"], tile["y"]) for tile in observed["blocked_tiles"] if tile["name"] == "water"
        }

        def side_tile(direction):
            dx, dy = vectors[direction]
            position = checked["position"]
            box = checked["bounding_box"]
            edge = (
                box["right_bottom"]["x"] - position["x"]
                if dx > 0
                else position["x"] - box["left_top"]["x"]
                if dx < 0
                else box["right_bottom"]["y"] - position["y"]
                if dy > 0
                else position["y"] - box["left_top"]["y"]
            )
            # Take one tile step past the collision edge, toward the indicated side.
            return (
                math.floor(position["x"] + dx * (edge + 1)),
                math.floor(position["y"] + dy * (edge + 1)),
            )

        # Read the actual tiles as well: observation omits walkable land tiles.
        tile_sides = {}
        if water_direction in vectors and output_direction in vectors:
            wx, wy = side_tile(water_direction)
            ox, oy = side_tile(output_direction)
            query = (
                "/c local s=game.surfaces.nauvis;"
                f"local w=s.get_tile({wx},{wy});local o=s.get_tile({ox},{oy});"
                'rcon.print("m13_sides:"..helpers.table_to_json({'
                'water=w.collides_with("water_tile"),land=o.collides_with("ground_tile")}))'
            )
            with Client(
                "127.0.0.1", 27277, passwd=password, timeout=5, frag_threshold=RCON_FRAG_THRESHOLD
            ) as client:
                raw = client.run(query)
                if "m13_sides:" not in raw:
                    raw = client.run(query)
            tile_sides = json.loads(raw.split("m13_sides:", 1)[1].strip())
        record(
            "offshore pump reports water side",
            checked.get("placeable") is True
            and output_direction in vectors
            and water_direction == (output_direction + 8) % 16
            and output_direction == (candidate["direction"] + 8) % 16
            and any(
                port["direction"] == output_direction and port["flow"] == "output"
                for port in checked.get("fluid_connections", [])
            )
            and side_tile(water_direction) in water_tiles
            and side_tile(output_direction) not in water_tiles
            and tile_sides == {"water": True, "land": True},
            json.dumps(checked),
        )
        for direction in vectors:
            rotated = bridge.call(
                "check_placement",
                {"item": "offshore-pump", "surface": "nauvis", **candidate, "direction": direction},
            )["result"]
            record(
                f"offshore pump sides rotate with direction {direction}",
                rotated.get("water_direction") == direction
                and rotated.get("output_direction") == (direction + 8) % 16,
                json.dumps(rotated),
            )
    finally:
        set_goal_tiles("grass-1")
        action("move_to", return_position)

    # M15-X: these are military-2's direct prerequisites in the 2.0.77 prototypes.
    # Unlike trial 9, this early script has not unlocked green science yet.
    prerequisites = ["logistic-science-pack", "military", "steel-processing"]
    research_before = bridge.call("get_research_status")["result"]
    record(
        "research fixture has unresearched military prerequisites",
        not set(prerequisites).intersection(research_before["researched"] or []),
        json.dumps(research_before),
    )
    server = create_server(bridge)
    for technology, reason, expected_missing in (
        ("military-2", "missing_prerequisites", prerequisites),
        ("m15-unknown-technology", "unknown", None),
    ):
        result = asyncio.run(server.call_tool("start_research", {"technology": technology}))
        value = json.loads(result.content[0].text)
        detail = value.get("detail", {})
        hint = value.get("hint", "")
        record(
            "research with missing prerequisites explains why"
            if expected_missing is not None
            else "unknown research explains why",
            result.is_error
            and value.get("error") == "invalid_target"
            and detail.get("technology") == technology
            and detail.get("reason") == reason
            and detail.get("missing_prerequisites") == expected_missing
            and all(
                text in hint
                for text in ("detail.reason", "detail.missing_prerequisites", "get_research_status")
            ),
            json.dumps(value),
        )
    research_after = bridge.call("get_research_status")["result"]
    record(
        "rejected research preserves research state",
        all(
            research_after.get(key) == research_before.get(key)
            for key in ("researched", "queue", "current")
        ),
    )
    current = bridge.call("get_agent_status")["result"]["position"]
    result = asyncio.run(server.call_tool("move_to", {**current, "x": current["x"] + 128.7}))
    value = json.loads(result.content[0].text)
    detail = value.get("detail", {})
    hint = value.get("hint", "")
    record(
        "move beyond 128 tiles hints intermediate points",
        result.is_error
        and value.get("error") == "out_of_range"
        and detail.get("reach") == 128
        and math.isclose(detail.get("distance", 0), 128.7, abs_tol=1e-6)
        and all(
            text in hint for text in ("detail.reach", "128", "straight line", "intermediate points")
        ),
        json.dumps(value),
    )
    record(
        "rejected distant move keeps position",
        bridge.call("get_agent_status")["result"]["position"] == current,
    )

    # M16-AB/AD: keep the original furnace and drill intact for logistics_line.py.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    pending_move = None
    try:
        for within in (0.49, 10.01, True, "3"):
            expect_error(
                f"move_to rejects invalid within {within!r}",
                "move_to",
                {**return_position, "within": within},
                {"invalid_input"},
            )
        expect_error(
            "move_to within keeps the 128-tile goal limit",
            "move_to",
            {**return_position, "x": return_position["x"] + 128.7, "within": 10},
            {"out_of_range"},
        )
        action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
        observed = bridge.call("observe_nearby")["result"]
        entities = observed["entities"]
        distances = [entity["distance"] for entity in entities]
        order = [
            (entity["distance"], entity["x"], entity["y"], entity["name"]) for entity in entities
        ]
        record(
            "observation lists nearest entities first",
            len(entities) >= 2 and distances == sorted(distances) and order == sorted(order),
            json.dumps(order),
        )
        page = bridge.call("observe_nearby", {"entity_offset": 1})["result"]
        record(
            "observation paging follows nearest-first order",
            [
                (entity["distance"], entity["x"], entity["y"], entity["name"])
                for entity in page["entities"]
            ]
            == order[1:],
        )
        goal = {"surface": "nauvis", "x": 6, "y": 4}
        record(
            "within goal is occupied by the original furnace",
            any(
                entity["name"] == "stone-furnace" and entity["x"] == 6 and entity["y"] == 4
                for entity in entities
            ),
        )
        start = {"surface": "nauvis", "x": 0, "y": 4}
        action("move_to", start)
        try:
            pending_move = bridge.call("move_to", goal)["result"]["action_id"]
            waited = asyncio.run(
                server.call_tool(
                    "wait_for_action", {"action_id": pending_move, "timeout_seconds": 15}
                )
            )
            plain = json.loads(waited.content[0].text)
        except BridgeError as error:
            plain = {"status": "failed", "error": error.code, "detail": error.detail}
        record(
            "plain move_to cannot stand at enclosed goal",
            (plain.get("status") == "failed" and plain.get("error") == "path_not_found")
            or (
                plain.get("status") == "succeeded"
                and (plain.get("detail") or {}).get("goal_blocked") is True
            ),
            json.dumps(plain),
        )
        if plain.get("status") == "running":
            bridge.call("cancel_action", {"action_id": pending_move})
        pending_move = None
        action("move_to", start)
        before_move = bridge.call("get_agent_status")["result"]["position"]
        initial_distance = math.hypot(before_move["x"] - goal["x"], before_move["y"] - goal["y"])
        pending_move = bridge.call("move_to", {**goal, "within": 3})["result"]["action_id"]
        waited = asyncio.run(
            server.call_tool("wait_for_action", {"action_id": pending_move, "timeout_seconds": 15})
        )
        value = json.loads(waited.content[0].text)
        detail = value.get("detail") or {}
        final_position = bridge.call("get_agent_status")["result"]["position"]
        final_distance = math.hypot(
            final_position["x"] - goal["x"], final_position["y"] - goal["y"]
        )
        record(
            "move_to within stops near an enclosed goal",
            not waited.is_error
            and value.get("status") == "succeeded"
            and initial_distance > 3
            and final_distance <= 3
            and detail.get("within") == 3
            and math.isclose(detail.get("distance", -1), final_distance, abs_tol=1e-6)
            and "goal_blocked" not in detail,
            json.dumps({"plain": plain, "within": value, "final_distance": final_distance}),
        )
        time.sleep(0.25)
        record(
            "move_to within stops walking after arrival",
            bridge.call("get_agent_status")["result"]["position"] == final_position,
        )
        # Already in radius: complete without needing a path into the furnace.
        arrived = action("move_to", {**goal, "within": 3})
        record(
            "move_to within succeeds when already near the goal",
            arrived.get("detail", {}).get("within") == 3
            and arrived["detail"]["distance"] <= 3
            and bridge.call("get_agent_status")["result"]["position"] == final_position,
        )
    finally:
        if pending_move is not None:
            current_move = bridge.call("get_action_status", {"action_id": pending_move})["result"]
            if current_move.get("status") == "running":
                bridge.call("cancel_action", {"action_id": pending_move})
        action("move_to", return_position)

    # M16-AC: this observed land is clear of ore and needs no drill item.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    try:
        action("move_to", {**land, "y": -2})
        observed = bridge.call("observe_nearby")["result"]
        record(
            "drill diagnostic fixture has no ore",
            not any(
                entity["type"] == "resource"
                and abs(entity["x"] - land["x"]) <= 1
                and abs(entity["y"] - land["y"]) <= 1
                for entity in observed["entities"]
            ),
        )
        for direction in (0, 4, 8, 12):
            result = asyncio.run(
                server.call_tool(
                    "check_placement",
                    {"item": "burner-mining-drill", **land, "direction": direction},
                )
            )
            checked = json.loads(result.content[0].text)
            record(
                f"drill on bare ground explains missing resources direction {direction}",
                not result.is_error
                and checked.get("placeable") is False
                and checked.get("reason") == "blocked"
                and checked.get("blocked_reason") == "placement_rules"
                and checked.get("reason_detail") == "no_resources_in_mining_area"
                and checked.get("mining_area_resources") == [],
                json.dumps(checked),
            )
    finally:
        action("move_to", return_position)

    # M16-AE: force knowledge includes technologies whose prerequisites are still locked.
    research_before = bridge.call("get_research_status")["result"]
    result = asyncio.run(server.call_tool("get_technology_info", {"technology": "military-2"}))
    info = json.loads(result.content[0].text)
    researched = set(research_before["researched"] or [])
    record(
        "locked technology exposes prerequisites costs and recipes",
        not result.is_error
        and info.get("name") == "military-2"
        and info.get("researched") == ("military-2" in researched)
        and info.get("enabled") is True
        and info.get("available") == all(name in researched for name in prerequisites)
        and info.get("prerequisites")
        == [{"name": name, "researched": name in researched} for name in prerequisites]
        and {item["name"] for item in info.get("ingredients", [])}
        == {"automation-science-pack", "logistic-science-pack"}
        and all(item["amount"] > 0 for item in info.get("ingredients", []))
        and info.get("unit_count", 0) > 0
        and info.get("unit_time", 0) > 0
        and isinstance(info.get("unlocks_recipes"), list)
        and len(info["unlocks_recipes"]) > 0,
        json.dumps(info),
    )
    unknown = "m16-unknown-technology"
    result = asyncio.run(server.call_tool("get_technology_info", {"technology": unknown}))
    info = json.loads(result.content[0].text)
    record(
        "unknown technology info explains unknown name",
        result.is_error
        and info.get("error") == "invalid_target"
        and info.get("detail") == {"technology": unknown, "reason": "unknown"}
        and "detail.technology" in info.get("hint", ""),
        json.dumps(info),
    )
    research_after = bridge.call("get_research_status")["result"]
    record(
        "reading technology info preserves research state",
        all(
            research_after.get(key) == research_before.get(key)
            for key in ("researched", "queue", "current")
        ),
    )

    # M17-AG: only the disposable-save harness creates these three iron plates.
    # Keep the original furnace/drill untouched for later integration scripts.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    held = items()
    ground_item = next(
        (name for name in ("stone-furnace", "wooden-chest") if held.get(name, 0) > 0),
        None,
    )
    if ground_item is None:
        raise RuntimeError("M17-AG requires a held stone furnace or wooden chest")
    placed_ground_entity = False
    ground_box = None

    def ground_fixture(body):
        command = "/c local s=game.surfaces.nauvis;" + body
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=5, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
            if "m17ag_fixture:" not in raw:
                raw = client.run(command)
        if "m17ag_fixture:" not in raw:
            raise RuntimeError("Missing M17-AG fixture response: " + raw)
        return json.loads(raw.split("m17ag_fixture:", 1)[1].strip())

    def create_ground_fixture(position):
        # The storage reference makes retries idempotent; never create a second stack.
        fixture = ground_fixture(
            "local e=storage.m17ag_ground_fixture;"
            "if not e or not e.valid then "
            'e=s.create_entity{name="item-on-ground",'
            f"position={{x={position['x']},y={position['y']}}},"
            'stack={name="iron-plate",quality="normal",count=3}};'
            "storage.m17ag_ground_fixture=e end;"
            'rcon.print("m17ag_fixture:"..helpers.table_to_json({'
            "created=e~=nil and e.valid,count=e and e.valid and e.stack.count or 0}))"
        )
        record(
            "test harness created ground stack of three iron plates",
            fixture == {"created": True, "count": 3},
            json.dumps(fixture),
        )

    def remove_ground_fixture():
        ground_fixture(
            "local e=storage.m17ag_ground_fixture;"
            "if e and e.valid then e.destroy() end;storage.m17ag_ground_fixture=nil;"
            "rcon.print('m17ag_fixture:{\"removed\":true}')"
        )

    try:
        action("move_to", {**land, "y": -1})
        bridge.call("observe_nearby")
        params = {"item": ground_item, **land}
        checked = bridge.call("check_placement", params)["result"]
        if not checked.get("placeable"):
            raise RuntimeError("M17-AG requires observed bare ground: " + json.dumps(checked))
        params.update(checked["position"])
        ground_box = checked["bounding_box"]
        expected_stacks = [{"name": "iron-plate", "quality": "normal", "count": 3}]
        before_ground = items()
        create_ground_fixture(params)
        checked = bridge.call("check_placement", params)["result"]
        record(
            "ground-only placement preview reports collectable stacks without collecting",
            checked.get("placeable") is True
            and checked.get("ground_items") == expected_stacks
            and items() == before_ground,
            json.dumps(checked),
        )
        placed = bridge.call("place_entity", params)["result"]
        placed_ground_entity = True
        record(
            "placement collects ground items and consumes its real building item",
            placed.get("collected_items") == expected_stacks
            and items().get("iron-plate", 0) == before_ground.get("iron-plate", 0) + 3
            and items().get(ground_item, 0) == before_ground[ground_item] - 1,
            json.dumps(placed),
        )
        left, right = ground_box["left_top"], ground_box["right_bottom"]
        remaining = ground_fixture(
            'local es=s.find_entities_filtered{type="item-entity",area={'
            f"{{{left['x']},{left['y']}}},{{{right['x']},{right['y']}}}"
            "}};"
            'rcon.print("m17ag_fixture:"..helpers.table_to_json({remaining=#es}))'
        )
        record("placement leaves no ground items in its footprint", remaining == {"remaining": 0})

        # A forced test-only drop under the placed entity gives two blocker types.
        create_ground_fixture(params)
        before_mixed = items()
        checked = bridge.call("check_placement", params)["result"]
        blockers = checked.get("blocking") or []
        record(
            "mixed blockers expose the ground stack and prevent collection",
            checked.get("placeable") is False
            and checked.get("reason") == "blocked"
            and any(entry.get("stack") == expected_stacks[0] for entry in blockers)
            and any(entry.get("type") != "item-entity" for entry in blockers)
            and items() == before_mixed,
            json.dumps(checked),
        )
    finally:
        remove_ground_fixture()
        if placed_ground_entity:
            observed = bridge.call("observe_nearby")["result"]
            target = next(
                entity
                for entity in observed["entities"]
                if entity["name"] == ground_item
                and entity["x"] == params["x"]
                and entity["y"] == params["y"]
            )
            action("mine_entity", {"target_ref": target["target_ref"], "count": 1})
            record(
                "ground placement fixture restores the held building item",
                items().get(ground_item, 0) == held[ground_item],
            )
        action("move_to", return_position)

    # M17-AH: an idle spare furnace keeps wood in its single fuel slot.
    # Use only held items and recover everything for the later scripts.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    before_full = items()
    if not all(before_full.get(name, 0) > 0 for name in ("stone-furnace", "wood", "coal")):
        raise RuntimeError("M17-AH requires a held spare furnace, wood, and coal")
    full_target = None
    try:
        action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
        bridge.call("observe_nearby")
        bridge.call("place_entity", {"item": "stone-furnace", **spare_position})
        full_target = next(
            entity
            for entity in bridge.call("observe_nearby", {"names": ["stone-furnace"]})["result"][
                "entities"
            ]
            if entity["x"] == spare_position["x"] and entity["y"] == spare_position["y"]
        )
        fuel_params = {
            "target_ref": full_target["target_ref"],
            "direction": "to_entity",
            "inventory_role": "fuel",
            "item": "wood",
            "count": 1,
        }
        stocked = bridge.call("transfer_item", fuel_params)["result"]
        record("full inventory fixture puts held wood in fuel", stocked.get("moved") == 1)
        blocked_params = {**fuel_params, "item": "coal"}
        expected_contents = [{"name": "wood", "quality": "normal", "count": 1}]

        def check_full_detail(value):
            detail = value.get("detail") or {}
            return (
                value.get("error") == "inventory_full"
                and detail.get("destination") == "entity"
                and detail.get("inventory_role") == "fuel"
                and detail.get("item") == "coal"
                and detail.get("requested") == 1
                and detail.get("slots") == 1
                and detail.get("empty_slots") == 0
                and detail.get("insertable") == 0
                and detail.get("contents") == expected_contents
                and "from_entity first" in value.get("hint", "")
            )

        rejected = asyncio.run(server.call_tool("transfer_item", blocked_params))
        value = json.loads(rejected.content[0].text)
        record(
            "full fuel slot describes occupying wood and destination capacity",
            rejected.is_error and check_full_detail(value),
            json.dumps(value),
        )
        batch_response = asyncio.run(
            server.call_tool(
                "transfer_items",
                {"items": [blocked_params, {**fuel_params, "direction": "from_entity"}]},
            )
        )
        batch = json.loads(batch_response.content[0].text)
        entries = batch.get("results") or []
        record(
            "batch full fuel slot keeps detail and continues to recover wood",
            not batch_response.is_error
            and batch.get("succeeded") == batch.get("failed") == 1
            and len(entries) == 2
            and entries[0].get("ok") is False
            and check_full_detail(entries[0])
            and entries[1].get("ok") is True
            and entries[1].get("result", {}).get("moved") == 1,
            json.dumps(batch),
        )
    finally:
        if full_target is not None:
            fuel = bridge.call("get_entity_inventory", {"target_ref": full_target["target_ref"]})[
                "result"
            ]["inventories"]["fuel"]
            for stack in fuel or []:
                bridge.call(
                    "transfer_item",
                    {
                        **fuel_params,
                        "direction": "from_entity",
                        "item": stack["name"],
                        "count": stack["count"],
                    },
                )
            action("mine_entity", {"target_ref": full_target["target_ref"], "count": 1})
        action("move_to", return_position)
    record("full fuel slot checks restore all held items", items() == before_full)

    # M18-AJ: an idle spare furnace provides a stable source for partial transfers.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    before_partial = items()
    partial_target = None
    partial_fields = ("partial", "limited_by", "available", "insertable")
    try:
        action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
        bridge.call("observe_nearby")
        bridge.call("place_entity", {"item": "stone-furnace", **spare_position})
        partial_target = next(
            entity
            for entity in bridge.call("observe_nearby", {"names": ["stone-furnace"]})["result"][
                "entities"
            ]
            if entity["x"] == spare_position["x"] and entity["y"] == spare_position["y"]
        )
        fuel_params = {
            "target_ref": partial_target["target_ref"],
            "direction": "to_entity",
            "inventory_role": "fuel",
            "item": "wood",
            "count": 1,
        }
        full = bridge.call("transfer_item", fuel_params)["result"]
        record(
            "full transfer returns requested and omits partial fields",
            full.get("requested") == full.get("moved") == 1
            and all(field not in full for field in partial_fields),
            json.dumps(full),
        )
        take = {**fuel_params, "direction": "from_entity", "count": 100}
        partial = bridge.call("transfer_item", take)["result"]

        def source_limited(value):
            return (
                value.get("requested") == 100
                and value.get("moved") == 1
                and value.get("partial") is True
                and value.get("limited_by") == "source"
                and value.get("available") == 1
                and value.get("insertable", 0) >= 1
            )

        record(
            "source-limited transfer explains the shortfall",
            source_limited(partial),
            json.dumps(partial),
        )
        batch_response = asyncio.run(
            server.call_tool("transfer_items", {"items": [fuel_params, take]})
        )
        batch = json.loads(batch_response.content[0].text)
        entries = batch.get("results") or []
        record(
            "batch transfers keep full and source-limited success details",
            not batch_response.is_error
            and batch.get("succeeded") == 2
            and batch.get("failed") == 0
            and len(entries) == 2
            and entries[0].get("ok") is True
            and entries[1].get("ok") is True
            and entries[0].get("result", {}).get("requested") == 1
            and all(field not in entries[0].get("result", {}) for field in partial_fields)
            and source_limited(entries[1].get("result", {})),
            json.dumps(batch),
        )
        # Optional cheap capacity check: no extra mining or fixture items are needed.
        if before_partial.get("coal", 0) >= 51:
            coal = {**fuel_params, "item": "coal", "count": 49}
            bridge.call("transfer_item", coal)
            available = items()["coal"]
            limited = bridge.call("transfer_item", {**coal, "count": 2})["result"]
            record(
                "destination-limited transfer reports the remaining fuel capacity",
                limited.get("requested") == 2
                and limited.get("moved") == 1
                and limited.get("partial") is True
                and limited.get("limited_by") == "destination"
                and limited.get("available") == available
                and limited.get("insertable") == 1,
                json.dumps(limited),
            )
    finally:
        if partial_target is not None:
            fuel = bridge.call(
                "get_entity_inventory", {"target_ref": partial_target["target_ref"]}
            )["result"]["inventories"]["fuel"]
            for stack in fuel or []:
                bridge.call(
                    "transfer_item",
                    {
                        **fuel_params,
                        "direction": "from_entity",
                        "item": stack["name"],
                        "count": stack["count"],
                    },
                )
            action("mine_entity", {"target_ref": partial_target["target_ref"], "count": 1})
        action("move_to", return_position)
    record("partial transfer checks restore all held items", items() == before_partial)

    # M19-AR: walk into a held spare furnace to create contact without teleporting.
    # A furnace spans enough of the y axis to catch the walk despite move_to's
    # arrival tolerance. Reading the real boxes avoids guessing fixed-point edges.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    before_contact = items()
    contact_placed = False
    pending_move = None

    def contact_walking(walking):
        # storage belongs to the mod's Lua state, not the default level state.
        command = (
            "/c __factorio_ai_mcp__ local c=storage.ai_mcp.character;"
            f"c.walking_state={{walking={str(walking).lower()},direction=defines.direction.east}};"
            'rcon.print("m19ar_contact:"..helpers.table_to_json({box=c.bounding_box}))'
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=5, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
            if "m19ar_contact:" not in raw:
                raw = client.run(command)
        if "m19ar_contact:" not in raw:
            raise RuntimeError(f"Missing M19-AR contact response: {raw!r}")
        return json.loads(raw.split("m19ar_contact:", 1)[1].strip())["box"]

    try:
        approach = {**land, "x": land["x"] - 3}
        action("move_to", approach)
        bridge.call("observe_nearby")
        params = {"item": "stone-furnace", **land}
        checked = bridge.call("check_placement", params)["result"]
        if not checked.get("placeable"):
            raise RuntimeError("M19-AR requires clear land: " + json.dumps(checked))
        bridge.call("place_entity", params)
        contact_placed = True
        furnace_box = checked["bounding_box"]
        previous = bridge.call("get_agent_status")["result"]["position"]
        initial_x = previous["x"]
        stable = 0
        contact_walking(True)
        for _ in range(50):
            time.sleep(0.1)
            current = bridge.call("get_agent_status")["result"]["position"]
            stable = stable + 1 if current == previous else 0
            previous = current
            if stable >= 3 and current["x"] - initial_x > 0.5:
                break
        character_box = contact_walking(False)
        gap = furnace_box["left_top"]["x"] - character_box["right_bottom"]["x"]
        touching = (
            stable >= 3
            and 0 <= gap < 0.01
            and character_box["left_top"]["y"] < furnace_box["right_bottom"]["y"]
            and character_box["right_bottom"]["y"] > furnace_box["left_top"]["y"]
        )
        record("walking fixture touches the furnace collision box", touching, f"gap={gap}")
        if not touching:
            raise RuntimeError("M19-AR did not establish obstacle contact")
        pending_move = bridge.call("move_to", approach)["result"]["action_id"]
        waited = asyncio.run(
            server.call_tool("wait_for_action", {"action_id": pending_move, "timeout_seconds": 15})
        )
        escaped = json.loads(waited.content[0].text)
        record(
            "move_to walks away from a touching obstacle",
            not waited.is_error and escaped.get("status") == "succeeded",
            json.dumps(escaped),
        )
    finally:
        # Log cleanup failures without replacing an exception from the fixture.
        try:
            contact_walking(False)
        except Exception as error:
            record("M19-AR walking cleanup", False, repr(error))
        try:
            if pending_move is not None:
                current = bridge.call("get_action_status", {"action_id": pending_move})["result"]
                if current.get("status") == "running":
                    bridge.call("cancel_action", {"action_id": pending_move})
            if contact_placed:
                observed = bridge.call("observe_nearby", {"names": ["stone-furnace"]})["result"]
                contact_target = next(
                    entity["target_ref"]
                    for entity in observed["entities"]
                    if entity["x"] == checked["position"]["x"]
                    and entity["y"] == checked["position"]["y"]
                )
                action("mine_entity", {"target_ref": contact_target, "count": 1})
            action("move_to", return_position)
        except Exception as error:
            record("M19-AR fixture cleanup", False, repr(error))
    record("obstacle contact check restores all held items", items() == before_contact)

    failed = [label for label, passed, _ in results if not passed]
    if failed:
        raise RuntimeError("Failed checks: " + ", ".join(failed))
    print("NEGATIVE_CHECKS_SUCCEEDED", len(results), flush=True)


if __name__ == "__main__":
    main()
