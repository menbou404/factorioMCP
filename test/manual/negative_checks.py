"""Manual failure-path checks for a disposable save on RCON port 27277 only.

Run right after production_line.py on the same server: it relies on the
fixture's stone patch and the running stone furnace at (6, 4).
"""

import asyncio
import json
import math
import time
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError
from rcon.source import Client

from factorio_mcp.bridge import RCON_FRAG_THRESHOLD, BridgeError, parse_bridge_response
from factorio_mcp.server import create_server
from production_line import action, bridge, password, reference

results = []
errors = {}
observed_map_cells = {}


def remember_map_observation(observation):
    # Reconstruct fully observed 8x8 cells using only observe_nearby's returned circle.
    position, radius = observation["position"], observation["radius"]
    cells = observed_map_cells.setdefault(position["surface"], set())
    for cx in range(
        math.floor((position["x"] - radius) / 8), math.floor((position["x"] + radius) / 8) + 1
    ):
        for cy in range(
            math.floor((position["y"] - radius) / 8), math.floor((position["y"] + radius) / 8) + 1
        ):
            if all(
                (x - position["x"]) ** 2 + (y - position["y"]) ** 2 <= radius**2
                for x in (cx * 8, (cx + 1) * 8)
                for y in (cy * 8, (cy + 1) * 8)
            ):
                cells.add((cx, cy))


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


def m42_chunk_generation_checks():
    # Disposable unbounded surface: generation, terrain edits and belts vanish on cleanup.
    m42_surface = "m42-chunk-generation-fixture"
    m42_server = create_server(bridge)
    original_items = bridge.call("get_inventory")["result"]["items"]
    original_position = bridge.call("get_agent_status")["result"]["position"]

    def fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;local c=state.character;"
            "assert(c and c.valid);local inv=c.get_main_inventory();"
            f'local name="{m42_surface}";local s=game.surfaces[name];'
            "local b=storage.m42_backup;" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=15, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
        if "m42:" not in raw:
            raise RuntimeError("M42 fixture failed: " + raw)
        return json.loads(raw.split("m42:", 1)[1].strip())

    def tool(name, args):
        response = asyncio.run(m42_server.call_tool(name, args))
        value = json.loads(response.content[0].text)
        assert not response.is_error, value
        return value

    try:
        edge = fixture(
            "assert(not s and not b,'M42 fixture already exists');"
            "assert(not state.action and not state.craft_action and c.crafting_queue_size==0);"
            "b={position={x=c.position.x,y=c.position.y},surface=c.surface,selected=c.selected,"
            "inventory=game.create_inventory(#inv),saved={},"
            "fields={'targets','owned_refs','recipe_baselines','last_observation',"
            "'recent_observations','explored_map','last_action','completed_actions',"
            "'completed_order','processed','processed_order','next_target_id','next_action_id',"
            "'path_request_id','last_heartbeat_tick','last_generated_chunk'}};storage.m42_backup=b;"
            "for _,key in ipairs(b.fields) do b.saved[key]=state[key] end;"
            "for _,key in ipairs({'targets','owned_refs','recipe_baselines','recent_observations',"
            "'explored_map','completed_actions','completed_order','processed','processed_order'}) "
            "do state[key]={} end;state.last_observation=nil;state.last_action=nil;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(b.inventory[i].swap_stack(inv[i])) end end;b.stashed=true;"
            "s=game.create_surface(name,{water='none',autoplace_settings={"
            "entity={treat_missing_as_default=false},decorative={treat_missing_as_default=false}}});"
            "s.peaceful_mode=true;s.request_to_generate_chunks({0,0},2);"
            "s.force_generate_chunk_requests();assert(c.teleport({0.5,0.5},s));"
            "local cx=math.floor(c.position.x/32);local cy=math.floor(c.position.y/32);"
            "while s.is_chunk_generated({cx,cy}) do cx=cx+1;assert(cx<1024) end;"
            "b.edge=cx*32;b.cy=cy;"
            "assert(c.teleport({b.edge-10,cy*32+0.5},s));"
            "rcon.print('m42:'..helpers.table_to_json({edge=b.edge,cy=cy,"
            "generated=s.is_chunk_generated({cx,cy}),tick=game.tick}))"
        )
        assert edge["generated"] is False, edge
        deadline = time.monotonic() + 20
        while True:
            generated = fixture(
                "local corridor=true;"
                "for cx=math.floor((b.edge-16)/32),math.floor((b.edge+63)/32) do "
                "for cy=-1,0 do corridor=corridor and s.is_chunk_generated({cx,cy}) end end;"
                "rcon.print('m42:'..helpers.table_to_json({generated="
                "s.is_chunk_generated({b.edge/32,b.cy}),corridor=corridor,tick=game.tick}))"
            )
            if (generated["generated"] and generated["corridor"]) or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        record(
            "M42 DA-1 crossing a chunk requests asynchronous generation beyond the old edge",
            generated["generated"] is True and generated["tick"] > edge["tick"],
            json.dumps({"before": edge, "after": generated}),
        )
        assert generated["generated"], generated
        # Only clear tiles after the periodic path has generated the old frontier.
        fixture(
            "local tiles={};for x=b.edge-16,b.edge+63 do for y=-6,6 do "
            "assert(s.is_chunk_generated({math.floor(x/32),math.floor(y/32)}));"
            "tiles[#tiles+1]={name='grass-1',position={x,y}} end end;s.set_tiles(tiles);"
            "rcon.print('m42:{}')"
        )
        goal = {"surface": m42_surface, "x": edge["edge"] + 30.5, "y": 0.5}
        moved = tool("move_to", {**goal, "wait_seconds": 20})
        if moved["status"] == "running":
            moved = tool(
                "wait_for_action", {"action_id": moved["action_id"], "timeout_seconds": 20}
            )
        position = bridge.call("get_agent_status")["result"]["position"]
        record(
            "M42 DA-1 move_to succeeds thirty tiles beyond the old edge",
            moved["status"] == "succeeded"
            and math.hypot(position["x"] - goal["x"], position["y"] - goal["y"]) <= 1,
            json.dumps(moved),
        )
        unseen = fixture(
            "rcon.print('m42:'..helpers.table_to_json({"
            "unobserved=state.explored_map[name]==nil and state.last_observation==nil}))"
        )
        record("M42 DA generation and movement do not reveal an explored map", unseen["unobserved"])

        # An impassable water goal returns the normal diagnostic on real generated terrain.
        fixture(
            "s.request_to_generate_chunks({b.edge+100,0},1);s.force_generate_chunk_requests();"
            "local tiles={};for x=b.edge+62,b.edge+88 do for y=-13,13 do "
            "tiles[#tiles+1]={name='water',position={x,y}} end end;s.set_tiles(tiles);"
            "rcon.print('m42:{}')"
        )
        try:
            bridge.call("move_to", {"surface": m42_surface, "x": edge["edge"] + 75.5, "y": 0.5})
        except BridgeError as error:
            record(
                "M42 DA-2 path_not_found includes goal_chunk_generated",
                error.code == "path_not_found"
                and (error.detail or {}).get("goal_chunk_generated") is True,
                json.dumps(error.detail),
            )
        else:
            record(
                "M42 DA-2 path_not_found includes goal_chunk_generated", False, "unexpected success"
            )

        fixture(
            "local tiles={};for x=b.edge-16,b.edge+61 do for y=-6,6 do "
            "tiles[#tiles+1]={name='grass-1',position={x,y}} end end;s.set_tiles(tiles);"
            "assert(c.teleport({b.edge+0.5,0.5},s));"
            "assert(inv.insert{name='transport-belt',count=22}==22);rcon.print('m42:{}')"
        )
        line = tool(
            "place_line",
            {
                "item": "transport-belt",
                "surface": m42_surface,
                "from_x": edge["edge"] + 0.5,
                "from_y": 4.5,
                "to_x": edge["edge"] + 39.5,
                "to_y": 4.5,
                "walk": True,
                "wait_seconds": 20,
            },
        )
        if line["status"] == "running":
            line = tool("wait_for_action", {"action_id": line["action_id"], "timeout_seconds": 20})
        polled = tool("get_action_status", {"action_id": line["action_id"]})
        record(
            "M42 DB walking line running out of belts retains summary and adds partial hint",
            all(
                value.get("status") == "succeeded"
                and value.get("complete") is False
                and value.get("partial") is True
                and value.get("stopped") == "missing_materials"
                and value.get("placed") == 22
                and value.get("requested") == 40
                and value.get("already_present") == 0
                and value.get("next_position") == {"x": edge["edge"] + 22.5, "y": 4.5}
                and value.get("last_position") == {"x": edge["edge"] + 21.5, "y": 4.5}
                and "22 of 40 positions (missing_materials)" in value.get("hint", "")
                and "Continue from next_position." in value.get("hint", "")
                for value in (line, polled)
            ),
            json.dumps(line),
        )
        position = bridge.call("get_agent_status")["result"]["position"]
        record(
            "M42 DB line actually walked before exhausting items", position["x"] > edge["edge"] + 10
        )
    finally:
        pending = fixture(
            "rcon.print('m42:'..helpers.table_to_json({id=b and state.action and state.action.id}))"
        )
        if pending.get("id"):
            bridge.call("cancel_action", {"action_id": pending["id"]})
        fixture(
            "if b then assert(c.teleport(b.position,b.surface));c.selected=b.selected;"
            "if b.stashed then inv.clear() end;"
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            "for i=1,#b.inventory do assert(not b.inventory[i].valid_for_read) end;"
            "b.inventory.destroy();if s then assert(game.delete_surface(s)) end;"
            "for _,key in ipairs(b.fields) do state[key]=b.saved[key] end;"
            "storage.m42_backup=nil;assert(c.position.x==b.position.x and "
            "c.position.y==b.position.y and c.surface==b.surface) end;rcon.print('m42:{}')"
        )
        removed = fixture(
            "rcon.print('m42:'..helpers.table_to_json({backup=b~=nil,surface=s~=nil}))"
        )
        record(
            "M42 restores position and inventory and removes fixture terrain and entities",
            removed == {"backup": False, "surface": False}
            and bridge.call("get_inventory")["result"]["items"] == original_items
            and bridge.call("get_agent_status")["result"]["position"] == original_position,
        )


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

    # Current surroundings also count as observed for placement (M20-AT).
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

    resource = reference("stone")
    record("resource target_ref changes on reobservation", reference("stone") != resource)
    time.sleep(61)
    expect_error(
        "expired target_ref", "mine_entity", {"target_ref": resource, "count": 1}, {"target_gone"}
    )

    # M20-AT: reuse the 61-second wait above, without observing again. This cleared
    # spot is within build reach from (13, 4); explicit observations have expired.
    before = items()
    current = bridge.call("get_agent_status")["result"]["position"]
    nearby_placement = {"item": "stone-furnace", "surface": "nauvis", "x": 13, "y": 8}
    checked = bridge.call("check_placement", nearby_placement)["result"]
    record(
        "current surroundings allow placement after observations expire",
        checked.get("placeable") is True and checked.get("reason") != "not_visible",
        json.dumps(checked),
    )
    placed = bridge.call("place_entity", nearby_placement)["result"]
    after = items()
    record(
        "place within reach without refreshing expired observations",
        all(placed["position"][axis] == checked["position"][axis] for axis in ("x", "y"))
        and after.get("stone-furnace", 0) == before.get("stone-furnace", 0) - 1,
        json.dumps({"placed": placed, "checked": checked, "before": before, "after": after}),
    )
    action("mine_entity", {"target_ref": placed["target_ref"], "count": 1})
    after = items()
    recovered_position = bridge.call("get_agent_status")["result"]["position"]
    record(
        "expired-observation placement recovery preserves inventory and position",
        after == before
        and all(abs(recovered_position[axis] - current[axis]) <= 0.01 for axis in ("x", "y")),
        json.dumps(
            {
                "before": before,
                "after": after,
                "position_before": current,
                "position_after": recovered_position,
            }
        ),
    )

    # M6 checks are appended so the original checks and their output order stay intact.
    action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
    # M19-AM: this original owned ref survives the same wait as the resource.
    accessible = bridge.call("get_entity_inventory", {"target_ref": furnace})["result"]
    record("owned furnace target_ref survives 60 seconds", accessible["name"] == "stone-furnace")
    record(
        "owned furnace target_ref is stable on reobservation", reference("stone-furnace") == furnace
    )
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

    # M8-D / M22-AZ: a tree trunk's occupied centre uses a standable arrival goal.
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
    adjusted = detail.get("goal_adjusted")
    position = bridge.call("get_agent_status")["result"]["position"]
    record(
        "move to a blocked tree goal finishes at goal_adjusted",
        not waited.is_error
        and value.get("status") == "succeeded"
        and isinstance(adjusted, dict)
        and adjusted != goal
        and detail.get("original_goal") == goal
        and math.hypot(position["x"] - adjusted["x"], position["y"] - adjusted["y"]) <= 0.5
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

    # M9-G: within preserves the original water goal and its failure diagnostics.
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
        started = bridge.call(
            "move_to", {"surface": return_position["surface"], **goal, "within": 0.5}
        )["result"]
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
            "plain move_to adjusts an enclosed goal or explains no path",
            (plain.get("status") == "failed" and plain.get("error") == "path_not_found")
            or (
                plain.get("status") == "succeeded"
                and isinstance((plain.get("detail") or {}).get("goal_adjusted"), dict)
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

    # M19-AO/AP: keep the production furnace/drill untouched for the later scripts.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    before_rejection = items()
    if not all(before_rejection.get(name, 0) > 0 for name in ("stone-furnace", "wood")):
        raise RuntimeError("M19-AO/AP requires a held spare furnace and wood")
    rejection_target = None

    def transfer_error(label, params, code, fields):
        before = items()
        response = asyncio.run(server.call_tool("transfer_item", params))
        value = json.loads(response.content[0].text)
        detail = value.get("detail") or {}
        record(
            label,
            response.is_error
            and value.get("error") == code
            and all(detail.get(key) == expected for key, expected in fields.items())
            and items() == before,
            json.dumps(value),
        )
        return value

    try:
        action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
        bridge.call("observe_nearby")
        bridge.call("place_entity", {"item": "stone-furnace", **spare_position})
        rejection_target = next(
            entity
            for entity in bridge.call("observe_nearby", {"names": ["stone-furnace"]})["result"][
                "entities"
            ]
            if entity["x"] == spare_position["x"] and entity["y"] == spare_position["y"]
        )
        rejected_params = {
            "target_ref": rejection_target["target_ref"],
            "direction": "to_entity",
            "inventory_role": "input",
            "item": "wood",
            "count": 1,
        }
        transfer_error(
            "furnace rejects non-smeltable source item without moving wood",
            rejected_params,
            "item_not_accepted",
            {
                "item": "wood",
                "entity": "stone-furnace",
                "inventory_role": "source",
                "reason": "no_smelting_recipe",
                "insertable": 0,
            },
        )
        machine = bridge.call(
            "get_entity_inventory", {"target_ref": rejection_target["target_ref"]}
        )["result"]
        shortage = transfer_error(
            "empty furnace result includes entity status without moving items",
            {
                **rejected_params,
                "direction": "from_entity",
                "inventory_role": "output",
                "item": "iron-plate",
            },
            "missing_materials",
            {"available": 0, "inventory_role": "result", "status": machine.get("status")},
        )
        record(
            "empty furnace status uses inventory observation names",
            isinstance(machine.get("status"), str)
            and shortage.get("detail", {}).get("status") == machine["status"],
        )
    finally:
        if rejection_target is not None:
            action("mine_entity", {"target_ref": rejection_target["target_ref"], "count": 1})
        action("move_to", return_position)
    record("furnace rejection checks restore all held items", items() == before_rejection)

    # The disposable-save harness supplies only a temporary unpowered assembler,
    # never items or research. Destroy it afterwards so later scripts inherit no changes.
    def assembler_fixture(body):
        command = "/c " + body
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=5, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
            if "m19_fixture:" not in raw:
                raw = client.run(command)
        if "m19_fixture:" not in raw:
            raise RuntimeError("Missing M19 assembler fixture response: " + raw)
        return json.loads(raw.split("m19_fixture:", 1)[1].strip())

    try:
        action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
        created = assembler_fixture(
            "local e=storage.m19_transfer_assembler;"
            "if not e or not e.valid then "
            'e=game.surfaces.nauvis.create_entity{name="assembling-machine-1",'
            "position={1.5,6.5},force=game.forces.player};"
            "storage.m19_transfer_assembler=e end;"
            'rcon.print("m19_fixture:"..helpers.table_to_json({created=e~=nil and e.valid}))'
        )
        if created.get("created") is not True:
            raise RuntimeError("M19 assembler fixture could not be created")
        assembler = next(
            entity
            for entity in bridge.call("observe_nearby", {"names": ["assembling-machine-1"]})[
                "result"
            ]["entities"]
            if entity["x"] == 1.5 and entity["y"] == 6.5
        )
        rejected_params = {**rejected_params, "target_ref": assembler["target_ref"]}
        transfer_error(
            "assembler without recipe rejects input without moving wood",
            rejected_params,
            "item_not_accepted",
            {"reason": "no_recipe", "inventory_role": "source", "insertable": 0},
        )
        # iron-gear-wheel is enabled from the start; this uses the normal recipe tool.
        bridge.call(
            "set_recipe", {"target_ref": assembler["target_ref"], "recipe": "iron-gear-wheel"}
        )
        rejected = transfer_error(
            "assembler rejects non-ingredient with recipe and ingredient names",
            rejected_params,
            "item_not_accepted",
            {
                "item": "wood",
                "entity": "assembling-machine-1",
                "inventory_role": "source",
                "recipe": "iron-gear-wheel",
                "ingredients": ["iron-plate"],
                "insertable": 0,
            },
        )
        take_params = {
            **rejected_params,
            "direction": "from_entity",
            "inventory_role": "output",
            "item": "iron-gear-wheel",
        }
        empty = transfer_error(
            "empty assembler result includes status and configured recipe",
            take_params,
            "missing_materials",
            {"available": 0, "inventory_role": "result", "recipe": "iron-gear-wheel"},
        )
        machine = bridge.call("get_entity_inventory", {"target_ref": assembler["target_ref"]})[
            "result"
        ]
        record(
            "empty assembler status uses inventory observation names",
            isinstance(machine.get("status"), str)
            and empty.get("detail", {}).get("status") == machine["status"],
        )
        response = asyncio.run(
            server.call_tool("transfer_items", {"items": [rejected_params, take_params]})
        )
        batch = json.loads(response.content[0].text)
        entries = batch.get("results") or []
        record(
            "batch rejection and empty source retain codes details and hints",
            not response.is_error
            and batch.get("succeeded") == 0
            and batch.get("failed") == 2
            and len(entries) == 2
            and all(entry.get("ok") is False for entry in entries)
            and entries[0].get("error") == "item_not_accepted"
            and entries[0].get("detail") == rejected.get("detail")
            and "detail.ingredients" in entries[0].get("hint", "")
            and entries[1].get("error") == "missing_materials"
            and entries[1].get("detail") == empty.get("detail")
            and "wait_for_items" in entries[1].get("hint", "")
            and items() == before_rejection,
            json.dumps(batch),
        )
    finally:
        assembler_fixture(
            "local e=storage.m19_transfer_assembler;"
            "if e and e.valid then e.destroy() end;storage.m19_transfer_assembler=nil;"
            "rcon.print('m19_fixture:{\"removed\":true}')"
        )
        action("move_to", return_position)
    record("assembler rejection checks preserve all held items", items() == before_rejection)

    # M20-AS: recreate trial 14's walkable 0.45-tile slot with real held buildings.
    # Only fixture positioning uses teleport, in the mod's Lua state, never through MCP.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    before_escape = items()  # Include all setup costs in the restoration baseline.
    crafted_escape_chest = False
    escape_targets = []
    escape_action_id = None

    def escape_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;"
            "local c=state and state.character;assert(c and c.valid);" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=5, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
            if "m20as_fixture:" not in raw:
                raw = client.run(command)
        if "m20as_fixture:" not in raw:
            raise RuntimeError("Missing M20-AS fixture response: " + raw)
        return json.loads(raw.split("m20as_fixture:", 1)[1].strip())

    def overlaps(first, second):
        return all(
            first["left_top"][axis] < second["right_bottom"][axis]
            and first["right_bottom"][axis] > second["left_top"][axis]
            for axis in ("x", "y")
        )

    def mine_escape_wood():
        # Use a separate raw-resource fixture; later scripts still need M8-D's tree.
        tree = escape_fixture(
            "local e=storage.m20as_tree;"
            "if not e or not e.valid then "
            'local p=c.surface.find_non_colliding_position("tree-01",c.position,8,0.5);'
            'assert(p,"M20-AS cannot place wood recovery tree");'
            'e=c.surface.create_entity{name="tree-01",position=p};'
            "assert(e);storage.m20as_tree=e end;"
            'rcon.print("m20as_fixture:"..helpers.table_to_json({position=e.position}))'
        )["position"]
        try:
            action("move_to", {"surface": "nauvis", **tree, "within": 2})
            observed = bridge.call("observe_nearby", {"types": ["tree"]})["result"]["entities"]
            target = next(
                entity
                for entity in observed
                if entity["name"] == "tree-01"
                and entity["x"] == tree["x"]
                and entity["y"] == tree["y"]
            )
            action("mine_entity", {"target_ref": target["target_ref"], "count": 1})
        finally:
            escape_fixture(
                "local e=storage.m20as_tree;if e and e.valid then e.destroy() end;"
                "storage.m20as_tree=nil;rcon.print('m20as_fixture:{\"removed\":true}')"
            )

    try:
        character_dimensions = escape_fixture(
            'rcon.print("m20as_fixture:"..helpers.table_to_json({box=c.prototype.collision_box}))'
        )["box"]
        character_height = (
            character_dimensions["right_bottom"]["y"] - character_dimensions["left_top"]["y"]
        )
        attempts = []
        setup = None
        upper_items = [
            name
            for name in ("wooden-chest", "iron-chest", "burner-inserter", "small-electric-pole")
            if before_escape.get(name, 0) > 0
        ]
        if "wooden-chest" not in upper_items:
            # Craft only if no held collider reproduces the slot.
            upper_items.append("wooden-chest")
        for upper_item in upper_items:
            for x, y in ((12, -4), (16, 4), (-18, 8)):
                action("move_to", {"surface": "nauvis", "x": x - 3, "y": y + 1})
                bridge.call("observe_nearby")
                chest_params = {"item": upper_item, "surface": "nauvis", "x": x + 0.5, "y": y + 0.5}
                chest_check = bridge.call("check_placement", chest_params)["result"]
                for lower_item in ("burner-mining-drill", "stone-furnace"):
                    lower_params = {"item": lower_item, "surface": "nauvis", "x": x + 1, "y": y + 2}
                    lower_check = bridge.call("check_placement", lower_params)["result"]
                    goal = {"surface": "nauvis", "x": x - 5.5, "y": y + 1.5}
                    goal_check = bridge.call("check_placement", {**goal, "item": upper_item})[
                        "result"
                    ]
                    measured = {"chest": chest_check, "lower": lower_check, "goal": goal_check}
                    attempts.append(measured)
                    if not all(
                        check.get("placeable") and not check.get("ground_items")
                        for check in measured.values()
                    ):
                        continue
                    if before_escape.get(lower_item, 0) < 1:
                        continue
                    chest_box, lower_box = chest_check["bounding_box"], lower_check["bounding_box"]
                    gap = lower_box["left_top"]["y"] - chest_box["right_bottom"]["y"]
                    # Factorio quantizes collision boxes to 1/256 tile.
                    if not math.isclose(gap, 0.45, abs_tol=1 / 256) or not (
                        0 < character_height < gap
                    ):
                        continue
                    slot = {
                        "x": lower_box["left_top"]["x"] - 1 / 128,
                        "y": chest_box["right_bottom"]["y"]
                        - character_dimensions["left_top"]["y"]
                        + (gap - character_height) / 4,
                    }
                    setup = (chest_params, lower_params, chest_box, lower_box, slot, goal)
                    break
                if setup:
                    break
            if setup:
                break
        if setup is None:
            raise RuntimeError(
                "M20-AS cannot reproduce slot; measured boxes: " + json.dumps(attempts)
            )
        chest_params, lower_params, chest_box, lower_box, slot, goal = setup
        if not before_escape.get(chest_params["item"], 0):
            if items().get("wood", 0) < 2:
                mine_escape_wood()
            action("craft", {"recipe": "wooden-chest", "count": 1})
            crafted_escape_chest = True
            # Resource recovery may have moved us away from the selected placement.
            action("move_to", {"surface": "nauvis", "x": x - 3, "y": y + 1})
            bridge.call("observe_nearby")
        for params in (chest_params, lower_params):
            placed = bridge.call("place_entity", params)["result"]
            escape_targets.append(placed)
        positioned = escape_fixture(
            "assert(not state.action and not state.craft_action);"
            f"local ok=c.teleport({{x={slot['x']},y={slot['y']}}});"
            'rcon.print("m20as_fixture:"..helpers.table_to_json({'
            "positioned=ok,position=c.position,character_box=c.bounding_box}))"
        )
        # Check actual entity boxes too, rather than relying only on the preview.
        observed = bridge.call("observe_nearby")["result"]["entities"]
        actual_boxes = [
            next(
                entity["bounding_box"]
                for entity in observed
                if entity["target_ref"] == target["target_ref"]
            )
            for target in escape_targets
        ]
        character_box = positioned["character_box"]
        measured = {**positioned, "chest": actual_boxes[0], "lower": actual_boxes[1]}
        valid_slot = (
            positioned.get("positioned") is True
            and actual_boxes == [chest_box, lower_box]
            and math.isclose(
                lower_box["left_top"]["y"] - chest_box["right_bottom"]["y"],
                0.45,
                abs_tol=1 / 256,
            )
            and math.isclose(
                character_box["right_bottom"]["y"] - character_box["left_top"]["y"],
                0.4,
                abs_tol=1 / 256,
            )
            and chest_box["right_bottom"]["y"] < character_box["left_top"]["y"]
            and character_box["right_bottom"]["y"] < lower_box["left_top"]["y"]
            and all(not overlaps(character_box, box) for box in actual_boxes)
            and all(
                character_box["left_top"]["x"] < box["right_bottom"]["x"]
                and character_box["right_bottom"]["x"] > box["left_top"]["x"]
                for box in actual_boxes
            )
        )
        record(
            "escape fixture reproduces non-overlapping trial 14 slot",
            valid_slot,
            json.dumps(measured),
        )
        if not valid_slot:
            raise RuntimeError("M20-AS invalid slot; measured boxes: " + json.dumps(measured))
        # Start via MCP, then retain a read-only reference to the live action for diagnostics.
        started = asyncio.run(server.call_tool("move_to", {**goal, "within": 0.5}))
        value = json.loads(started.content[0].text)
        escape_action_id = value.get("action_id")
        if not escape_action_id:
            raise RuntimeError("M20-AS move did not start: " + json.dumps(value))
        escape_fixture(
            "storage.m20as_action=state.action or state.last_action;"
            "rcon.print('m20as_fixture:{\"watching\":true}')"
        )
        waited = asyncio.run(
            server.call_tool(
                "wait_for_action", {"action_id": escape_action_id, "timeout_seconds": 20}
            )
        )
        value = json.loads(waited.content[0].text)
        diagnostics = escape_fixture(
            "local a=storage.m20as_action;"
            'rcon.print("m20as_fixture:"..helpers.table_to_json({'
            "escape_tried=a and a.escape_tried or false,"
            "start_adjusted=a and (a.path_start_adjusted or a.detail and a.detail.start_adjusted),"
            "retries=a and a.retries}))"
        )
        diagnostics["start_adjusted_appeared"] = bool(diagnostics.get("start_adjusted"))
        position = bridge.call("get_agent_status")["result"]["position"]
        record(
            "move walks out of trial 14 slot to open goal",
            not started.is_error
            and not waited.is_error
            and value.get("status") == "succeeded"
            and math.hypot(position["x"] - goal["x"], position["y"] - goal["y"]) <= 0.5,
            json.dumps({"result": value, "position": position, **diagnostics}),
        )
    finally:
        if escape_action_id:
            current = bridge.call("get_action_status", {"action_id": escape_action_id})["result"]
            if current.get("status") == "running":
                bridge.call("cancel_action", {"action_id": escape_action_id})
        escape_fixture("storage.m20as_action=nil;rcon.print('m20as_fixture:{\"removed\":true}')")
        for target in escape_targets:
            position = bridge.call("get_agent_status")["result"]["position"]
            if (
                math.hypot(
                    position["x"] - target["position"]["x"],
                    position["y"] - target["position"]["y"],
                )
                > 4
            ):
                action("move_to", {"surface": "nauvis", "x": x - 3, "y": y + 1})
            # Chest first: a failed escape must be freed without another teleport.
            action("mine_entity", {"target_ref": target["target_ref"], "count": 1})
        if crafted_escape_chest:
            if items().get("wood", 0) < before_escape.get("wood", 0):
                mine_escape_wood()
            # Dispose of the newly crafted chest and surplus tree yield together.
            # Real placement/transfers restore the character's original inventory;
            # the harness removes only this expendable fixture, never held stock.
            action("move_to", {"surface": "nauvis", "x": x - 3, "y": y + 1})
            bridge.call("observe_nearby")
            disposal = bridge.call("place_entity", chest_params)["result"]
            try:
                surplus = items().get("wood", 0) - before_escape.get("wood", 0)
                if surplus > 0:
                    moved = bridge.call(
                        "transfer_item",
                        {
                            "target_ref": disposal["target_ref"],
                            "direction": "to_entity",
                            "inventory_role": "main",
                            "item": "wood",
                            "count": surplus,
                        },
                    )["result"]
                    if moved.get("moved") != surplus:
                        raise RuntimeError(
                            "M20-AS could not store surplus wood: " + json.dumps(moved)
                        )
            finally:
                escape_fixture(
                    f'local t=state.targets["{disposal["target_ref"]}"];'
                    'if t and t.entity.valid then assert(t.entity.name=="wooden-chest");'
                    "t.entity.destroy() end;rcon.print('m20as_fixture:{\"disposed\":true}')"
                )
        action("move_to", {**return_position, "within": 0.5})
        after_escape = items()
        record(
            "escape fixture restores the pre-fixture inventory exactly",
            after_escape == before_escape,
            json.dumps({"before": before_escape, "after": after_escape}),
        )

    # M22-AZ/AY: use held buildings and ordinary walking in a clear fixture area.
    # AY is a corner-navigation smoke test: pathfinder waypoint choices can vary,
    # so this geometry cannot guarantee that the old steering clips on every save.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    before_m22 = items()
    m22_targets = []
    m22_action_id = None

    def movement_fixture(body):
        # Read-only collision probes; no spawning, teleporting or inventory changes.
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;"
            "local c=state and state.character;assert(c and c.valid);" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=5, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
            if "m22_fixture:" not in raw:
                raw = client.run(command)
        if "m22_fixture:" not in raw:
            raise RuntimeError("Missing M22 fixture response: " + raw)
        return json.loads(raw.split("m22_fixture:", 1)[1].strip())

    def place_movement_obstacle(item):
        for x, y in ((-18, -18), (12, -18), (-18, 18), (12, 18)):
            action("move_to", {"surface": "nauvis", "x": x + 5, "y": y - 3})
            params = {"item": item, "surface": "nauvis", "x": x, "y": y}
            preview = bridge.call("check_placement", params)["result"]
            if not preview.get("placeable") or preview.get("ground_items"):
                continue
            centre = preview["position"]
            clear = movement_fixture(
                f"local a={{x={centre['x']},y={centre['y']}}};local clear=true;"
                "for dx=-7,6 do for dy=-3,4 do local p={x=a.x+dx,y=a.y+dy};"
                "local d=(p.x-c.position.x)^2+(p.y-c.position.y)^2;"
                'if d>0.25 and not c.surface.can_place_entity{name="character",position=p} '
                "then clear=false end end end;"
                'rcon.print("m22_fixture:"..helpers.table_to_json({clear=clear}))'
            )
            if clear["clear"]:
                placed = bridge.call("place_entity", params)["result"]
                m22_targets.append(placed)
                return placed
        raise RuntimeError("M22 cannot find a clear movement fixture for " + item)

    def finish_movement(params):
        nonlocal m22_action_id
        started = bridge.call("move_to", params)["result"]
        m22_action_id = started["action_id"]
        running = bridge.call("get_action_status", {"action_id": m22_action_id})["result"]
        waited = asyncio.run(
            server.call_tool("wait_for_action", {"action_id": m22_action_id, "timeout_seconds": 20})
        )
        return started, running, json.loads(waited.content[0].text)

    def recover_movement_obstacle(target):
        nonlocal m22_action_id
        if m22_action_id:
            current = bridge.call("get_action_status", {"action_id": m22_action_id})["result"]
            if current.get("status") == "running":
                bridge.call("cancel_action", {"action_id": m22_action_id})
            m22_action_id = None
        reach = bridge.call("observe_nearby")["result"]["reach"]["mine_resource"]
        position = bridge.call("get_agent_status")["result"]["position"]
        centre = target["position"]
        if math.hypot(position["x"] - centre["x"], position["y"] - centre["y"]) > reach:
            action("move_to", {**centre, "surface": "nauvis", "within": reach})
        action("mine_entity", {"target_ref": target["target_ref"], "count": 1})
        m22_targets.remove(target)

    try:
        if not before_m22.get("stone-furnace"):
            raise RuntimeError("M22-AZ requires a held stone furnace")
        target = place_movement_obstacle("stone-furnace")
        centre = target["position"]
        action("move_to", {**centre, "x": centre["x"] + 5.2, "y": centre["y"] - 1.1})
        goal = {"x": centre["x"], "y": centre["y"]}
        started, running, final = finish_movement({"surface": "nauvis", **goal})
        adjusted = started.get("goal_adjusted")
        position = bridge.call("get_agent_status")["result"]["position"]
        record(
            "non-standable furnace goal walks to goal_adjusted",
            final.get("status") == "succeeded"
            and isinstance(adjusted, dict)
            and adjusted != goal
            and started.get("original_goal") == goal
            and all(
                value.get("goal_adjusted") == adjusted and value.get("original_goal") == goal
                for value in (running.get("detail") or {}, final, final.get("detail") or {})
            )
            and math.hypot(position["x"] - adjusted["x"], position["y"] - adjusted["y"]) <= 0.5,
            json.dumps({"start": started, "running": running, "final": final}),
        )
        # Move away before can_place_entity: the character itself occupies the arrival spot.
        action("move_to", {**centre, "x": centre["x"] + 5.2, "y": centre["y"] - 1.1})
        if isinstance(adjusted, dict):
            standable = movement_fixture(
                'rcon.print("m22_fixture:"..helpers.table_to_json({standable='
                'c.surface.can_place_entity{name="character",'
                f"position={{x={adjusted['x']},y={adjusted['y']}}}" + "}}))"
            )
            record("goal_adjusted is standable", standable.get("standable"), json.dumps(standable))
        recover_movement_obstacle(target)

        corner_item = (
            "assembling-machine-1" if before_m22.get("assembling-machine-1") else "stone-furnace"
        )
        target = place_movement_obstacle(corner_item)
        centre = target["position"]
        action("move_to", {**centre, "x": centre["x"] + 5.2, "y": centre["y"] - 1.1})
        goal = {"surface": "nauvis", "x": centre["x"] - 6, "y": centre["y"] + 3}
        _, _, final = finish_movement(goal)
        record(
            "path-following walks around a building corner (smoke test)",
            final.get("status") == "succeeded",
            json.dumps({"item": corner_item, "centre": centre, "goal": goal, "result": final}),
        )
        recover_movement_obstacle(target)
    finally:
        for target in list(m22_targets):
            recover_movement_obstacle(target)
        action("move_to", {**return_position, "within": 0.5})
        record(
            "M22 movement fixtures restore all held items",
            items() == before_m22,
            json.dumps({"before": before_m22, "after": items()}),
        )

    # M23-BF: check frontier against cells derived from actual observation responses.
    bridge.call("observe_nearby")
    mapped = bridge.call("get_explored_map", {"limit": 50})["result"]
    frontier = mapped["frontier"]
    size, position = mapped["cell_size"], mapped["position"]
    explored = mapped["explored"]
    known_cells = observed_map_cells[position["surface"]]
    left, top = explored["left_top"]["x"], explored["left_top"]["y"]
    right, bottom = explored["right_bottom"]["x"], explored["right_bottom"]["y"]
    offsets = ((-1, 0), (1, 0), (0, -1), (0, 1))
    frontier_keys = {
        (math.floor(cell["x"] / size), math.floor(cell["y"] / size)) for cell in frontier
    }

    def valid_frontier_cell(cell):
        x, y = cell["x"], cell["y"]
        cx, cy = math.floor(x / size), math.floor(y / size)
        # A save may contain observations from production_line.py before this script.
        # For those cells, use the permitted bounds fallback without reading the world.
        outside_bounds = not (left <= x < right and top <= y < bottom)
        adjacent = any((cx + dx, cy + dy) in known_cells for dx, dy in offsets)
        bounds_adjacent = outside_bounds and any(
            left <= x + dx * size < right and top <= y + dy * size < bottom for dx, dy in offsets
        )
        return (
            set(cell) == {"x", "y", "distance"}
            and x == (cx + 0.5) * size
            and y == (cy + 0.5) * size
            and (cx, cy) not in known_cells
            and (adjacent or bounds_adjacent)
            and math.isclose(
                cell["distance"],
                math.hypot(x - position["x"], y - position["y"]),
                rel_tol=0,
                abs_tol=1e-6,
            )
        )

    record(
        "frontier cells are unexplored edge neighbors with only coordinates and distance",
        size == 8
        and bool(frontier)
        and len(frontier_keys) == len(frontier)
        and all(valid_frontier_cell(cell) for cell in frontier),
        json.dumps(mapped),
    )
    record(
        "frontier distances are sorted with coordinate tie breaks",
        frontier == sorted(frontier, key=lambda cell: (cell["distance"], cell["x"], cell["y"])),
        json.dumps(frontier),
    )
    if len(known_cells) == explored["cells"]:
        # All remembered cells were observed during this script: verify the exact set.
        expected = {(cx + dx, cy + dy) for cx, cy in known_cells for dx, dy in offsets}
        expected -= known_cells
        expected = sorted(
            expected,
            key=lambda cell: (
                math.hypot(
                    (cell[0] + 0.5) * size - position["x"], (cell[1] + 0.5) * size - position["y"]
                ),
                cell[0],
                cell[1],
            ),
        )
        record(
            "frontier matches all observed cell edges before truncation",
            mapped["frontier_total"] == len(expected) and frontier_keys == set(expected[:50]),
        )
    limited = bridge.call("get_explored_map", {"limit": 1, "resource_names": []})["result"]
    record(
        "frontier respects limit and ignores resource filtering",
        len(frontier) == min(50, mapped["frontier_total"])
        and limited["frontier_total"] == mapped["frontier_total"]
        and limited["frontier"] == frontier[:1],
        json.dumps(limited),
    )

    # M25-BH/BI: isolate sightings from earlier full observations in this save.
    # Only the disposable-save harness creates/teleports; the tools still observe and walk.
    return_position = bridge.call("get_agent_status")["result"]["position"]
    before_m25 = items()
    m25_surface = "m25-partial-map-check"
    m25_action_id = None

    def partial_map_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;"
            "local c=state and state.character;assert(c and c.valid);"
            f'local name="{m25_surface}";local s=game.surfaces[name];' + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
            if "m25_fixture:" not in raw:
                raw = client.run(command)
        if "m25_fixture:" not in raw:
            raise RuntimeError("Missing M25 fixture response: " + raw)
        return json.loads(raw.split("m25_fixture:", 1)[1].strip())

    def coal_map():
        return bridge.call("get_explored_map", {"limit": 50, "resource_names": ["coal"]})["result"]

    def coal_summary(observation):
        return next((entry for entry in observation["resources"] if entry["name"] == "coal"), None)

    try:
        setup = partial_map_fixture(
            "assert(not state.action and not state.craft_action);"
            "if not s then s=game.create_surface(name,{width=64,height=64});"
            "s.request_to_generate_chunks({0,0},1);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-32,31 do for y=-32,31 do "
            'tiles[#tiles+1]={name="grass-1",position={x,y}} end end;s.set_tiles(tiles);'
            "s.peaceful_mode=true end;"
            "for _,x in ipairs({3.5,5.5}) do "
            'local e=s.find_entity("coal",{x,4.5}) or '
            's.create_entity{name="coal",position={x,4.5},amount=100};assert(e) end;'
            "assert(c.teleport({4,4},s));"
            # Exercise the legacy shape with no partial_cells/partial_count fields.
            "state.explored_map[name]=state.explored_map[name] or {cells={},count=0};"
            "rcon.print('m25_fixture:{\"ready\":true}')"
        )
        initial = coal_map()
        if setup != {"ready": True} or initial["explored"]["cells"] != 0 or initial["resources"]:
            raise RuntimeError("M25 requires an empty explored map: " + json.dumps(initial))

        observed = bridge.call("observe_nearby", {"radius": 1})["result"]
        seen = coal_summary(observed)
        if not seen or seen["tiles"] != 1:
            raise RuntimeError(
                "M25 cannot see exactly one partial-cell coal tile: " + json.dumps(observed)
            )
        mapped = coal_map()
        record(
            "partial resource sightings retain exactly the seen tile and amount totals",
            len(mapped["resources"]) == 1
            and mapped["resources"][0].get("partial") is True
            and mapped["resources"][0]["tiles"] == seen["tiles"]
            and mapped["resources"][0]["total_amount"] == seen["total_amount"],
            json.dumps(mapped),
        )
        record(
            "partial-only cells do not add explored bounds, terrain or frontier edges",
            mapped["explored"] == {"cells": 0}
            and mapped["frontier_total"] == 0
            and not mapped["frontier"]
            and all(not mapped[kind] for kind in ("water", "trees", "rocks")),
            json.dumps(mapped),
        )

        observed = bridge.call("observe_nearby", {"radius": 2})["result"]
        seen = coal_summary(observed)
        if not seen or seen["tiles"] != 2:
            raise RuntimeError("M25 cannot see both coal fixture tiles: " + json.dumps(observed))
        larger = coal_map()["resources"]
        bridge.call("observe_nearby", {"radius": 1})
        record(
            "partial re-observation keeps the larger resource tile sum",
            len(larger) == 1 and larger[0]["tiles"] == 2 and coal_map()["resources"] == larger,
            json.dumps(larger),
        )
        partial_map_fixture(
            'for _,e in pairs(s.find_entities_filtered{type="resource"}) do e.amount=77 end;'
            "rcon.print('m25_fixture:{\"updated\":true}')"
        )
        bridge.call("observe_nearby", {"radius": 2})
        equal = coal_map()["resources"]
        record(
            "equal partial tile sums replace the old amounts",
            len(equal) == 1 and equal[0]["tiles"] == 2 and equal[0]["total_amount"] == 154,
            json.dumps(equal),
        )
        action("move_to", {"surface": m25_surface, "x": 4, "y": 7, "within": 0.5})
        empty = bridge.call("observe_nearby", {"radius": 1})["result"]
        after_empty = coal_map()["resources"]
        record(
            "empty partial observations preserve earlier resources",
            coal_summary(empty) is None
            and len(after_empty) == 1
            and after_empty[0].get("partial") is True
            and all(
                after_empty[0][key] == equal[0][key]
                for key in ("tiles", "total_amount", "observed_tick")
            ),
            json.dumps(empty),
        )
        action("move_to", {"surface": m25_surface, "x": 4, "y": 4, "within": 0.5})
        partial_map_fixture(
            'local e=s.find_entity("coal",{8.5,4.5}) or '
            's.create_entity{name="coal",position={8.5,4.5},amount=77};assert(e);'
            "rcon.print('m25_fixture:{\"neighbor\":true}')"
        )
        bridge.call("observe_nearby", {"radius": 6.5})
        mixed = coal_map()
        record(
            "partial resources join full neighbors while remaining in frontier",
            mixed["explored"]
            == {"cells": 1, "left_top": {"x": 0, "y": 0}, "right_bottom": {"x": 8, "y": 8}}
            and len(mixed["resources"]) == 1
            and mixed["resources"][0].get("partial") is True
            and mixed["resources"][0]["cells"] == 2
            and mixed["resources"][0]["tiles"] == 3
            and mixed["resources"][0]["total_amount"] == 231
            and any(cell["x"] == 12 and cell["y"] == 4 for cell in mixed["frontier"]),
            json.dumps(mixed),
        )
        bridge.call("observe_nearby", {"radius": 16})
        full = coal_map()
        record(
            "full observation replaces partial resources and counts the explored cell",
            len(full["resources"]) == 1
            and not full["resources"][0].get("partial", False)
            and full["resources"][0]["tiles"] == 3
            and full["resources"][0]["total_amount"] == 231
            and full["explored"]["cells"] > 0,
            json.dumps(full),
        )
        bridge.call("observe_nearby", {"radius": 1})
        record(
            "partial observations never overwrite full resources",
            coal_map()["resources"] == full["resources"],
        )
        partial_map_fixture(
            'for _,e in pairs(s.find_entities_filtered{type="resource"}) do e.destroy() end;'
            "rcon.print('m25_fixture:{\"depleted\":true}')"
        )
        bridge.call("observe_nearby", {"radius": 16})
        record("full re-observation still forgets depleted resources", not coal_map()["resources"])

        started = bridge.call("move_to", {"surface": m25_surface, "x": 20, "y": 4})["result"]
        m25_action_id = started["action_id"]
        running = bridge.call("get_agent_status")["result"].get("action") or {}
        record(
            "running move status includes action_id equal to id",
            running.get("status") == "running"
            and running.get("action_id") == running.get("id") == m25_action_id,
            json.dumps(running),
        )
    finally:
        if m25_action_id:
            current = bridge.call("get_action_status", {"action_id": m25_action_id})["result"]
            if current.get("status") == "running":
                bridge.call("cancel_action", {"action_id": m25_action_id})
        partial_map_fixture(
            "assert(not state.action and not state.craft_action);"
            f"assert(c.teleport({{x={return_position['x']},y={return_position['y']}}},"
            f"game.surfaces[{json.dumps(return_position['surface'])}]));"
            "if s then assert(game.delete_surface(s)) end;state.explored_map[name]=nil;"
            "rcon.print('m25_fixture:{\"removed\":true}')"
        )
        observed_map_cells.pop(m25_surface, None)
        record("M25 fixture preserves held items", items() == before_m25)

    # M26-BJ: use an isolated, explicitly cleared surface and stash the *actual*
    # original stacks, including metadata and slots. Fixture items never consume
    # the production/logistics scripts' wood, belts, or other materials.
    m26_surface = "m26-place-line-check"
    before_m26 = bridge.call("get_inventory")["result"]["items"]
    m26_return_position = bridge.call("get_agent_status")["result"]["position"]

    def line_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;"
            "local c=state and state.character;assert(c and c.valid);"
            f'local name="{m26_surface}";local s=game.surfaces[name];'
            "local inv=c.get_main_inventory();assert(inv);" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            # Do not retry mutations: a missing acknowledgement must fail loudly.
            raw = client.run(command)
        if "m26_fixture:" not in raw:
            raise RuntimeError("M26 line fixture failed: " + raw)
        return json.loads(raw.split("m26_fixture:", 1)[1].strip())

    def reset_line_fixture(belts=80, extra=""):
        return line_fixture(
            "assert(s and not state.action and not state.craft_action);"
            "for _,e in pairs(s.find_entities()) do "
            'if e.type~="character" then e.destroy() end end;'
            "assert(c.teleport({0.5,3.5},s));inv.clear();"
            f'assert(inv.insert{{name="transport-belt",count={belts}}}=={belts});'
            + extra
            + "rcon.print('m26_fixture:{\"reset\":true}')"
        )

    def line(start=(0.5, 0.5), end=(5.5, 0.5), item="transport-belt"):
        return {
            "item": item,
            "surface": m26_surface,
            "from_x": start[0],
            "from_y": start[1],
            "to_x": end[0],
            "to_y": end[1],
        }

    def fixture_belts():
        return line_fixture(
            'local found={};for _,e in pairs(s.find_entities_filtered{type="transport-belt"}) '
            "do found[#found+1]={x=e.position.x,y=e.position.y,direction=e.direction} end;"
            "rcon.print('m26_fixture:'..helpers.table_to_json({belts=found}))"
        )["belts"]

    try:
        line_fixture(
            "assert(not state.action and not state.craft_action);"
            "assert(not s and not storage.m26_line_backup,'M26 fixture already exists');"
            "storage.m26_line_backup={position={x=c.position.x,y=c.position.y},"
            "surface=c.surface,inventory=game.create_inventory(#inv)};"
            "local backup=storage.m26_line_backup.inventory;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(backup[i].swap_stack(inv[i])) end end;storage.m26_line_backup.stashed=true;"
            "s=game.create_surface(name,{width=128,height=128});s.peaceful_mode=true;"
            "s.request_to_generate_chunks({0,0},2);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-64,63 do for y=-64,63 do "
            'tiles[#tiles+1]={name="grass-1",position={x,y}} end end;s.set_tiles(tiles);'
            "assert(c.teleport({0.5,3.5},s),'M26 cannot find a clear test location');"
            "assert(c.build_distance==10,'M26 expects build reach 10');"
            "rcon.print('m26_fixture:{\"ready\":true}')"
        )
        for start, end, direction in (
            ((-6.2, 0.1), (-1.1, 0.9), 4),
            ((0.5, -4.5), (0.5, 0.5), 8),
            ((5.5, 0.5), (0.5, 0.5), 12),
            ((0.5, 0.5), (0.5, -4.5), 0),
        ):
            reset_line_fixture()
            params = line(start, end)
            request_id = f"m26-line-{direction}-{time.time_ns()}"
            response = send_with_id("place_line", params, request_id)
            built = response["result"]
            belts = fixture_belts()
            fx, fy = math.floor(start[0]) + 0.5, math.floor(start[1]) + 0.5
            tx, ty = math.floor(end[0]) + 0.5, math.floor(end[1]) + 0.5
            dx, dy = (tx - fx) / 5, (ty - fy) / 5
            expected = {(fx + i * dx, fy + i * dy, direction) for i in range(6)}
            record(
                f"M26 straight line direction {direction} builds each belt with travel direction",
                built.get("complete") is True
                and built["requested"] == built["placed"] == 6
                and built["already_present"] == 0
                and built["direction"] == direction
                and built["first_position"] == {"x": fx, "y": fy}
                and built["last_position"] == {"x": tx, "y": ty}
                and "next_position" not in built
                and {(b["x"], b["y"], b["direction"]) for b in belts} == expected,
                json.dumps(built),
            )
            after_first = items()
            record(
                "M26 request id replay returns cached progress without consuming more belts",
                send_with_id("place_line", params, request_id) == response
                and items() == after_first
                and bridge.call("get_request_result", {"request_id": request_id})["result"][
                    "original_response"
                ]
                == response,
            )
            repeated = bridge.call("place_line", params)["result"]
            record(
                "M26 repeated line skips every belt",
                repeated["placed"] == 0
                and repeated["already_present"] == repeated["requested"] == 6
                and repeated["complete"] is True
                and items() == after_first,
                json.dumps(repeated),
            )

        reset_line_fixture()
        params = line(end=(25.5, 0.5))
        partial = bridge.call("place_line", params)["result"]
        next_tile = {"x": 0.5 + partial["placed"], "y": 0.5}
        record(
            "M26 reach stop identifies the first unplaced tile and leaves later tiles empty",
            partial["complete"] is False
            and partial["stopped"] == "out_of_range"
            and 0 < partial["placed"] < partial["requested"] == 26
            and partial["next_position"] == next_tile
            and partial["last_position"] == {"x": next_tile["x"] - 1, "y": 0.5}
            and {(b["x"], b["y"]) for b in fixture_belts()}
            == {(0.5 + i, 0.5) for i in range(partial["placed"])},
            json.dumps(partial),
        )
        action(
            "move_to",
            {"surface": m26_surface, "x": (next_tile["x"] + 25.5) / 2, "y": 3.5, "within": 0.5},
        )
        resumed = bridge.call("place_line", line((next_tile["x"], next_tile["y"]), (25.5, 0.5)))[
            "result"
        ]
        record(
            "M26 walking near next_position and resuming finishes the line",
            resumed["complete"] is True
            and resumed["placed"] + partial["placed"] == 26
            and len(fixture_belts()) == 26,
            json.dumps(resumed),
        )

        reset_line_fixture(
            extra='assert(s.create_entity{name="wooden-chest",position={3.5,0.5},force=c.force});'
        )
        blocked = bridge.call("place_line", line())["result"]
        record(
            "M26 chest obstacle stops before its tile and preserves the prefix",
            blocked["complete"] is False
            and blocked["stopped"] == "blocked"
            and blocked["placed"] == 3
            and blocked["next_position"] == {"x": 3.5, "y": 0.5}
            and {b["x"] for b in fixture_belts()} == {0.5, 1.5, 2.5},
            json.dumps(blocked),
        )
        reset_line_fixture(
            extra='assert(s.create_entity{name="transport-belt",position={3.5,0.5},'
            "direction=0,force=c.force});"
        )
        turned = bridge.call("place_line", line())["result"]
        record(
            "M26 different belt direction stops with its existing direction",
            turned["complete"] is False
            and turned["stopped"] == "blocked"
            and turned["placed"] == 3
            and turned["next_position"] == {"x": 3.5, "y": 0.5}
            and turned["detail"]["reason"] == "different_direction"
            and turned["detail"]["existing_direction"] == 0,
            json.dumps(turned),
        )
        reset_line_fixture(belts=2)
        short = bridge.call("place_line", line())["result"]
        record(
            "M26 missing belts stops after consuming only the available prefix",
            short["complete"] is False
            and short["stopped"] == "missing_materials"
            and short["placed"] == 2
            and short["next_position"] == {"x": 2.5, "y": 0.5}
            and items().get("transport-belt", 0) == 0
            and {b["x"] for b in fixture_belts()} == {0.5, 1.5},
            json.dumps(short),
        )
        expect_error(
            "M26 diagonal line rejected", "place_line", line(end=(3.5, 2.5)), {"invalid_input"}
        )
        expect_error(
            "M26 non-line item rejected",
            "place_line",
            line(item="wooden-chest"),
            {"invalid_target"},
        )
        record(
            "M26 non-line item explains not_line_placeable",
            errors["M26 non-line item rejected"].detail.get("reason") == "not_line_placeable",
        )

        reset_line_fixture(
            extra='assert(inv.insert{name="pipe",count=3}==3);'
            'assert(s.create_entity{name="pipe",position={1.5,0.5},direction=8,force=c.force});'
        )
        pipes = bridge.call("place_line", line(end=(2.5, 0.5), item="pipe"))["result"]
        record(
            "M26 existing pipe ignores direction",
            pipes["complete"] is True and pipes["placed"] == 2 and pipes["already_present"] == 1,
            json.dumps(pipes),
        )
    finally:
        line_fixture(
            "local b=storage.m26_line_backup;"
            "if b then "
            "if state.action then c.walking_state={walking=false};state.action=nil end;"
            "assert(not state.craft_action);"
            "assert(c.teleport(b.position,b.surface),'M26 could not restore character position');"
            "if b.stashed then inv.clear() end;"
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            "b.inventory.destroy();storage.m26_line_backup=nil;"
            "if s then assert(game.delete_surface(s)) end;state.explored_map[name]=nil end;"
            "rcon.print('m26_fixture:{\"restored\":true}')"
        )
        observed_map_cells.pop(m26_surface, None)
        record(
            "M26 restores the exact original inventory and character position",
            bridge.call("get_inventory")["result"]["items"] == before_m26
            and bridge.call("get_agent_status")["result"]["position"] == m26_return_position,
        )

    # M26-BK/BL: isolated machines and enough belts to exercise compact paging.
    status_surface = "m26-status-compact-check"
    status_inventory = bridge.call("get_inventory")["result"]["items"]
    status_position = bridge.call("get_agent_status")["result"]["position"]

    def status_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;"
            "local c=state and state.character;assert(c and c.valid);"
            f'local name="{status_surface}";local s=game.surfaces[name];'
            "local inv=c.get_main_inventory();assert(inv);" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
        if "m26_status:" not in raw:
            raise RuntimeError("M26 status fixture failed: " + raw)
        return json.loads(raw.split("m26_status:", 1)[1].strip())

    try:
        status_fixture(
            "assert(not state.action and not state.craft_action);"
            "assert(not s and not storage.m26_status_backup,'M26 status fixture already exists');"
            "local b={position={x=c.position.x,y=c.position.y},surface=c.surface,"
            "inventory=game.create_inventory(#inv)};storage.m26_status_backup=b;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(b.inventory[i].swap_stack(inv[i])) end end;"
            "s=game.create_surface(name,{width=128,height=128});s.peaceful_mode=true;"
            "s.request_to_generate_chunks({0,0},2);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-64,63 do for y=-64,63 do "
            'tiles[#tiles+1]={name="grass-1",position={x,y}} end end;s.set_tiles(tiles);'
            "assert(c.teleport({0.5,3.5},s));assert(c.reach_distance<13);"
            'local ore=s.create_entity{name="iron-ore",position={-2.5,0.5},amount=1000000};'
            "assert(ore and ore.valid,'M26 requires a real resource under the drill');"
            'b.drill=s.create_entity{name="electric-mining-drill",position={-2.5,0.5},'
            "direction=0,force=c.force};assert(b.drill and b.drill.valid);"
            # Keep the drill supplied while it acquires its mining target. No fixture
            # power network is needed; all writes are confined to this test surface.
            "b.drill.energy=1000000;"
            'b.inserter=s.create_entity{name="inserter",position={3.5,3.5},'
            "direction=0,force=c.force};assert(b.inserter);b.inserter.active=false;"
            'assert(s.create_entity{name="burner-inserter",position={5.5,3.5},force=c.force});'
            'assert(s.create_entity{name="burner-mining-drill",position={-2,-4},force=c.force});'
            # Walls remain unsupported by inventory reads after M37's fluid diagnostics.
            'local wall=s.create_entity{name="stone-wall",position={0.5,0.5},force=c.force};'
            "assert(wall and c.can_reach_entity(wall));"
            'assert(s.create_entity{name="electric-mining-drill",position={13.5,3.5},'
            "force=c.force});"
            'assert(s.create_entity{name="inserter",position={6.5,3.5},force="enemy"});'
            "for x=-5,5 do for y=-13,-4 do "
            # Skip the burner drill's occupied tiles, while retaining >100 entities.
            "if not (x>=-3 and x<=-2 and y>=-5 and y<=-4) then "
            'assert(s.create_entity{name="transport-belt",position={x+0.5,y+0.5},'
            "direction=4,force=c.force}) end end end;"
            # A blocked tile verifies compact does not change tile results.
            's.set_tiles{{name="water",position={8,8}}};'
            "rcon.print('m26_status:{\"ready\":true}')"
        )
        # mining_target may be assigned on a subsequent simulation tick. Fail loudly
        # if this fixture cannot get the drill targeting the resource it just placed.
        deadline = time.monotonic() + 5
        while True:
            target = status_fixture(
                "local d=storage.m26_status_backup.drill;assert(d and d.valid);"
                "d.energy=1000000;local t=d.mining_target;"
                "if t and t.valid then d.energy=0 end;"
                "rcon.print('m26_status:'..helpers.table_to_json({"
                "resource=t and t.valid and t.name or nil}))"
            )
            if target.get("resource") == "iron-ore":
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("M26 electric drill never acquired the placed iron-ore resource")
            time.sleep(0.1)

        near = bridge.call(
            "observe_nearby",
            {"names": ["electric-mining-drill", "inserter", "stone-wall"]},
        )["result"]["entities"]
        drill = next(e for e in near if e["name"] == "electric-mining-drill" and e["x"] < 0)
        far_drill = next(e for e in near if e["name"] == "electric-mining-drill" and e["x"] > 10)
        inserter = next(e for e in near if e["name"] == "inserter" and e["owned"])
        enemy = next(e for e in near if e["name"] == "inserter" and not e["owned"])
        wall = next(e for e in near if e["name"] == "stone-wall" and e["owned"])
        refs = [drill["target_ref"], inserter["target_ref"]]
        reads = [bridge.call("get_entity_inventory", {"target_ref": ref})["result"] for ref in refs]
        for entity, result in zip((drill, inserter), reads, strict=True):
            record(
                "M26 inventory-less " + entity["name"] + " returns status and positions",
                not result["inventories"]
                and isinstance(result.get("status"), str)
                and result.get("direction") == entity["direction"]
                and result.get("drop_position") == entity["drop_position"]
                and (
                    result.get("pickup_position") == entity["pickup_position"]
                    if entity["type"] == "inserter"
                    else result.get("mining_target") == "iron-ore"
                ),
                json.dumps(result),
            )
        record("M26 empty inserter hand omits held_stack", "held_stack" not in reads[1])
        status_fixture(
            "local i=storage.m26_status_backup.inserter;assert(i and i.valid);"
            'assert(i.held_stack.set_stack{name="iron-plate",quality="normal",count=1});'
            "rcon.print('m26_status:{\"holding\":true}')"
        )
        holding = bridge.call("get_entity_inventory", {"target_ref": refs[1]})["result"]
        record(
            "M26 held_stack reports name quality and count",
            holding.get("held_stack") == {"name": "iron-plate", "quality": "normal", "count": 1},
            json.dumps(holding),
        )
        batch = bridge.call("get_entity_inventories", {"target_refs": refs})["result"]
        record(
            "M26 status-only inventory batch succeeds for both machines",
            batch["succeeded"] == 2
            and batch["failed"] == 0
            and all(e["ok"] and not e["result"]["inventories"] for e in batch["results"])
            and batch["results"][0]["result"].get("mining_target") == "iron-ore"
            and batch["results"][1]["result"] == holding,
            json.dumps(batch),
        )
        burners = bridge.call(
            "observe_nearby", {"names": ["burner-inserter", "burner-mining-drill"]}
        )["result"]["entities"]
        for entity in burners:
            result = bridge.call("get_entity_inventory", {"target_ref": entity["target_ref"]})[
                "result"
            ]
            record(
                "M26 " + entity["name"] + " keeps fuel inventory and adds positions",
                "fuel" in result["inventories"]
                and "status" in result
                and result.get("drop_position") == entity["drop_position"]
                and (
                    result.get("pickup_position") == entity["pickup_position"]
                    and "held_stack" not in result
                    if entity["type"] == "inserter"
                    else "mining_target" not in result
                ),
                json.dumps(result),
            )
        for entity in (drill, inserter):
            label = "M26 transfer still rejects " + entity["name"]
            expect_error(
                label,
                "transfer_item",
                {
                    "target_ref": entity["target_ref"],
                    "direction": "to_entity",
                    "inventory_role": "fuel",
                    "item": "coal",
                    "count": 1,
                },
                {"invalid_target"},
            )
            record(
                label + " as unsupported_entity",
                errors[label].detail["reason"] == "unsupported_entity",
            )
        for label, entity, code in (
            ("M26 reachable stone-wall remains unsupported", wall, "invalid_target"),
            ("M26 distant drill remains out of range", far_drill, "out_of_range"),
            ("M26 enemy inserter remains forbidden", enemy, "invalid_target"),
        ):
            expect_error(
                label, "get_entity_inventory", {"target_ref": entity["target_ref"]}, {code}
            )
        record(
            "M26 stone-wall rejection retains unsupported_entity reason",
            errors["M26 reachable stone-wall remains unsupported"].detail.get("reason")
            == "unsupported_entity",
        )
        record(
            "M26 enemy inserter rejection retains other_force reason",
            errors["M26 enemy inserter remains forbidden"].detail.get("reason") == "other_force",
        )

        whitelist = {
            "target_ref",
            "name",
            "x",
            "y",
            "direction",
            "status",
            "recipe",
            "belt_items",
            "underground_type",
            "neighbour",
            "pickup_position",
            "drop_position",
            "electric_network_id",
            "no_drop_target",
            "missing_ingredients",
            "missing_fluids",
            "input_priority",
            "output_priority",
            "filter",
            "inventory_limit",
            "mining_target",
        }
        page = 0
        total = 0
        while True:
            params = {"owned_only": True, "entity_offset": page}
            full = bridge.call("observe_nearby", params)["result"]
            compact = bridge.call("observe_nearby", {**params, "compact": True})["result"]
            explicit_false = bridge.call("observe_nearby", {**params, "compact": False})["result"]
            record(
                f"M26 compact page {page} preserves references and only whitelisted fields",
                full == explicit_false
                and "compact" not in full["filters"]
                and compact["filters"] == {**full["filters"], "compact": True}
                and compact["entity_total"] == full["entity_total"]
                and compact.get("next_entity_offset") == full.get("next_entity_offset")
                and compact["entities"]
                == [
                    {k: v for k, v in entity.items() if k in whitelist}
                    for entity in full["entities"]
                ]
                and compact["resources"] == full["resources"]
                and compact["blocked_tiles"] == full["blocked_tiles"]
                and compact["blocked_tile_total"] == full["blocked_tile_total"]
                and compact.get("next_tile_offset") == full.get("next_tile_offset")
                and len(json.dumps(compact)) < len(json.dumps(full)),
                f"full={len(json.dumps(full))}, compact={len(json.dumps(compact))}",
            )
            total += len(compact["entities"])
            if "next_entity_offset" not in full:
                break
            page = full["next_entity_offset"]
        record(
            "M26 compact exercises multiple pages without losing entities",
            page > 0 and total == full["entity_total"],
        )
        compact_refs = bridge.call(
            "observe_nearby", {"owned_only": True, "names": ["inserter"], "compact": True}
        )["result"]["entities"]
        record(
            "M26 compact target_ref remains usable for inventory reads",
            bridge.call("get_entity_inventory", {"target_ref": compact_refs[0]["target_ref"]})[
                "result"
            ]
            == holding,
        )
        for compact in (0, 1, "true", [], {}):
            expect_error(
                "M26 non-boolean compact rejected " + repr(compact),
                "observe_nearby",
                {"compact": compact},
                {"invalid_input"},
            )
    finally:
        status_fixture(
            "local b=storage.m26_status_backup;if b then "
            "assert(not state.action and not state.craft_action);"
            "assert(c.teleport(b.position,b.surface),'M26 status position restore failed');"
            # Only slots stashed above are swapped back, even if setup failed partway.
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            "b.inventory.destroy();storage.m26_status_backup=nil;"
            "if s then assert(game.delete_surface(s)) end;state.explored_map[name]=nil end;"
            "rcon.print('m26_status:{\"restored\":true}')"
        )
        observed_map_cells.pop(status_surface, None)
        record(
            "M26 status fixture restores exact inventory and position",
            bridge.call("get_inventory")["result"]["items"] == status_inventory
            and bridge.call("get_agent_status")["result"]["position"] == status_position,
        )

    # M27-BM/BN: feeder diagnostics and current position on reach failures.
    diagnostics_surface = "m27-feeder-diagnostics-check"
    diagnostics_inventory = bridge.call("get_inventory")["result"]["items"]
    diagnostics_position = bridge.call("get_agent_status")["result"]["position"]

    def diagnostics_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;"
            "local c=state and state.character;assert(c and c.valid);"
            f'local name="{diagnostics_surface}";local s=game.surfaces[name];'
            "local inv=c.get_main_inventory();assert(inv);" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
        if "m27_diagnostics:" not in raw:
            raise RuntimeError("M27 diagnostics fixture failed: " + raw)
        return json.loads(raw.split("m27_diagnostics:", 1)[1].strip())

    diagnostics_server = create_server(bridge)

    async def diagnostics_tool(operation, params):
        response = await diagnostics_server.call_tool(operation, params)
        return response.is_error, json.loads(response.content[0].text)

    def wait_for_chest(target_ref):
        is_error, result = asyncio.run(
            diagnostics_tool(
                "wait_for_items",
                {
                    "target_ref": target_ref,
                    "item": "iron-plate",
                    "count": 1,
                    "timeout_seconds": 1,
                },
            )
        )
        assert not is_error, result
        return result

    try:
        diagnostics_fixture(
            "assert(not state.action and not state.craft_action);"
            "assert(not s and not storage.m27_diagnostics_backup,'M27 fixture already exists');"
            "local b={position={x=c.position.x,y=c.position.y},surface=c.surface,"
            "inventory=game.create_inventory(#inv)};storage.m27_diagnostics_backup=b;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(b.inventory[i].swap_stack(inv[i])) end end;b.stashed=true;"
            "s=game.create_surface(name,{width=64,height=64});s.peaceful_mode=true;"
            "s.request_to_generate_chunks({0,0},1);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-32,31 do for y=-32,31 do "
            'tiles[#tiles+1]={name="grass-1",position={x,y}} end end;s.set_tiles(tiles);'
            "assert(c.teleport({0.5,4.5},s));assert(c.reach_distance<13);"
            'b.feeder=s.create_entity{name="inserter",position={0.5,0.5},'
            "direction=0,force=c.force};assert(b.feeder);b.feeder.energy=0;"
            # Derive chest tiles from actual pickup/drop positions, including the
            # inserter prototype's fractional drop offset, rather than guessing.
            "local p=b.feeder.pickup_position;local d=b.feeder.drop_position;"
            'b.source=s.create_entity{name="iron-chest",'
            "position={math.floor(p.x)+0.5,math.floor(p.y)+0.5},force=c.force};assert(b.source);"
            'b.target=s.create_entity{name="wooden-chest",'
            "position={math.floor(d.x)+0.5,math.floor(d.y)+0.5},force=c.force};assert(b.target);"
            'b.other=s.create_entity{name="inserter",position={2.5,0.5},'
            "direction=0,force=c.force};assert(b.other);b.other.energy=0;"
            "local o=b.other.drop_position;"
            'b.other_target=s.create_entity{name="wooden-chest",'
            "position={math.floor(o.x)+0.5,math.floor(o.y)+0.5},force=c.force};"
            "assert(b.other_target);"
            'assert(s.create_entity{name="wooden-chest",position={-3.5,1.5},force=c.force});'
            'assert(s.create_entity{name="wooden-chest",position={13.5,4.5},force=c.force});'
            "rcon.print('m27_diagnostics:{\"ready\":true}')"
        )
        # Let Factorio assign the inserters' targets on simulation ticks. A fixture
        # failure must be explicit rather than silently testing an empty feeder list.
        deadline = time.monotonic() + 5
        while True:
            ready = diagnostics_fixture(
                "local b=storage.m27_diagnostics_backup;"
                "local d=b.feeder.drop_target;local p=b.feeder.pickup_target;"
                "local o=b.other.drop_target;"
                "rcon.print('m27_diagnostics:'..helpers.table_to_json({"
                "ready=d and d.valid and d.unit_number==b.target.unit_number and "
                "p and p.valid and p.unit_number==b.source.unit_number and "
                "o and o.valid and o.unit_number==b.other_target.unit_number or false}))"
            )
            if ready["ready"]:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("M27 inserters did not acquire the fixture chest targets")
            time.sleep(0.1)

        entities = bridge.call("observe_nearby")["result"]["entities"]
        target = next(e for e in entities if e["name"] == "wooden-chest" and e["x"] == 0.5)
        empty = next(e for e in entities if e["name"] == "wooden-chest" and e["x"] == -3.5)
        distant = next(e for e in entities if e["name"] == "wooden-chest" and e["x"] == 13.5)
        feeder = next(e for e in entities if e["name"] == "inserter" and e["x"] == 0.5)
        feeder_status = bridge.call("get_entity_inventory", {"target_ref": feeder["target_ref"]})[
            "result"
        ]["status"]
        timed_out = wait_for_chest(target["target_ref"])
        record(
            "M27 timeout reports exactly the unpowered feeder and its pickup chest",
            timed_out["reached"] is False
            and timed_out["timed_out"] is True
            and timed_out["stalled"] is False
            and feeder_status == "no_power"
            and timed_out.get("feeders")
            == [
                {
                    "name": "inserter",
                    "x": feeder["x"],
                    "y": feeder["y"],
                    "status": feeder_status,
                    "pickup_target": "iron-chest",
                }
            ],
            json.dumps(timed_out),
        )
        record(
            "M27 nearby inserter dropping into a different chest is excluded",
            isinstance(timed_out.get("feeders"), list)
            and all(f["x"] != 2.5 for f in timed_out["feeders"])
            and len(timed_out["feeders"]) == 1,
        )
        no_feeders = wait_for_chest(empty["target_ref"])
        record(
            "M27 chest with no inserters returns an empty feeder list",
            no_feeders["timed_out"] is True and no_feeders.get("feeders") == [],
            json.dumps(no_feeders),
        )
        diagnostics_fixture(
            "local b=storage.m27_diagnostics_backup;assert(b.source.destroy());"
            "rcon.print('m27_diagnostics:{\"source_removed\":true}')"
        )
        no_pickup = wait_for_chest(target["target_ref"])
        record(
            "M27 feeder without a pickup entity omits pickup_target",
            no_pickup.get("feeders")
            == [{"name": "inserter", "x": feeder["x"], "y": feeder["y"], "status": feeder_status}],
            json.dumps(no_pickup),
        )
        diagnostics_fixture(
            'assert(storage.m27_diagnostics_backup.target.insert{name="iron-plate",count=1}==1);'
            "rcon.print('m27_diagnostics:{\"stocked\":true}')"
        )
        reached = wait_for_chest(target["target_ref"])
        record(
            "M27 reached wait omits feeders",
            reached["reached"] is True and "feeders" not in reached,
            json.dumps(reached),
        )
        actual_position = bridge.call("get_agent_status")["result"]["position"]
        is_error, out_of_range = asyncio.run(
            diagnostics_tool("get_entity_inventory", {"target_ref": distant["target_ref"]})
        )
        reported_position = out_of_range.get("detail", {}).get("character_position", {})
        record(
            "M27 out_of_range includes the character's actual position",
            is_error
            and out_of_range.get("error") == "out_of_range"
            and all(
                isinstance(reported_position.get(axis), (int, float))
                and math.isclose(reported_position[axis], actual_position[axis], abs_tol=0.01)
                for axis in ("x", "y")
            ),
            json.dumps(out_of_range),
        )
    finally:
        diagnostics_fixture(
            "local b=storage.m27_diagnostics_backup;if b then "
            "assert(not state.action and not state.craft_action);"
            "assert(c.teleport(b.position,b.surface),'M27 position restore failed');"
            "if b.stashed then inv.clear() end;"
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            "b.inventory.destroy();storage.m27_diagnostics_backup=nil;"
            "if s then assert(game.delete_surface(s)) end;state.explored_map[name]=nil end;"
            "rcon.print('m27_diagnostics:{\"restored\":true}')"
        )
        observed_map_cells.pop(diagnostics_surface, None)
        record(
            "M27 diagnostics fixture restores exact inventory and position",
            bridge.call("get_inventory")["result"]["items"] == diagnostics_inventory
            and bridge.call("get_agent_status")["result"]["position"] == diagnostics_position,
        )

    # M28: use an isolated surface and stash whole inventory slots to preserve
    # qualities, durability and the original character position exactly.
    m28_surface = "m28_waits_check_line"
    m28_inventory = bridge.call("get_inventory")["result"]["items"]
    m28_position = bridge.call("get_agent_status")["result"]["position"]
    m28_server = create_server(bridge)

    def m28_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;"
            "local c=state and state.character;assert(c and c.valid);"
            f'local name="{m28_surface}";local s=game.surfaces[name];'
            "local inv=c.get_main_inventory();assert(inv);" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
        if "m28:" not in raw:
            raise RuntimeError("M28 fixture failed: " + raw)
        return json.loads(raw.split("m28:", 1)[1].strip())

    async def m28_tool(operation, params):
        # Invalid argument exceptions carry the same JSON error as tool results.
        from mcp.server.mcpserver.exceptions import ToolError

        try:
            response = await m28_server.call_tool(operation, params)
        except ToolError as exc:
            return True, json.loads(str(exc))
        return response.is_error, json.loads(response.content[0].text)

    def m28_check(x1, y1, x2, y2, item="transport-belt"):
        is_error, result = asyncio.run(
            m28_tool(
                "check_line",
                {
                    "item": item,
                    "surface": m28_surface,
                    "from_x": x1,
                    "from_y": y1,
                    "to_x": x2,
                    "to_y": y2,
                },
            )
        )
        assert not is_error, result
        return result

    try:
        m28_fixture(
            "assert(not state.action and not state.craft_action);"
            "assert(not s and not storage.m28_backup,'M28 fixture already exists');"
            "local b={position={x=c.position.x,y=c.position.y},surface=c.surface,"
            "inventory=game.create_inventory(#inv),last_observation=state.last_observation,"
            "recent_observations=state.recent_observations};storage.m28_backup=b;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(b.inventory[i].swap_stack(inv[i])) end end;b.stashed=true;"
            "s=game.create_surface(name,{width=128,height=128});s.peaceful_mode=true;"
            "s.request_to_generate_chunks({0,0},2);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-64,63 do for y=-64,63 do "
            'tiles[#tiles+1]={name="grass-1",position={x,y}} end end;s.set_tiles(tiles);'
            "assert(c.teleport({0.5,4.5},s));assert(c.build_distance<13);"
            "state.last_observation=nil;state.recent_observations={};"
            'b.belt=s.create_entity{name="transport-belt",position={8.5,0.5},'
            'direction=4,force="neutral"};assert(b.belt);b.belt.active=false;'
            'b.opposite=s.create_entity{name="transport-belt",position={3.5,2.5},'
            "direction=12,force=c.force};assert(b.opposite);b.opposite.active=false;"
            'b.secret=s.create_entity{name="wooden-chest",position={25.5,-4.5},'
            "force=c.force};assert(b.secret);"
            's.set_tiles{{name="water",position={26,-5}}};'
            'assert(s.create_entity{name="item-on-ground",position={4.5,0.5},'
            'stack={name="wood",count=2}});'
            # Trees use mining reach through can_reach_entity, not ordinary reach.
            # Keep it two tiles away and west of every check_line row and move path.
            'b.tree=s.create_entity{name="tree-01",position={-1.5,4.5}};assert(b.tree);'
            "assert(c.can_reach_entity(b.tree),'M28 fixture tree outside mining reach');"
            "rcon.print('m28:{\"ready\":true}')"
        )
        observed = bridge.call("observe_nearby")["result"]
        baseline = m28_fixture(
            "rcon.print('m28:'..helpers.table_to_json({count=#s.find_entities(),"
            "inventory=inv.get_contents()}))"
        )
        clear = m28_check(1.5, 0.5, 5.5, 0.5)
        after_check = m28_fixture(
            "rcon.print('m28:'..helpers.table_to_json({count=#s.find_entities(),"
            "inventory=inv.get_contents()}))"
        )
        record(
            "M28 observed clear line ignores ground items and places/consumes nothing",
            clear.get("complete") is True
            and clear.get("clear") == clear.get("requested") == 5
            and clear.get("already_present") == 0
            and clear.get("problems") == []
            and clear.get("problem_total") == 0
            and baseline == after_check
            and bridge.call("get_inventory")["result"]["items"] in ([], {}),
            json.dumps(clear),
        )
        m28_fixture(
            'local b=storage.m28_backup;b.obstacle=s.create_entity{name="wooden-chest",'
            "position={3.5,0.5},force=c.force};assert(b.obstacle);"
            "rcon.print('m28:{\"obstacle\":true}')"
        )
        blocked = m28_check(1.5, 0.5, 5.5, 0.5)
        problem = next(iter(blocked.get("problems", [])), {})
        record(
            "M28 fixture chest is a named blocker at the correct tile",
            blocked.get("complete") is False
            and blocked.get("problem_total") == 1
            and (problem.get("x"), problem.get("y"), problem.get("reason")) == (3.5, 0.5, "blocked")
            and any(
                e.get("name") == "wooden-chest" and e.get("target_ref")
                for e in problem.get("blocking", [])
            ),
            json.dumps(blocked),
        )
        distant = m28_check(13.5, 4.5, 14.5, 4.5)
        record(
            "M28 observed tiles beyond build reach are clear",
            distant.get("complete") is True and distant.get("clear") == 2,
            json.dumps(distant),
        )
        hidden = m28_check(12.5, -4.5, 44.5, -4.5)
        expected_hidden = [
            {"x": x + 0.5, "y": -4.5, "reason": "not_visible"}
            for x in range(12, 45)
            if math.hypot(x + 0.5 - observed["position"]["x"], -4.5 - observed["position"]["y"])
            > observed["radius"]
        ]
        record(
            "M28 unobserved tiles hide fixture chest/water and cap problems at 16",
            hidden.get("complete") is False
            and hidden.get("problem_total") == len(expected_hidden)
            and hidden.get("problems") == expected_hidden[:16],
            json.dumps(hidden),
        )
        opposite = m28_check(1.5, 2.5, 5.5, 2.5)
        record(
            "M28 opposite belt reports different_direction",
            opposite.get("problems")
            == [{"x": 3.5, "y": 2.5, "reason": "different_direction", "existing_direction": 12}],
            json.dumps(opposite),
        )
        same = m28_check(5.5, 2.5, 1.5, 2.5)
        record(
            "M28 matching belt counts as already_present",
            same.get("complete") is True
            and same.get("already_present") == 1
            and same.get("clear") == 4,
            json.dumps(same),
        )
        tree = next(e for e in observed["entities"] if e["name"] == "tree-01")
        started = bridge.call("mine_entity", {"target_ref": tree["target_ref"], "count": 100})[
            "result"
        ]
        while_mining = m28_check(13.5, 4.5, 14.5, 4.5)
        mining_status = bridge.call("get_action_status", {"action_id": started["action_id"]})[
            "result"
        ]
        if mining_status["status"] == "running":
            bridge.call("cancel_action", {"action_id": started["action_id"]})
        record("M28 check_line works while mining", while_mining.get("complete") is True)

        # An inactive belt makes the final-position check deterministic. Belt drift
        # itself is covered with fake-clock unit tests, not RCON timing assumptions.
        action("move_to", {"surface": m28_surface, "x": 8.5, "y": 4.5})
        # Aim slightly beyond the belt centre along the approach, so the normal
        # 0.5-tile arrival tolerance leaves the character inside its collision box.
        is_error, belt_move = asyncio.run(
            m28_tool("move_to", {"surface": m28_surface, "x": 8.5, "y": 0.3, "wait_seconds": 20})
        )
        record(
            "M28/M37 move_to adjusts a neutral belt goal and finishes off belts",
            not is_error
            and belt_move.get("status") == "succeeded"
            and belt_move.get("goal_adjusted")
            and not belt_move.get("standing_on_belt"),
            json.dumps(belt_move),
        )
        started = bridge.call("move_to", {"surface": m28_surface, "x": 0.5, "y": 4.5})["result"]
        while_walking = m28_check(13.5, 4.5, 14.5, 4.5)
        is_error, ground_move = asyncio.run(
            m28_tool("wait_for_action", {"action_id": started["action_id"], "timeout_seconds": 20})
        )
        record("M28 check_line works while walking", while_walking.get("complete") is True)
        record(
            "M28 wait_for_action move onto ground omits belt warning",
            not is_error
            and ground_move.get("status") == "succeeded"
            and "standing_on_belt" not in ground_move,
            json.dumps(ground_move),
        )
        # Already-within success follows the synchronous move completion path.
        m28_fixture("assert(c.teleport({8.5,0.5},s));rcon.print('m28:{\"on_belt\":true}')")
        is_error, immediate = asyncio.run(
            m28_tool("move_to", {"surface": m28_surface, "x": 8.5, "y": 0.5, "within": 0.5})
        )
        record(
            "M28 immediate move success also warns on belts",
            not is_error
            and immediate.get("status") == "succeeded"
            and immediate.get("standing_on_belt") is True,
            json.dumps(immediate),
        )
        for threshold in (4, 51):
            is_error, invalid = asyncio.run(
                m28_tool(
                    "wait_for_research", {"technology": "automation", "stall_seconds": threshold}
                )
            )
            record(
                f"M28 wait_for_research rejects stall_seconds={threshold}",
                is_error and invalid.get("error") == "invalid_input",
                json.dumps(invalid),
            )
    finally:
        running = bridge.call("get_agent_status")["result"].get("action")
        if running:
            bridge.call("cancel_action", {"action_id": running.get("action_id") or running["id"]})
        m28_fixture(
            "local b=storage.m28_backup;if b then "
            "assert(not state.action and not state.craft_action);"
            "assert(c.teleport(b.position,b.surface),'M28 position restore failed');"
            # Discard fixture items, including wood mined before cancellation.
            "if b.stashed then inv.clear() end;"
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            "b.inventory.destroy();state.last_observation=b.last_observation;"
            "state.recent_observations=b.recent_observations;storage.m28_backup=nil;"
            "if s then assert(game.delete_surface(s)) end;state.explored_map[name]=nil end;"
            "rcon.print('m28:{\"restored\":true}')"
        )
        observed_map_cells.pop(m28_surface, None)
        record(
            "M28 fixture restores exact inventory and position",
            bridge.call("get_inventory")["result"]["items"] == m28_inventory
            and bridge.call("get_agent_status")["result"]["position"] == m28_position,
        )

    # M29: fixtures live on a disposable surface; no existing inserter is touched.
    m29_surface = "m29-filter-fixture"
    m29_server = create_server(bridge)
    m29_inventory = bridge.call("get_inventory")["result"]["items"]
    m29_position = bridge.call("get_agent_status")["result"]["position"]

    def m29_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;"
            "local c=state and state.character;assert(c and c.valid);"
            f'local name="{m29_surface}";local s=game.surfaces[name];'
            "local inv=c.get_main_inventory();assert(inv);local b=storage.m29_backup;" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
        if "m29:" not in raw:
            raise RuntimeError("M29 fixture failed: " + raw)
        return json.loads(raw.split("m29:", 1)[1].strip())

    async def m29_tool(operation, params):
        from mcp.server.mcpserver.exceptions import ToolError

        try:
            response = await m29_server.call_tool(operation, params)
        except ToolError as exc:
            return True, json.loads(str(exc))
        return response.is_error, json.loads(response.content[0].text)

    def m29_call(operation, **params):
        is_error, value = asyncio.run(m29_tool(operation, params))
        assert not is_error, value
        return value

    try:
        setup = m29_fixture(
            "assert(not state.action and not state.craft_action);"
            "assert(not s and not b,'M29 fixture already exists');"
            "b={position={x=c.position.x,y=c.position.y},surface=c.surface,"
            "inventory=game.create_inventory(#inv),last_observation=state.last_observation,"
            "recent_observations=state.recent_observations};storage.m29_backup=b;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(b.inventory[i].swap_stack(inv[i])) end end;b.stashed=true;"
            "s=game.create_surface(name,{width=128,height=128});s.peaceful_mode=true;"
            "s.request_to_generate_chunks({0,0},2);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-64,63 do for y=-64,63 do "
            'tiles[#tiles+1]={name="grass-1",position={x,y}} end end;s.set_tiles(tiles);'
            "assert(c.teleport({0.5,4.5},s));assert(c.reach_distance<12);"
            "state.last_observation=nil;state.recent_observations={};"
            'b.inserter=s.create_entity{name="inserter",position={1.5,3.5},force=c.force};'
            'b.burner=s.create_entity{name="burner-inserter",position={-2.5,3.5},'
            "direction=8,force=c.force};"
            'b.splitter=s.create_entity{name="splitter",position={3,1.5},force=c.force};'
            'b.far=s.create_entity{name="inserter",position={12.5,4.5},force=c.force};'
            'b.enemy=s.create_entity{name="inserter",position={-4.5,3.5},force="enemy"};'
            "assert(b.inserter and b.burner and b.splitter and b.far and b.enemy);"
            "b.inserter.active=false;b.burner.active=false;b.far.active=false;b.enemy.active=false;"
            'b.source=s.create_entity{name="wooden-chest",position=b.burner.pickup_position,'
            "force=c.force};"
            'b.dest=s.create_entity{name="wooden-chest",position=b.burner.drop_position,'
            "force=c.force};assert(b.source and b.dest);"
            'assert(b.source.insert{name="iron-plate",count=4}==4);'
            'assert(b.source.insert{name="copper-plate",count=4}==4);'
            "assert(c.can_reach_entity(b.inserter) and c.can_reach_entity(b.burner) and "
            "c.can_reach_entity(b.splitter));"
            "rcon.print('m29:'..helpers.table_to_json({burner_slots=b.burner.filter_slot_count}))"
        )
        observed = m29_call("observe_nearby")
        refs = {
            "inserter": next(
                e["target_ref"]
                for e in observed["entities"]
                if e["name"] == "inserter" and e["x"] == 1.5
            ),
            "burner": next(
                e["target_ref"] for e in observed["entities"] if e["name"] == "burner-inserter"
            ),
            "splitter": next(
                e["target_ref"] for e in observed["entities"] if e["name"] == "splitter"
            ),
            "chest": next(
                e["target_ref"] for e in observed["entities"] if e["name"] == "wooden-chest"
            ),
            "far": next(
                e["target_ref"]
                for e in observed["entities"]
                if e["name"] == "inserter" and e["x"] == 12.5
            ),
            "enemy": next(
                e["target_ref"]
                for e in observed["entities"]
                if e["name"] == "inserter" and e["x"] == -4.5
            ),
        }

        def m29_contents():
            return m29_fixture(
                "local function held(e) local h=e.held_stack;return h and h.valid_for_read and "
                "{name=h.name,count=h.count,quality=h.quality.name} or nil end;"
                "rcon.print('m29:'..helpers.table_to_json({inventory=inv.get_contents(),"
                "source=b.source.get_inventory(defines.inventory.chest).get_contents(),"
                "dest=b.dest.get_inventory(defines.inventory.chest).get_contents(),"
                "fuel=b.burner.get_fuel_inventory().get_contents(),"
                "inserter=held(b.inserter),burner=held(b.burner)}))"
            )

        initial_contents = m29_contents()
        desired = ["iron-plate", "copper-plate"]
        configured = m29_call("set_inserter_filter", target_ref=refs["inserter"], items=desired)
        read = m29_call("get_entity_inventory", target_ref=refs["inserter"])
        batch = m29_call("get_entity_inventories", target_refs=[refs["inserter"]])
        record(
            "M29 whitelist result, single and batch inventory read-back",
            configured.get("name") == "inserter"
            and configured.get("use_filters") is True
            and configured.get("mode") == "whitelist"
            and configured.get("filters") == desired
            and configured.get("position", {}).get("surface") == m29_surface
            and read.get("filters") == desired
            and read.get("filter_mode") == "whitelist"
            and batch["results"][0]["result"].get("filters") == desired,
            json.dumps(configured),
        )
        configured = m29_call(
            "set_inserter_filter", target_ref=refs["inserter"], items=list(reversed(desired))
        )
        record(
            "M29 existing filter names can be reordered",
            configured.get("filters") == list(reversed(desired)),
        )
        configured = m29_call("set_inserter_filter", target_ref=refs["inserter"], items=[])
        read = m29_call("get_entity_inventory", target_ref=refs["inserter"])
        record(
            "M29 empty list clears slots and disables filtering",
            configured.get("use_filters") is False
            and configured.get("filters") == []
            and configured.get("mode") == "whitelist"
            and "filters" not in read
            and "filter_mode" not in read,
            json.dumps(configured),
        )
        configured = m29_call(
            "set_inserter_filter", target_ref=refs["inserter"], items=["coal"], mode="blacklist"
        )
        read = m29_call("get_entity_inventory", target_ref=refs["inserter"])
        record(
            "M29 blacklist mode round trip",
            configured.get("mode") == "blacklist"
            and read.get("filter_mode") == "blacklist"
            and read.get("filters") == ["coal"],
        )
        expect_error(
            "M29 unknown inserter items are rejected",
            "set_inserter_filter",
            {"target_ref": refs["inserter"], "items": ["m29-unknown-a", "m29-unknown-b"]},
            {"invalid_input"},
        )
        record(
            "M29 unknown item detail lists every name",
            errors["M29 unknown inserter items are rejected"].detail.get("unknown_items")
            == ["m29-unknown-a", "m29-unknown-b"],
        )
        for label, invalid_items in (
            ("six names", ["coal", "iron-plate", "copper-plate", "stone", "wood", "iron-ore"]),
            ("duplicates", ["coal", "coal"]),
        ):
            params = {"target_ref": refs["inserter"], "items": invalid_items}
            # Exercise both Mod-side validation and server-side validation.
            expect_error(
                "M29 Mod rejects " + label, "set_inserter_filter", params, {"invalid_input"}
            )
            is_error, value = asyncio.run(m29_tool("set_inserter_filter", params))
            record(
                "M29 server rejects " + label, is_error and value.get("error") == "invalid_input"
            )
        read = m29_call("get_entity_inventory", target_ref=refs["inserter"])
        record(
            "M29 rejected filters leave previous configuration unchanged",
            read.get("filter_mode") == "blacklist" and read.get("filters") == ["coal"],
        )
        for label, key, code in (
            ("chest", "chest", "invalid_target"),
            ("distant inserter", "far", "out_of_range"),
            ("other force", "enemy", "invalid_target"),
        ):
            expect_error(
                "M29 rejects " + label,
                "set_inserter_filter",
                {"target_ref": refs[key], "items": ["coal"]},
                {code},
            )
        record(
            "M29 reach rejection includes distance and reach",
            all(
                key in errors["M29 rejects distant inserter"].detail
                for key in ("distance", "reach")
            ),
        )

        if setup["burner_slots"] > 0:
            configured = m29_call(
                "set_inserter_filter", target_ref=refs["burner"], items=["iron-plate"]
            )
            read = m29_call("get_entity_inventory", target_ref=refs["burner"])
            record(
                "M29 burner-inserter accepts filters (expected 5 slots)",
                setup["burner_slots"] == 5
                and configured.get("filters") == ["iron-plate"]
                and read.get("filters") == ["iron-plate"],
                json.dumps(setup),
            )
        else:
            expect_error(
                "M29 burner has no slots; settings unsupported in this base data",
                "set_inserter_filter",
                {"target_ref": refs["burner"], "items": ["iron-plate"]},
                {"invalid_target"},
            )

        default_splitter = next(e for e in observed["entities"] if e["name"] == "splitter")
        record(
            "M29 default splitter observation omits settings",
            all(
                key not in default_splitter
                for key in ("input_priority", "output_priority", "filter")
            ),
        )
        configured = m29_call(
            "set_splitter",
            target_ref=refs["splitter"],
            input_priority="right",
            output_priority="right",
            filter="iron-plate",
        )
        record(
            "M29 splitter result reads actual priorities and filter",
            configured.get("name") == "splitter"
            and configured.get("input_priority") == "right"
            and configured.get("output_priority") == "right"
            and configured.get("filter") == "iron-plate"
            and "hint" not in configured,
        )
        for compact in (False, True):
            observed_splitter = next(
                e
                for e in m29_call("observe_nearby", compact=compact)["entities"]
                if e["name"] == "splitter"
            )
            record(
                f"M29 splitter observation compact={compact}",
                observed_splitter.get("input_priority") == "right"
                and observed_splitter.get("output_priority") == "right"
                and observed_splitter.get("filter") == "iron-plate",
            )
        configured = m29_call("set_splitter", target_ref=refs["splitter"], output_priority="none")
        record(
            "M29 retained filter with output none becomes left with hint",
            configured.get("output_priority") == "left"
            and bool(configured.get("hint"))
            and configured.get("filter") == "iron-plate"
            and configured.get("input_priority") == "right",
        )
        configured = m29_call(
            "set_splitter",
            target_ref=refs["splitter"],
            filter="",
            output_priority="none",
            input_priority="none",
        )
        record(
            "M29 empty string clears splitter filter",
            "filter" not in configured and configured.get("output_priority") == "none",
        )
        configured = m29_call("set_splitter", target_ref=refs["splitter"], filter="coal")
        record(
            "M29 new filter with default output becomes left with hint",
            configured.get("filter") == "coal"
            and configured.get("output_priority") == "left"
            and bool(configured.get("hint")),
        )
        expect_error(
            "M29 unknown splitter filter is rejected",
            "set_splitter",
            {"target_ref": refs["splitter"], "filter": "m29-unknown", "input_priority": "right"},
            {"invalid_input"},
        )
        configured = m29_call("set_splitter", target_ref=refs["splitter"], output_priority="left")
        record(
            "M29 unknown splitter filter changes nothing",
            configured.get("filter") == "coal"
            and configured.get("output_priority") == "left"
            and configured.get("input_priority") == "none",
        )
        configured = m29_call(
            "set_splitter",
            target_ref=refs["splitter"],
            filter="",
            input_priority="none",
            output_priority="none",
        )
        for compact in (False, True):
            cleared = next(
                e
                for e in m29_call("observe_nearby", compact=compact)["entities"]
                if e["name"] == "splitter"
            )
            record(
                f"M29 cleared splitter omits default fields compact={compact}",
                all(key not in cleared for key in ("input_priority", "output_priority", "filter")),
            )
        expect_error(
            "M29 non-splitter is rejected",
            "set_splitter",
            {"target_ref": refs["chest"], "output_priority": "right"},
            {"invalid_target"},
        )
        is_error, value = asyncio.run(m29_tool("set_splitter", {"target_ref": refs["splitter"]}))
        record(
            "M29 splitter requires a setting", is_error and value.get("error") == "invalid_input"
        )
        record(
            "M29 settings preserve character, chest, fuel and held-stack contents",
            initial_contents == m29_contents(),
        )

        if setup["burner_slots"] > 0:
            # No electrical network or production timing dependency: transfer only four plates.
            m29_fixture(
                'assert(b.burner.get_fuel_inventory().insert{name="coal",count=5}==5);'
                "b.burner.active=true;rcon.print('m29:{\"running\":true}')"
            )
            deadline = time.monotonic() + 12
            settled_at = None
            while True:
                arrived = m29_fixture(
                    'rcon.print("m29:"..helpers.table_to_json({'
                    'iron=b.dest.get_item_count("iron-plate"),'
                    'copper=b.dest.get_item_count("copper-plate"),'
                    'source_copper=b.source.get_item_count("copper-plate")}))'
                )
                now = time.monotonic()
                if arrived["iron"] == 4 and settled_at is None:
                    # Allow another stroke so a broken filter cannot hide a later contaminant.
                    settled_at = now + 2
                if (settled_at is not None and now >= settled_at) or now >= deadline:
                    break
                time.sleep(0.2)
            record(
                "M29 fueled burner whitelist transfers only the selected item",
                arrived == {"iron": 4, "copper": 0, "source_copper": 4},
                json.dumps(arrived),
            )
    finally:
        restored = m29_fixture(
            "if b then assert(not state.action and not state.craft_action);"
            "assert(c.teleport(b.position,b.surface),'M29 position restore failed');"
            "if b.stashed then inv.clear() end;"
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            # swap_stack restores each original slot and all its item metadata.
            "for i=1,#b.inventory do assert(not b.inventory[i].valid_for_read) end;"
            "b.inventory.destroy();state.last_observation=b.last_observation;"
            "state.recent_observations=b.recent_observations;storage.m29_backup=nil;"
            "for ref,entry in pairs(state.targets) do if entry.entity and entry.entity.valid and "
            "entry.entity.surface==s then if entry.unit_number then "
            "state.owned_refs[entry.unit_number]=nil end;state.targets[ref]=nil end end;"
            "if s then assert(game.delete_surface(s)) end;state.explored_map[name]=nil;"
            "assert(c.position.x==b.position.x and c.position.y==b.position.y "
            "and c.surface==b.surface) "
            "end;rcon.print('m29:{\"restored\":true}')"
        )
        observed_map_cells.pop(m29_surface, None)
        record(
            "M29 fixture restores exact inventory slots and position; removes all placed entities",
            restored.get("restored") is True
            and bridge.call("get_inventory")["result"]["items"] == m29_inventory
            and bridge.call("get_agent_status")["result"]["position"] == m29_position,
        )

    # M30: isolate all entities and stash actual inventory stacks, including metadata.
    m30_surface = "m30-mining-power-fixture"
    m30_server = create_server(bridge)
    m30_inventory = bridge.call("get_inventory")["result"]["items"]
    m30_position = bridge.call("get_agent_status")["result"]["position"]

    def m30_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;local c=state.character;"
            "assert(c and c.valid);local inv=c.get_main_inventory();"
            f'local name="{m30_surface}";local s=game.surfaces[name];'
            "local b=storage.m30_backup;" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
        if "m30:" not in raw:
            raise RuntimeError("M30 fixture failed: " + raw)
        return json.loads(raw.split("m30:", 1)[1].strip())

    async def m30_tool(operation, params):
        response = await m30_server.call_tool(operation, params)
        assert not response.is_error, response
        return json.loads(response.content[0].text)

    def m30_call(operation, **params):
        return asyncio.run(m30_tool(operation, params))

    # Reproduce the old mining counter: all inventory items plus queued final products.
    m30_craft_counts = (
        "local n=0;local total=inv.get_item_count();"
        "for _,q in ipairs(c.crafting_queue or {}) do "
        "if q.prerequisite then n=n+q.count else "
        "for _,p in pairs(c.force.recipes[q.recipe].products) do "
        "if p.type=='item' then total=total+q.count*(p.amount or 0) end end end end;"
    )

    try:
        setup = m30_fixture(
            "assert(not s and not b,'M30 fixture already exists');"
            # Refuse before mutation if another test left work in progress: an empty
            # queue can be restored exactly without restarting partially completed crafts.
            "assert(not state.action and not state.craft_action and c.crafting_queue_size==0);"
            "b={position={x=c.position.x,y=c.position.y},surface=c.surface,"
            "inventory=game.create_inventory(#inv),mining=c.character_mining_speed_modifier,"
            "last_observation=state.last_observation,recent_observations=state.recent_observations,"
            "hand_crafted=helpers.table_to_json(state.hand_crafted),"
            "recipes={['electronic-circuit']=c.force.recipes['electronic-circuit'].enabled,"
            "['copper-cable']=c.force.recipes['copper-cable'].enabled},"
            "selected=c.selected};storage.m30_backup=b;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(b.inventory[i].swap_stack(inv[i])) end end;b.stashed=true;"
            "s=game.create_surface(name,{width=128,height=128});s.peaceful_mode=true;"
            "s.request_to_generate_chunks({0,0},2);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-64,63 do for y=-64,63 do "
            'tiles[#tiles+1]={name="grass-1",position={x,y}} end end;s.set_tiles(tiles);'
            "assert(c.teleport({0.5,4.5},s));assert(c.reach_distance<12);"
            "state.last_observation=nil;state.recent_observations={};"
            'b.rock=s.create_entity{name="big-rock",position={2.5,4.5}};'
            'b.tree=s.create_entity{name="tree-01",position={-1.5,4.5}};'
            'b.ore=s.create_entity{name="iron-ore",position={0.5,6.5},amount=100};'
            # Fixture creation needs no recipe unlocks; only gears are machine crafted.
            'b.pole=s.create_entity{name="small-electric-pole",position={0.5,0.5},force=c.force};'
            'b.far=s.create_entity{name="small-electric-pole",position={12.5,4.5},force=c.force};'
            'b.machine=s.create_entity{name="assembling-machine-1",position={2.5,0.5},'
            "force=c.force};assert(b.rock and b.tree and b.ore and b.pole and b.far and b.machine);"
            'b.machine.set_recipe("iron-gear-wheel");'
            'assert(b.machine.insert{name="iron-plate",count=100}==100);'
            "b.has_generator=prototypes.entity['electric-energy-interface']~=nil;"
            "if b.has_generator then "
            'b.generator=s.create_entity{name="electric-energy-interface",position={-1.5,0.5},'
            "force=c.force};assert(b.generator);b.generator.power_production=1000000/60;"
            "b.generator.power_usage=0;b.generator.electric_buffer_size=1000000;"
            "b.generator.energy=1000000 end;"
            "assert(c.can_reach_entity(b.pole) and c.can_reach_entity(b.rock));"
            "assert((c.position.x-b.rock.position.x)^2+(c.position.y-b.rock.position.y)^2 "
            "<=c.resource_reach_distance^2);"
            "b.products={};for _,p in pairs(b.rock.prototype.mineable_properties.products) do "
            "if p.type=='item' then b.products[p.name]=true end end;"
            # In 2.0.77 electronics unlocks both recipes; restore their exact flags below.
            "for recipe in pairs(b.recipes) do c.force.recipes[recipe].enabled=true end;"
            # One circuit needs three cables. Two copper plates make four, leaving
            # one cable that raises the old mining counter without any rock products.
            'assert(inv.insert{name="iron-plate",count=1}==1);'
            'assert(inv.insert{name="copper-plate",count=2}==2);'
            "rcon.print('m30:'..helpers.table_to_json({has_generator=b.has_generator}))"
        )
        observed = m30_call("observe_nearby")
        refs = {
            "rock": next(e["target_ref"] for e in observed["entities"] if e["name"] == "big-rock"),
            "tree": next(e["target_ref"] for e in observed["entities"] if e["name"] == "tree-01"),
            "ore": next(e["target_ref"] for e in observed["entities"] if e["name"] == "iron-ore"),
            "pole": next(
                e["target_ref"]
                for e in observed["entities"]
                if e["name"] == "small-electric-pole" and e["x"] == 0.5
            ),
            "far": next(
                e["target_ref"]
                for e in observed["entities"]
                if e["name"] == "small-electric-pole" and e["x"] == 12.5
            ),
        }
        craft = m30_call("craft", recipe="electronic-circuit", count=1)
        queued = m30_fixture(
            m30_craft_counts
            + "for product in pairs(b.products) do assert(inv.get_item_count(product)==0) end;"
            # Measure the actual queue, including partially completed work, before
            # starting mining. Slow this rock to outlast the entire remaining queue
            # by three seconds, while staying below the 20 s mining stall timeout.
            "assert(n>0,'M30 requires unfinished prerequisite crafts');"
            "assert(total==1 and inv.get_item_count('copper-cable')==0,"
            "'M30 requires the leftover cable to appear after mining starts');"
            "local craft_seconds=0;local recipes={};assert(c.crafting_speed>0);"
            "for i,q in ipairs(c.crafting_queue) do "
            "local count=q.count-(i==1 and c.crafting_queue_progress or 0);"
            "local seconds=count*c.force.recipes[q.recipe].energy/c.crafting_speed;"
            "craft_seconds=craft_seconds+seconds;"
            "recipes[#recipes+1]={recipe=q.recipe,seconds=seconds,prerequisite=q.prerequisite} end;"
            "local rock_seconds=math.max(5,craft_seconds+3);"
            "assert(rock_seconds<18,'M30 craft queue is too slow for mining stall timeout');"
            "local mining_time=b.rock.prototype.mineable_properties.mining_time;"
            "local base_speed=c.prototype.mining_speed;assert(mining_time>0 and base_speed>0);"
            "local original_speed=base_speed*(1+c.force.manual_mining_speed_modifier+b.mining);"
            "c.character_mining_speed_modifier=mining_time/(base_speed*rock_seconds)"
            "-1-c.force.manual_mining_speed_modifier;"
            "local mining_speed=base_speed*(1+c.force.manual_mining_speed_modifier"
            "+c.character_mining_speed_modifier);assert(mining_speed>0);"
            "rcon.print('m30:'..helpers.table_to_json({prerequisites=n,total_count=total,"
            "craft_seconds=craft_seconds,recipes=recipes,mining_time=mining_time,"
            "original_mining_speed=original_speed,mining_speed=mining_speed,"
            "rock_seconds=mining_time/mining_speed}))"
        )
        assert queued["prerequisites"] > 0, queued
        started = m30_call("mine_entity", target_ref=refs["rock"], count=1)
        assert abs(started["estimated_seconds"] - queued["rock_seconds"]) < 0.2, (started, queued)
        snapshots = []
        crafted_snapshot = None
        deadline = time.monotonic() + 30
        while True:
            # Normal protocol reads keep the connection heartbeat alive as well as
            # exercising the public running-detail count during concurrent crafts.
            live_status = m30_call("get_action_status", action_id=started["action_id"])
            # Capture existence and action progress atomically, avoiding an RCON race
            # where the rock disappears between a status read and a fixture read.
            snapshot = m30_fixture(
                f"local id={json.dumps(started['action_id'])};"
                "local a=(state.action and state.action.id==id and state.action) or "
                "state.completed_actions[id];assert(a);"
                + m30_craft_counts
                + "local products=0;for _,item in pairs(inv.get_contents()) do "
                "if b.products[item.name] then products=products+item.count end end;"
                "rcon.print('m30:'..helpers.table_to_json({exists=b.rock.valid,status=a.status,"
                "gained=(a.detail and a.detail.gained) or a.gained,products=products,"
                "prerequisites=n,total_count=total,queue_size=c.crafting_queue_size,"
                "circuits=inv.get_item_count('electronic-circuit'),"
                "cables=inv.get_item_count('copper-cable'),"
                "plates=inv.get_item_count('iron-plate')+inv.get_item_count('copper-plate')}))"
            )
            snapshot["reported_gained"] = live_status["detail"]["gained"]
            snapshots.append(snapshot)
            # Preserve the completed circuit and leftover cable while mining still
            # runs; later samples verify the mine never finishes before removal.
            if crafted_snapshot is None and snapshot["exists"] and snapshot["queue_size"] == 0:
                crafted_snapshot = snapshot
            if snapshot["status"] != "running" or time.monotonic() >= deadline:
                break
            time.sleep(1 / 60)
        record(
            "M30 rock stays running with zero gained after crafting leaves an extra cable",
            crafted_snapshot is not None
            and crafted_snapshot["exists"]
            and crafted_snapshot["status"] == "running"
            and crafted_snapshot["gained"] == crafted_snapshot["reported_gained"] == 0
            and crafted_snapshot["products"] == 0
            and crafted_snapshot["prerequisites"] == 0
            and crafted_snapshot["circuits"] == crafted_snapshot["cables"] == 1
            and crafted_snapshot["plates"] == 0
            # The leftover cable raises the old counter by one and would finish early.
            and crafted_snapshot["total_count"] == queued["total_count"] + 1
            and all(
                s["status"] == "running" and s["gained"] == s["reported_gained"] == 0
                for s in snapshots
                if s["exists"]
            ),
            json.dumps({"queued": queued, "crafted": crafted_snapshot, "last": snapshot}),
        )
        rock_result = m30_call("get_action_status", action_id=started["action_id"])
        record(
            "M30 rock succeeds only after removal; gained is exactly its item products",
            snapshot["status"] == "succeeded"
            and not snapshot["exists"]
            and snapshot["products"] > 1
            and snapshot["gained"] == snapshot["products"]
            and rock_result["detail"]["gained"] == snapshot["products"],
        )
        crafted = m30_call("wait_for_action", action_id=craft["action_id"], timeout_seconds=30)
        assert crafted["status"] == "succeeded", crafted
        m30_fixture(
            "c.character_mining_speed_modifier=b.mining;rcon.print('m30:{\"normal_speed\":true}')"
        )
        for key in ("tree", "ore"):
            started = m30_call("mine_entity", target_ref=refs[key], count=1)
            done = m30_call("wait_for_action", action_id=started["action_id"], timeout_seconds=20)
            exists = m30_fixture(
                "rcon.print('m30:'..helpers.table_to_json({exists=b." + key + ".valid}))"
            )["exists"]
            record(
                "M30 ordinary " + key + " mine succeeds",
                done["status"] == "succeeded"
                and done["detail"]["gained"] >= 1
                and exists == (key == "ore"),
                json.dumps(done),
            )

        # Allow the entire five-second average to fill even with a very fast miner.
        time.sleep(5.2)
        pole = m30_call("get_entity_inventory", target_ref=refs["pole"])
        power = m30_fixture(
            "rcon.print('m30:'..helpers.table_to_json({id=b.pole.electric_network_id,"
            "finished=b.machine.products_finished}))"
        )
        network = pole.get("electric_network", {})

        def m30_flow_list(entries):
            return (
                isinstance(entries, list)
                and len(entries) <= 10
                and all(
                    isinstance(e.get("name"), str)
                    and isinstance(e.get("kw"), (float, int))
                    and e["kw"] > 0
                    and round(e["kw"], 1) == e["kw"]
                    for e in entries
                )
                and [e["kw"] for e in entries] == sorted((e["kw"] for e in entries), reverse=True)
            )

        record(
            "M30 nearby pole read has empty inventories and matching electric network statistics",
            pole["inventories"] == {}
            and network.get("id") == power.get("id")
            and power.get("id") is not None
            and all(
                isinstance(network.get(k), (int, float)) and network[k] >= 0
                for k in ("production_kw", "consumption_kw")
            )
            and all(m30_flow_list(network.get(k)) for k in ("producers", "consumers")),
            json.dumps(pole),
        )
        if setup["has_generator"]:
            record(
                "M30 powered machine consumes power; interface produces power in kW",
                power["finished"] > 0
                and 0 < network.get("production_kw", 0) <= 1000
                and 0 < network.get("consumption_kw", 0) < 1000
                and any(e["name"] == "electric-energy-interface" for e in network["producers"])
                and any(e["name"] == "assembling-machine-1" for e in network["consumers"]),
            )
        else:
            record(
                "M30 no interface prototype: tested zero power and field shape only",
                network.get("production_kw") == 0 and network.get("producers") == [],
            )
        batch = m30_call("get_entity_inventories", target_refs=[refs["pole"], refs["far"]])
        record(
            "M30 batch pole read succeeds and distant pole returns out_of_range",
            batch["results"][0]["ok"]
            and batch["results"][0]["result"]["electric_network"]["id"] == power["id"]
            and not batch["results"][1]["ok"]
            and batch["results"][1]["error"] == "out_of_range",
        )
        expect_error(
            "M30 distant pole single read returns out_of_range",
            "get_entity_inventory",
            {"target_ref": refs["far"]},
            {"out_of_range"},
        )
        expect_error(
            "M30 transfer to pole still rejects unsupported_entity",
            "transfer_item",
            {
                "target_ref": refs["pole"],
                "direction": "to_entity",
                "inventory_role": "main",
                "item": "iron-plate",
                "count": 1,
            },
            {"invalid_target"},
        )
        record(
            "M30 pole transfer preserves the existing unsupported_entity detail",
            errors["M30 transfer to pole still rejects unsupported_entity"].detail["reason"]
            == "unsupported_entity",
        )
    finally:
        # Cancel through the mod so mining state, actions and crafting returns are
        # handled normally, even if a preceding assertion failed mid-action.
        for slot in ("action", "craft_action"):
            pending = m30_fixture(
                f"rcon.print('m30:'..helpers.table_to_json({{id=b and state.{slot} "
                f"and state.{slot}.id}}))"
            )
            if pending.get("id"):
                bridge.call("cancel_action", {"action_id": pending["id"]})
        restored = m30_fixture(
            "if b then while c.crafting_queue_size>0 do "
            "c.cancel_crafting{index=1,count=c.crafting_queue[1].count} end;"
            "c.character_mining_speed_modifier=b.mining;"
            "assert(c.character_mining_speed_modifier==b.mining);"
            "for recipe,enabled in pairs(b.recipes) do "
            "c.force.recipes[recipe].enabled=enabled;"
            "assert(c.force.recipes[recipe].enabled==enabled) end;"
            "assert(c.teleport(b.position,b.surface));c.selected=b.selected;"
            # Clear fixture products, including the circuit and leftover cable,
            # before swapping the original stacks back into their exact slots.
            "if b.stashed then inv.clear() end;"
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            "for i=1,#b.inventory do assert(not b.inventory[i].valid_for_read) end;"
            "b.inventory.destroy();state.hand_crafted=helpers.json_to_table(b.hand_crafted);"
            "state.last_observation=b.last_observation;"
            "state.recent_observations=b.recent_observations;"
            "for ref,entry in pairs(state.targets) do if entry.entity and entry.entity.valid "
            "and entry.entity.surface==s then if entry.unit_number then "
            "state.owned_refs[entry.unit_number]=nil end;state.targets[ref]=nil end end;"
            "if s then assert(game.delete_surface(s)) end;state.explored_map[name]=nil;"
            "storage.m30_backup=nil;assert(c.crafting_queue_size==0);"
            "assert(c.position.x==b.position.x and c.position.y==b.position.y "
            "and c.surface==b.surface) end;rcon.print('m30:{\"restored\":true}')"
        )
        observed_map_cells.pop(m30_surface, None)
        record(
            "M30 restores exact inventory slots, empty queue and position; removes fixture",
            restored.get("restored") is True
            and bridge.call("get_inventory")["result"]["items"] == m30_inventory
            and bridge.call("get_agent_status")["result"]["position"] == m30_position,
        )

    # M31: real chest automation, long-running walks and pole placement statistics.
    m31_surface = "m31-limit-walk-power-fixture"
    m31_server = create_server(bridge)
    m31_inventory = bridge.call("get_inventory")["result"]["items"]
    m31_position = bridge.call("get_agent_status")["result"]["position"]

    def m31_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;local c=state.character;"
            "assert(c and c.valid);local inv=c.get_main_inventory();"
            f'local name="{m31_surface}";local s=game.surfaces[name];'
            "local b=storage.m31_backup;" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
        if "m31:" not in raw:
            raise RuntimeError("M31 fixture failed: " + raw)
        return json.loads(raw.split("m31:", 1)[1].strip())

    async def m31_tool(operation, params, error=None):
        response = await m31_server.call_tool(operation, params)
        value = json.loads(response.content[0].text)
        assert bool(response.is_error) == (error is not None), value
        if error:
            assert value["error"] == error, value
        return value

    def m31_call(operation, **params):
        return asyncio.run(m31_tool(operation, params))

    def m31_error(operation, error, **params):
        # Keep M31's Mod conflict coverage immediate; M32 tests server waits below.
        try:
            bridge.call(operation, params)
        except BridgeError as exc:
            assert exc.code == error, exc
            return {"error": exc.code, "detail": exc.detail}
        raise AssertionError("Expected Mod error: " + error)

    try:
        m31_fixture(
            "assert(not s and not b,'M31 fixture already exists');"
            "assert(not state.action and not state.craft_action and c.crafting_queue_size==0);"
            "b={position={x=c.position.x,y=c.position.y},surface=c.surface,"
            "inventory=game.create_inventory(#inv),speed=c.character_running_speed_modifier,"
            "last_observation=state.last_observation,recent_observations=state.recent_observations,"
            "selected=c.selected};storage.m31_backup=b;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(b.inventory[i].swap_stack(inv[i])) end end;b.stashed=true;"
            "s=game.create_surface(name,{width=256,height=128});s.peaceful_mode=true;"
            "s.request_to_generate_chunks({0,0},4);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-128,127 do for y=-64,63 do "
            'tiles[#tiles+1]={name="grass-1",position={x,y}} end end;s.set_tiles(tiles);'
            "assert(c.teleport({0.5,4.5},s));assert(c.reach_distance<12);"
            "state.last_observation=nil;state.recent_observations={};"
            'b.chest=s.create_entity{name="wooden-chest",position={2.5,4.5},force=c.force};'
            'b.source=s.create_entity{name="wooden-chest",position={2.5,2.5},force=c.force};'
            'b.feeder=s.create_entity{name="burner-inserter",position={2.5,3.5},'
            "direction=defines.direction.north,force=c.force};"
            'b.far=s.create_entity{name="wooden-chest",position={12.5,4.5},force=c.force};'
            "assert(b.chest and b.source and b.feeder and b.far);b.feeder.active=false;"
            'assert(b.feeder.get_fuel_inventory().insert{name="coal",count=10}==10);'
            'assert(b.source.insert{name="transport-belt",count=300}==300);'
            'assert(b.chest.insert{name="transport-belt",count=98}==98);'
            # Existing contents in red slots must survive setting the limit.
            'b.chest.get_inventory(defines.inventory.chest)[3].set_stack{name="coal",count=7};'
            'assert(inv.insert{name="small-electric-pole",count=5}==5);'
            'assert(inv.insert{name="wooden-chest",count=1}==1);'
            "rcon.print('m31:'..helpers.table_to_json({ready=true}))"
        )
        seen = m31_call("observe_nearby")
        refs = {
            key: next(
                e["target_ref"]
                for e in seen["entities"]
                if e["name"] == name and e["x"] == x and e["y"] == y
            )
            for key, name, x, y in (
                ("chest", "wooden-chest", 2.5, 4.5),
                ("source", "wooden-chest", 2.5, 2.5),
                ("feeder", "burner-inserter", 2.5, 3.5),
                ("far", "wooden-chest", 12.5, 4.5),
            )
        }
        limited = m31_call("set_inventory_limit", target_ref=refs["chest"], slots=1)
        record(
            "M31 setter returns one usable slot and total chest slots",
            limited["inventory_limit"] == 1
            and limited["slots"] == 16
            and limited["target_ref"] == refs["chest"],
        )
        m31_fixture("b.feeder.active=true;rcon.print('m31:{}')")
        deadline = time.monotonic() + 15
        while True:
            snapshot = m31_fixture(
                "local i=b.chest.get_inventory(defines.inventory.chest);"
                "rcon.print('m31:'..helpers.table_to_json({bar=i.get_bar(),"
                "belts=i.get_item_count('transport-belt'),coal=i.get_item_count('coal'),"
                "source=b.source.get_item_count('transport-belt')}))"
            )
            if snapshot["belts"] >= 100 or time.monotonic() >= deadline:
                break
            m31_call("factorio_ping")
            time.sleep(0.25)
        time.sleep(3)
        stable = m31_fixture(
            "rcon.print('m31:'..helpers.table_to_json({belts=b.chest.get_item_count('transport-belt'),"
            "source=b.source.get_item_count('transport-belt')}))"
        )
        record(
            "M31 real inserter stops at one stack; red-slot contents remain",
            snapshot["bar"] == 2
            and snapshot["belts"] == stable["belts"] == 100
            and snapshot["coal"] == 7
            and stable["source"] > 100,
            json.dumps(snapshot),
        )
        m31_fixture("b.feeder.active=false;rcon.print('m31:{}')")
        single = m31_call("get_entity_inventory", target_ref=refs["chest"])
        batch = m31_call("get_entity_inventories", target_refs=[refs["chest"]])
        record(
            "M31 inventory reads preserve inventory_limit",
            single["inventory_limit"] == batch["results"][0]["result"]["inventory_limit"] == 1,
        )
        for compact in (False, True):
            observed = m31_call("observe_nearby", compact=compact)
            chest = next(e for e in observed["entities"] if e["target_ref"] == refs["chest"])
            source = next(e for e in observed["entities"] if e["target_ref"] == refs["source"])
            record(
                f"M31 observation compact={compact} retains only actual chest limits",
                chest["inventory_limit"] == 1 and "inventory_limit" not in source,
            )
        for slots in (17, 1.5, -1, False):
            # Exercise Mod-side validation directly, including non-integer JSON values.
            expect_error(
                f"M31 slots={slots!r} is invalid_params",
                "set_inventory_limit",
                {"target_ref": refs["chest"], "slots": slots},
                {"invalid_params"},
            )
            unchanged = m31_call("get_entity_inventory", target_ref=refs["chest"])
            record(
                f"M31 invalid slots={slots!r} changes nothing", unchanged["inventory_limit"] == 1
            )
        expect_error(
            "M31 non-container rejected",
            "set_inventory_limit",
            {"target_ref": refs["feeder"], "slots": 1},
            {"invalid_target"},
        )
        expect_error(
            "M31 distant chest rejected",
            "set_inventory_limit",
            {"target_ref": refs["far"], "slots": 1},
            {"out_of_range"},
        )
        zero = m31_call("set_inventory_limit", target_ref=refs["chest"], slots=0)
        record("M31 zero usable slots is represented as zero", zero["inventory_limit"] == 0)
        for args in ({"slots": None}, {}):
            cleared = m31_call("set_inventory_limit", target_ref=refs["chest"], **args)
            observed = m31_call("observe_nearby", compact=True)
            chest = next(e for e in observed["entities"] if e["target_ref"] == refs["chest"])
            read = m31_call("get_entity_inventory", target_ref=refs["chest"])
            bar = m31_fixture(
                "rcon.print('m31:'..helpers.table_to_json({bar=b.chest.get_inventory("
                "defines.inventory.chest).get_bar()}))"
            )["bar"]
            record(
                f"M31 clear limit with {args} restores no limit",
                cleared["inventory_limit"] is None
                and bar == 17
                and "inventory_limit" not in chest
                and "inventory_limit" not in read,
            )

        # No generators exist on the fixture surface during these placements.
        pole = m31_call(
            "place_entity", item="small-electric-pole", surface=m31_surface, x=-2.5, y=4.5
        )
        poles = m31_call(
            "place_entities",
            items=[{"item": "small-electric-pole", "surface": m31_surface, "x": -4.5, "y": 4.5}],
        )
        for label, placed in (("single", pole), ("batch", poles["results"][0]["result"])):
            record(
                "M31 " + label + " isolated pole reports empty producers and guidance",
                placed["electric_network"]["producers"] == []
                and "last 5 seconds" in placed["hint"]
                and "nothing consumes" in placed["hint"]
                and "may not be connected" in placed["hint"],
                json.dumps(placed),
            )

        # Slow the fixture character so a 64-tile walk outlasts two short waits
        # and conflict checks even on slow CI. The Mod still moves only by walking.
        m31_fixture("c.character_running_speed_modifier=-0.5;rcon.print('m31:{}')")
        walk = m31_call("move_to", surface=m31_surface, x=64.5, y=4.5, wait_seconds=1)
        record(
            "M31 move timeout reports remaining distance and next action",
            walk["status"] == "running"
            and walk["remaining_distance"] > 0
            and "wait_for_action" in walk["hint"]
            and walk["action_id"] in walk["hint"]
            and "action_conflict" in walk["hint"],
            json.dumps(walk),
        )
        waiting = m31_call("wait_for_action", action_id=walk["action_id"], timeout_seconds=1)
        record(
            "M31 wait timeout preserves walk guidance and distance",
            waiting["status"] == "running"
            and waiting["remaining_distance"] > 0
            and "wait_for_action" in waiting["hint"],
        )
        conflict = m31_error(
            "place_entity",
            "action_conflict",
            item="wooden-chest",
            surface=m31_surface,
            x=0.5,
            y=7.5,
        )
        running = conflict["detail"]["running_action"]
        record(
            "M31 placement conflict reports running walk distance",
            running["type"] == "move"
            and running["id"] == walk["action_id"]
            and running["remaining_distance"] > 0,
        )
        deadline = time.monotonic() + 100
        while waiting["status"] == "running" and time.monotonic() < deadline:
            waiting = m31_call("wait_for_action", action_id=walk["action_id"], timeout_seconds=50)
        record(
            "M31 walk eventually succeeds", waiting["status"] == "succeeded", json.dumps(waiting)
        )

        # Fixture teleport only: put the character back to inspect a powered network.
        generator = m31_fixture(
            "assert(c.teleport({0.5,4.5},s));c.character_running_speed_modifier=b.speed;"
            "local available=prototypes.entity['electric-energy-interface']~=nil;"
            "if available then "
            'b.pole=s.create_entity{name="small-electric-pole",position={0.5,-0.5},force=c.force};'
            'b.machine=s.create_entity{name="assembling-machine-1",position={2.5,-0.5},force=c.force};'
            'b.generator=s.create_entity{name="electric-energy-interface",position={-1.5,-0.5},'
            "force=c.force};assert(b.pole and b.machine and b.generator);"
            'b.machine.set_recipe("iron-gear-wheel");'
            'assert(b.machine.insert{name="iron-plate",count=100}==100);'
            "b.generator.power_production=1000000/60;b.generator.power_usage=0;"
            "b.generator.electric_buffer_size=1000000;b.generator.energy=1000000 end;"
            "rcon.print('m31:'..helpers.table_to_json({available=available}))"
        )
        if generator["available"]:
            time.sleep(5.2)
            powered = m31_call(
                "place_entity", item="small-electric-pole", surface=m31_surface, x=-2.5, y=0.5
            )
            powered_batch = m31_call(
                "place_entities",
                items=[
                    {"item": "small-electric-pole", "surface": m31_surface, "x": -4.5, "y": 0.5}
                ],
            )
            for label, placed in (
                ("single", powered),
                ("batch", powered_batch["results"][0]["result"]),
            ):
                record(
                    "M31 " + label + " pole joining powered network reports producers",
                    any(
                        e["name"] == "electric-energy-interface"
                        for e in placed["electric_network"]["producers"]
                    )
                    and "No generator produced" not in placed.get("hint", ""),
                    json.dumps(placed),
                )
        else:
            record("M31 no generator prototype: empty-network placements tested only", True)
    finally:
        for slot in ("action", "craft_action"):
            pending = m31_fixture(
                f"rcon.print('m31:'..helpers.table_to_json({{id=b and state.{slot} "
                f"and state.{slot}.id}}))"
            )
            if pending.get("id"):
                bridge.call("cancel_action", {"action_id": pending["id"]})
        restored = m31_fixture(
            "if b then while c.crafting_queue_size>0 do "
            "c.cancel_crafting{index=1,count=c.crafting_queue[1].count} end;"
            "c.character_running_speed_modifier=b.speed;"
            "assert(c.character_running_speed_modifier==b.speed);"
            "assert(c.teleport(b.position,b.surface));c.selected=b.selected;"
            "if b.stashed then inv.clear() end;"
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            "for i=1,#b.inventory do assert(not b.inventory[i].valid_for_read) end;"
            "b.inventory.destroy();state.last_observation=b.last_observation;"
            "state.recent_observations=b.recent_observations;"
            "for ref,entry in pairs(state.targets) do if entry.entity and entry.entity.valid "
            "and entry.entity.surface==s then if entry.unit_number then "
            "state.owned_refs[entry.unit_number]=nil end;state.targets[ref]=nil end end;"
            "if s then assert(game.delete_surface(s)) end;state.explored_map[name]=nil;"
            "storage.m31_backup=nil;assert(c.crafting_queue_size==0);"
            "assert(c.position.x==b.position.x and c.position.y==b.position.y "
            "and c.surface==b.surface) end;"
            "rcon.print('m31:'..helpers.table_to_json({restored=true}))"
        )
        observed_map_cells.pop(m31_surface, None)
        record(
            "M31 removes fixture and restores exact inventory slots, empty queue and position",
            restored.get("restored") is True
            and bridge.call("get_inventory")["result"]["items"] == m31_inventory
            and bridge.call("get_agent_status")["result"]["position"] == m31_position,
        )

    # M32: automatic walk waits and actionable character-in-the-way suggestions.
    m32_surface = "m32-walk-wait-stand-fixture"
    m32_server = create_server(bridge)
    m32_inventory = bridge.call("get_inventory")["result"]["items"]
    m32_position = bridge.call("get_agent_status")["result"]["position"]

    def m32_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;local c=state.character;"
            "assert(c and c.valid);local inv=c.get_main_inventory();"
            f'local name="{m32_surface}";local s=game.surfaces[name];'
            "local b=storage.m32_backup;" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
        if "m32:" not in raw:
            raise RuntimeError("M32 fixture failed: " + raw)
        return json.loads(raw.split("m32:", 1)[1].strip())

    async def m32_tool(operation, params, error):
        response = await m32_server.call_tool(operation, params)
        value = json.loads(response.content[0].text)
        assert bool(response.is_error) == (error is not None), value
        if error:
            assert value["error"] == error, value
        return value

    def m32_call(operation, error=None, **params):
        return asyncio.run(m32_tool(operation, params, error))

    try:
        m32_fixture(
            "assert(not s and not b,'M32 fixture already exists');"
            "assert(not state.action and not state.craft_action and c.crafting_queue_size==0);"
            "b={position={x=c.position.x,y=c.position.y},surface=c.surface,"
            "inventory=game.create_inventory(#inv),speed=c.character_running_speed_modifier,"
            "last_observation=state.last_observation,recent_observations=state.recent_observations,"
            "selected=c.selected};storage.m32_backup=b;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(b.inventory[i].swap_stack(inv[i])) end end;b.stashed=true;"
            "s=game.create_surface(name,{width=256,height=128});s.peaceful_mode=true;"
            "s.request_to_generate_chunks({0,0},4);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-128,127 do for y=-64,63 do "
            'tiles[#tiles+1]={name="grass-1",position={x,y}} end end;s.set_tiles(tiles);'
            "assert(c.teleport({0.5,4.5},s));c.character_running_speed_modifier=-0.5;"
            "state.last_observation=nil;state.recent_observations={};"
            'assert(inv.insert{name="stone-furnace",count=3}==3);'
            'assert(inv.insert{name="coal",count=10}==10);'
            'b.chest=s.create_entity{name="wooden-chest",position={18.5,7.5},force=c.force};'
            'b.ore=s.create_entity{name="iron-ore",position={2.5,4.5},amount=10000};'
            "assert(b.chest and b.ore);rcon.print('m32:{}')"
        )
        short = m32_call("move_to", surface=m32_surface, x=16.5, y=4.5, wait_seconds=1)
        assert short["status"] == "running", short
        placed = m32_call("place_entity", item="stone-furnace", surface=m32_surface, x=16, y=8)
        record(
            "M32 placement waits for short walk and succeeds at destination",
            placed["waited_for_walk"]["action_id"] == short["action_id"]
            and placed["waited_for_walk"]["status"] == "succeeded",
            json.dumps(placed),
        )
        seen = m32_call("observe_nearby")
        chest_ref = next(e["target_ref"] for e in seen["entities"] if e["name"] == "wooden-chest")
        # This straight walk takes about 42 seconds at one quarter normal speed.
        m32_fixture("c.character_running_speed_modifier=-0.75;rcon.print('m32:{}')")
        long = m32_call("move_to", surface=m32_surface, x=110.5, y=4.5, wait_seconds=1)
        assert long["status"] == "running", long
        transfer = m32_call(
            "transfer_item",
            error="action_conflict",
            target_ref=chest_ref,
            direction="to_entity",
            inventory_role="main",
            item="coal",
            count=1,
        )
        record(
            "M32 long walk transfer times out with wait metadata and remaining distance",
            transfer["waited_for_walk"]["action_id"] == long["action_id"]
            and transfer["waited_for_walk"]["status"] == "running"
            and transfer["waited_for_walk"]["seconds"] >= 15
            and transfer["detail"]["running_action"]["remaining_distance"] > 0,
            json.dumps(transfer),
        )
        finished = m32_call("wait_for_action", action_id=long["action_id"], timeout_seconds=50)
        assert finished["status"] == "succeeded", finished
        m32_fixture("assert(c.teleport({0.5,4.5},s));rcon.print('m32:{}')")
        seen = m32_call("observe_nearby")
        ore_ref = next(e["target_ref"] for e in seen["entities"] if e["name"] == "iron-ore")
        mining = m32_call("mine_entity", target_ref=ore_ref, count=100)
        started = time.monotonic()
        busy = m32_call(
            "place_entity",
            error="action_conflict",
            item="stone-furnace",
            surface=m32_surface,
            x=0,
            y=8,
        )
        record(
            "M32 running mine conflicts immediately without walk wait",
            "waited_for_walk" not in busy
            and time.monotonic() - started < 5
            and busy["detail"]["running_action"]["type"] == "mine",
        )
        m32_call("cancel_action", action_id=mining["action_id"])
        m32_fixture(
            "assert(c.teleport({0,0},s));c.character_running_speed_modifier=b.speed;"
            "rcon.print('m32:{}')"
        )
        placement_args = {"item": "stone-furnace", "surface": m32_surface, "x": 0, "y": 0}
        checked = m32_call("check_placement", **placement_args)
        blocked = m32_call("place_entity", error="blocked", **placement_args)
        batch = m32_call("place_entities", items=[placement_args])
        positions = blocked["detail"]["stand_positions"]
        if not positions:
            # This is the unrotated furnace at (0, 0), so its prototype box is
            # already in world coordinates. Mirror the bounded 16-point search
            # and tick_action's largest default successful arrival radius (1.5).
            diagnostic = m32_fixture(
                "local area=prototypes.entity['stone-furnace'].collision_box;"
                "local box=c.prototype.collision_box;local arrival=1.5;"
                "local clearance=arrival+0.15;"
                "local left=area.left_top.x-box.right_bottom.x-clearance;"
                "local right=area.right_bottom.x-box.left_top.x+clearance;"
                "local top=area.left_top.y-box.right_bottom.y-clearance;"
                "local bottom=area.right_bottom.y-box.left_top.y+clearance;"
                "local reach=c.build_distance;local candidates={};"
                "local function outside(p) return p and (p.x<=left or p.x>=right "
                "or p.y<=top or p.y>=bottom) or false end;"
                "local function dist(a,p) return math.sqrt((a.x-p.x)^2+(a.y-p.y)^2) end;"
                "local function visible(p) if dist(c.position,p)<=16 then return true end;"
                "for _,o in ipairs(state.recent_observations) do "
                "if o.surface==s.name and game.tick-o.tick<=3600 "
                "and dist(o.position,p)<=(o.radius or 16) then return true end end;"
                "return false end;"
                "local function check(p) "
                "local free=s.find_non_colliding_position(c.name,p,0.5,0.1);"
                "local distance=dist(p,{x=0,y=0});"
                "candidates[#candidates+1]={candidate=p,"
                "can_place=s.can_place_entity{name=c.name,position=p,force=c.force},"
                "find_non_colliding_position=free or false,outside_box=outside(p),"
                "search_outside_box=outside(free),distance=distance,"
                "build_distance=reach or false,build_distance_type=type(reach),"
                "in_build_distance=type(reach)=='number' and distance<=reach,"
                "arrival_in_build_distance=type(reach)=='number' and distance+arrival<=reach,"
                "visible=visible(p)} end;"
                "for i=0,3 do local t=i/4;check({x=left+(right-left)*t,y=top});"
                "check({x=right,y=top+(bottom-top)*t});"
                "check({x=right-(right-left)*t,y=bottom});"
                "check({x=left,y=bottom-(bottom-top)*t}) end;"
                "rcon.print('m32:'..helpers.table_to_json({area=area,character_box=box,"
                "character_position=c.position,surface=s.name,arrival_radius=arrival,"
                "candidates=candidates}))"
            )
            print("M32 stand-position diagnostic " + json.dumps(diagnostic), flush=True)
        record(
            "M32 single, batch and preview return stand positions for character obstruction",
            blocked["detail"]["reason"] == checked["blocked_reason"] == "character_in_the_way"
            and 1 <= len(positions) <= 3
            and checked["stand_positions"] == positions
            and batch["results"][0]["detail"]["stand_positions"] == positions
            and [math.hypot(p["x"], p["y"]) for p in positions]
            == sorted(math.hypot(p["x"], p["y"]) for p in positions),
            json.dumps(blocked),
        )
        if positions:
            m32_call("move_to", surface=m32_surface, **positions[0])
            replaced = m32_call("place_entity", **placement_args)
            record("M32 move_to suggested stand position then placement succeeds", replaced["ok"])
        else:
            record(
                "M32 move_to suggested stand position then placement succeeds",
                False,
                "no stand position returned; see fixture diagnostic above",
            )
    finally:
        for slot in ("action", "craft_action"):
            pending = m32_fixture(
                f"rcon.print('m32:'..helpers.table_to_json({{id=b and state.{slot} "
                f"and state.{slot}.id}}))"
            )
            if pending.get("id"):
                bridge.call("cancel_action", {"action_id": pending["id"]})
        restored = m32_fixture(
            "if b then while c.crafting_queue_size>0 do "
            "c.cancel_crafting{index=1,count=c.crafting_queue[1].count} end;"
            "c.character_running_speed_modifier=b.speed;"
            "assert(c.character_running_speed_modifier==b.speed);"
            "assert(c.teleport(b.position,b.surface));c.selected=b.selected;"
            "if b.stashed then inv.clear() end;"
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            "for i=1,#b.inventory do assert(not b.inventory[i].valid_for_read) end;"
            "b.inventory.destroy();state.last_observation=b.last_observation;"
            "state.recent_observations=b.recent_observations;"
            "for ref,entry in pairs(state.targets) do if entry.entity and entry.entity.valid "
            "and entry.entity.surface==s then if entry.unit_number then "
            "state.owned_refs[entry.unit_number]=nil end;state.targets[ref]=nil end end;"
            "if s then assert(game.delete_surface(s)) end;state.explored_map[name]=nil;"
            "storage.m32_backup=nil;assert(c.crafting_queue_size==0);"
            "assert(c.position.x==b.position.x and c.position.y==b.position.y "
            "and c.surface==b.surface) end;"
            "rcon.print('m32:'..helpers.table_to_json({restored=true}))"
        )
        observed_map_cells.pop(m32_surface, None)
        record(
            "M32 removes all fixture entities and restores exact inventory, queue and position",
            restored.get("restored") is True
            and bridge.call("get_inventory")["result"]["items"] == m32_inventory
            and bridge.call("get_agent_status")["result"]["position"] == m32_position,
        )

    # M33: strict tool arguments and absolute rotations in one Mod request.
    # The installed 2.0.77 base/prototypes/entity/entities.lua sets
    # storage-tank.two_direction_only=true. Probe its native cycle as well.
    m33_surface = "m33-absolute-rotation-fixture"
    m33_server = create_server(bridge)
    m33_inventory = bridge.call("get_inventory")["result"]["items"]
    m33_position = bridge.call("get_agent_status")["result"]["position"]

    def m33_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;local c=state.character;"
            "assert(c and c.valid);local inv=c.get_main_inventory();"
            f'local name="{m33_surface}";local s=game.surfaces[name];'
            "local b=storage.m33_backup;" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
        if "m33:" not in raw:
            raise RuntimeError("M33 fixture failed: " + raw)
        return json.loads(raw.split("m33:", 1)[1].strip())

    async def m33_tool(arguments):
        try:
            response = await m33_server.call_tool("rotate_entity", arguments)
        except ToolError as exc:
            return True, json.loads(str(exc))
        return bool(response.is_error), json.loads(response.content[0].text)

    try:
        cycle = m33_fixture(
            "assert(not s and not b,'M33 fixture already exists');"
            "assert(not state.action and not state.craft_action and c.crafting_queue_size==0);"
            "b={position={x=c.position.x,y=c.position.y},surface=c.surface,"
            "inventory=game.create_inventory(#inv),selected=c.selected,"
            "last_observation=state.last_observation,recent_observations=state.recent_observations};"
            "storage.m33_backup=b;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(b.inventory[i].swap_stack(inv[i])) end end;b.stashed=true;"
            "s=game.create_surface(name,{width=64,height=64});s.peaceful_mode=true;"
            "s.request_to_generate_chunks({0,0},1);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-32,31 do for y=-32,31 do "
            'tiles[#tiles+1]={name="grass-1",position={x,y}} end end;s.set_tiles(tiles);'
            "assert(c.teleport({0.5,0.5},s));"
            "state.last_observation=nil;state.recent_observations={};"
            'b.inserter=s.create_entity{name="inserter",position={2.5,0.5},'
            "force=c.force,direction=0};"
            'b.tank=s.create_entity{name="storage-tank",position={-2.5,0.5},'
            "force=c.force,direction=0};"
            'b.chest=s.create_entity{name="wooden-chest",position={0.5,2.5},force=c.force};'
            "assert(b.inserter and b.tank and b.chest);"
            "local original=b.tank.direction;local seen={[original]=true};"
            "for i=1,4 do assert(b.tank.rotate{reverse=false});seen[b.tank.direction]=true end;"
            "b.tank.direction=original;local reachable={};"
            "for d in pairs(seen) do reachable[#reachable+1]=d end;table.sort(reachable);"
            "rcon.print('m33:'..helpers.table_to_json({original=original,reachable=reachable}))"
        )
        seen = bridge.call("observe_nearby")["result"]["entities"]
        refs = {
            e["name"]: e["target_ref"]
            for e in seen
            if e["name"] in ("inserter", "storage-tank", "wooden-chest")
        }
        for direction in (0, 4, 8, 12):
            m33_fixture("b.inserter.direction=0;rcon.print('m33:{}')")
            with patch.object(bridge, "call", wraps=bridge.call) as calls:
                is_error, value = asyncio.run(
                    m33_tool({"target_ref": refs["inserter"], "direction": direction})
                )
            actual = m33_fixture(
                "rcon.print('m33:'..helpers.table_to_json({direction=b.inserter.direction}))"
            )
            record(
                f"M33 inserter absolute direction {direction} uses {direction // 4} rotations",
                not is_error
                and value["direction"] == actual["direction"] == direction
                and value["rotations"] == direction // 4
                and calls.call_count == 1,
                json.dumps(value),
            )
        is_error, already = asyncio.run(m33_tool({"target_ref": refs["inserter"], "direction": 12}))
        record(
            "M33 already at requested direction rotates zero times",
            not is_error and already["rotations"] == 0 and already["direction"] == 12,
            json.dumps(already),
        )
        is_error, plain = asyncio.run(m33_tool({"target_ref": refs["inserter"]}))
        record(
            "M33 plain rotation preserves behavior and reports one rotation",
            not is_error and plain["rotations"] == 1 and plain["direction"] == 0,
        )
        record(
            "M33 storage tank has a native two-direction rotation cycle",
            len(cycle["reachable"]) == 2,
        )
        unreachable = next(d for d in (0, 4, 8, 12) if d not in cycle["reachable"])
        is_error, value = asyncio.run(
            m33_tool({"target_ref": refs["storage-tank"], "direction": unreachable})
        )
        actual = m33_fixture(
            "rcon.print('m33:'..helpers.table_to_json({direction=b.tank.direction}))"
        )
        record(
            "M33 unreachable tank direction restores exact original direction",
            is_error
            and value["error"] == "invalid_target"
            and value["detail"]
            == {
                "reason": "direction_unreachable",
                "direction": cycle["original"],
                "requested_direction": unreachable,
                "reachable_directions": cycle["reachable"],
            }
            and actual["direction"] == cycle["original"],
            json.dumps(value),
        )
        is_error, value = asyncio.run(
            m33_tool({"target_ref": refs["wooden-chest"], "direction": 4})
        )
        record(
            "M33 non-rotatable entity reports direction_unreachable",
            is_error
            and value["error"] == "invalid_target"
            and value["detail"]
            == {
                "reason": "direction_unreachable",
                "direction": 0,
                "requested_direction": 4,
                "reachable_directions": [0],
            },
            json.dumps(value),
        )
        with patch.object(bridge, "call", wraps=bridge.call) as calls:
            is_error, value = asyncio.run(m33_tool({"target_ref": refs["inserter"], "dir": 12}))
        actual = m33_fixture(
            "rcon.print('m33:'..helpers.table_to_json({direction=b.inserter.direction}))"
        )
        record(
            "M33 unknown argument rejects before bridge and leaves entity unchanged",
            is_error
            and value["error"] == "invalid_input"
            and value["detail"]["errors"][0]["field"] == "dir"
            and value["detail"]["errors"][0]["type"] == "unexpected_argument"
            and calls.call_count == 0
            and actual["direction"] == 0,
            json.dumps(value),
        )
    finally:
        restored = m33_fixture(
            "if b then assert(c.crafting_queue_size==0);"
            "assert(c.teleport(b.position,b.surface));c.selected=b.selected;"
            "if b.stashed then inv.clear() end;"
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            "for i=1,#b.inventory do assert(not b.inventory[i].valid_for_read) end;"
            "b.inventory.destroy();state.last_observation=b.last_observation;"
            "state.recent_observations=b.recent_observations;"
            "for ref,entry in pairs(state.targets) do if entry.entity and entry.entity.valid "
            "and entry.entity.surface==s then if entry.unit_number then "
            "state.owned_refs[entry.unit_number]=nil end;state.targets[ref]=nil end end;"
            "if s then assert(game.delete_surface(s)) end;state.explored_map[name]=nil;"
            "storage.m33_backup=nil;assert(c.crafting_queue_size==0);"
            "assert(c.position.x==b.position.x and c.position.y==b.position.y "
            "and c.surface==b.surface) end;"
            "rcon.print('m33:'..helpers.table_to_json({restored=true}))"
        )
        observed_map_cells.pop(m33_surface, None)
        record(
            "M33 removes fixtures and restores exact inventory, empty queue and position",
            restored.get("restored") is True
            and bridge.call("get_inventory")["result"]["items"] == m33_inventory
            and bridge.call("get_agent_status")["result"]["position"] == m33_position,
        )

    # M34 CD/CE: a fresh path around a newly built assembler, and belt corners.
    # Use a disposable, entirely clear surface so the short detour is always free.
    m34_surface = "m34-fresh-path-corner-fixture"
    m34_server = create_server(bridge)
    m34_inventory = bridge.call("get_inventory")["result"]["items"]
    m34_position = bridge.call("get_agent_status")["result"]["position"]

    def m34_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;local c=state.character;"
            "assert(c and c.valid);local inv=c.get_main_inventory();"
            f'local name="{m34_surface}";local s=game.surfaces[name];'
            "local b=storage.m34_backup;" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
        if "m34:" not in raw:
            raise RuntimeError("M34 fixture failed: " + raw)
        return json.loads(raw.split("m34:", 1)[1].strip())

    async def m34_tool(name, arguments, *, error=None):
        try:
            response = await m34_server.call_tool(name, arguments)
        except ToolError as exc:
            value = json.loads(str(exc))
            is_error = True
        else:
            value = json.loads(response.content[0].text)
            is_error = bool(response.is_error)
        assert is_error == (error is not None), value
        if error:
            assert value["error"] == error, value
        return value

    try:
        m34_fixture(
            "assert(not s and not b,'M34 fixture already exists');"
            # Refuse an active queue before changing anything; preserve it on failure.
            "assert(not state.action and not state.craft_action and c.crafting_queue_size==0);"
            "b={position={x=c.position.x,y=c.position.y},surface=c.surface,"
            "inventory=game.create_inventory(#inv),selected=c.selected,"
            "last_observation=state.last_observation,recent_observations=state.recent_observations};"
            "storage.m34_backup=b;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(b.inventory[i].swap_stack(inv[i])) end end;b.stashed=true;"
            "s=game.create_surface(name,{width=160,height=64});s.peaceful_mode=true;"
            "s.request_to_generate_chunks({0,0},3);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-80,79 do for y=-32,31 do "
            'tiles[#tiles+1]={name="grass-1",position={x,y}} end end;s.set_tiles(tiles);'
            "assert(c.teleport({-29.5,0.5},s));"
            "state.last_observation=nil;state.recent_observations={};"
            "assert(inv.insert{name='transport-belt',count=16}==16);"
            "assert(inv.insert{name='pipe',count=3}==3);rcon.print('m34:{}')"
        )
        # Use 60 tiles to reliably warm the cache: defaults set
        # long_cache_min_cacheable_distance=30, while short paths need 50 algorithm
        # steps. A short clear walk is never cached; even 40 tiles missed the bug.
        # Warm A->B, teleport back to exact A, then put a 3x3 assembler across
        # its middle. Both sides have plenty of room for the short detour.
        goal = {"surface": m34_surface, "x": 30.5, "y": 0.5}
        action("move_to", goal)
        # Identical start/goal, using default arrival (no within) for both walks.
        # The assembler is 30 tiles from A, so create it outside build reach.
        assembler = m34_fixture(
            "assert(c.teleport({-29.5,0.5},s));"
            'assert(s.create_entity{name="assembling-machine-1",position={0.5,0.5},force=c.force});'
            "local e=s.find_entity('assembling-machine-1',{0.5,0.5});assert(e);"
            "rcon.print('m34:'..helpers.table_to_json({position=e.position}))"
        )
        record(
            "M34 CD assembler stands across the warmed route's middle",
            assembler["position"]["x"] == 0.5 and assembler["position"]["y"] == 0.5,
        )
        walked = action("move_to", goal)
        arrived = bridge.call("get_agent_status")["result"]["position"]
        record(
            "M34 CD warmed route avoids newly built assembler and arrives",
            walked["status"] == "succeeded"
            # Match move_to's default successful arrival radius.
            and math.hypot(arrived["x"] - goal["x"], arrived["y"] - goal["y"]) <= 0.5,
            json.dumps({"walked": walked, "position": arrived}),
        )
        m34_fixture(
            "local e=s.find_entity('assembling-machine-1',{0.5,0.5});assert(e);e.destroy();"
            "assert(c.teleport({0.5,5.5},s));rcon.print('m34:{}')"
        )
        line = {
            "item": "transport-belt",
            "surface": m34_surface,
            "from_x": 2.5,
            "from_y": 2.5,
            "to_x": 0.5,
            "to_y": 2.5,
            "end_direction": 0,
        }
        corner = asyncio.run(m34_tool("place_line", line))
        directions = m34_fixture(
            "local ds={};for _,x in ipairs({2.5,1.5,0.5}) do "
            "local e=s.find_entity('transport-belt',{x,2.5});assert(e);ds[#ds+1]=e.direction end;"
            "rcon.print('m34:'..helpers.table_to_json(ds))"
        )
        record(
            "M34 CE westward line turns only its last belt north",
            corner["placed"] == 3
            and corner["complete"]
            and corner["direction"] == 12
            and corner["end_direction"] == 0
            and directions == [12, 12, 0],
            json.dumps(corner),
        )
        north = asyncio.run(
            m34_tool(
                "place_line",
                {
                    "item": "transport-belt",
                    "surface": m34_surface,
                    "from_x": 0.5,
                    "from_y": 1.5,
                    "to_x": 0.5,
                    "to_y": -0.5,
                },
            )
        )
        connected = m34_fixture(
            "local corner=s.find_entity('transport-belt',{0.5,2.5});assert(corner);"
            "local ds={};for _,y in ipairs({1.5,0.5,-0.5}) do "
            "local e=s.find_entity('transport-belt',{0.5,y});assert(e);ds[#ds+1]=e.direction end;"
            "rcon.print('m34:'..helpers.table_to_json({corner=corner.direction,north=ds}))"
        )
        record(
            "M34 CE northward continuation starts at corner output without rotation",
            north["placed"] == 3
            and north["complete"]
            and connected == {"corner": 0, "north": [0, 0, 0]},
        )
        repeated = asyncio.run(m34_tool("place_line", line))
        preview = asyncio.run(m34_tool("check_line", line))
        record(
            "M34 CE repeat and matching preview count all three belts already present",
            repeated["already_present"] == preview["already_present"] == 3
            and repeated["placed"] == preview["clear"] == 0
            and repeated["complete"]
            and preview["complete"]
            and repeated["end_direction"] == preview["end_direction"] == 0
            and repeated["direction"] == preview["direction"] == 12,
        )
        mismatched = {**line, "end_direction": 8}
        preview = asyncio.run(m34_tool("check_line", mismatched))
        stopped = asyncio.run(m34_tool("place_line", mismatched))
        record(
            "M34 CE different final direction reports the corner's existing direction",
            preview["already_present"] == stopped["already_present"] == 2
            and preview["direction"] == stopped["direction"] == 12
            and preview["end_direction"] == stopped["end_direction"] == 8
            and preview["problem_total"] == 1
            and preview["problems"]
            == [
                {
                    "x": 0.5,
                    "y": 2.5,
                    "reason": "different_direction",
                    "existing_direction": 0,
                }
            ]
            and stopped["stopped"] == "blocked"
            and stopped["detail"]["reason"] == "different_direction"
            and stopped["detail"]["existing_direction"] == 0,
        )
        before_rejections = items()
        for tool in ("place_line", "check_line"):
            opposite = {**line, "from_y": 8.5, "to_y": 8.5, "end_direction": 4}
            value = asyncio.run(m34_tool(tool, opposite, error="invalid_input"))
            record(
                f"M34 CE {tool} rejects opposite end_direction in Python",
                any(e["field"] == "end_direction" for e in value["detail"]["errors"]),
            )
            # The Mod also defends direct callers, before any tile is placed.
            expect_error(
                f"M34 CE direct {tool} rejects opposite", tool, opposite, {"invalid_input"}
            )
            pipe = {**opposite, "item": "pipe", "end_direction": 0}
            value = asyncio.run(m34_tool(tool, pipe, error="invalid_input"))
            record(
                f"M34 CE {tool} rejects pipe end_direction in Mod",
                value["detail"]["reason"] == "end_direction_requires_transport_belt",
            )
        remaining = m34_fixture(
            "rcon.print('m34:'..helpers.table_to_json({"
            "belts=s.count_entities_filtered{type='transport-belt'},"
            "pipes=s.count_entities_filtered{type='pipe'},"
            "assemblers=s.count_entities_filtered{type='assembling-machine'}}))"
        )
        record(
            "M34 CE rejected calls place nothing and consume nothing; CD assembler removed",
            items() == before_rejections and remaining == {"belts": 6, "pipes": 0, "assemblers": 0},
        )
    finally:
        # Cancel only actions started after the backup. No fixture crafts, and
        # setup requires an empty queue, so queue restoration is exact.
        for slot in ("action", "craft_action"):
            pending = m34_fixture(
                f"rcon.print('m34:'..helpers.table_to_json({{id=b and state.{slot} "
                f"and state.{slot}.id}}))"
            )
            if pending.get("id"):
                bridge.call("cancel_action", {"action_id": pending["id"]})
        restored = m34_fixture(
            "if b then assert(c.crafting_queue_size==0);"
            "assert(c.teleport(b.position,b.surface));c.selected=b.selected;"
            "if b.stashed then inv.clear() end;"
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            "for i=1,#b.inventory do assert(not b.inventory[i].valid_for_read) end;"
            "b.inventory.destroy();state.last_observation=b.last_observation;"
            "state.recent_observations=b.recent_observations;"
            "for ref,entry in pairs(state.targets) do if entry.entity and entry.entity.valid "
            "and entry.entity.surface==s then if entry.unit_number then "
            "state.owned_refs[entry.unit_number]=nil end;state.targets[ref]=nil end end;"
            "if s then assert(game.delete_surface(s)) end;state.explored_map[name]=nil;"
            "storage.m34_backup=nil;assert(c.crafting_queue_size==0);"
            "assert(c.position.x==b.position.x and c.position.y==b.position.y "
            "and c.surface==b.surface) end;"
            "rcon.print('m34:'..helpers.table_to_json({restored=true}))"
        )
        observed_map_cells.pop(m34_surface, None)
        record(
            "M34 removes fixtures and restores exact inventory, empty queue and position",
            restored.get("restored") is True
            and bridge.call("get_inventory")["result"]["items"] == m34_inventory
            and bridge.call("get_agent_status")["result"]["position"] == m34_position,
        )

    # M35 CF/CG/CH: lane contents, real inserter drops, line continuation and
    # three-second full-output waits. All spawned items/entities stay on this surface.
    m35_surface = "m35-lanes-line-reach-fixture"
    m35_server = create_server(bridge)
    m35_inventory = bridge.call("get_inventory")["result"]["items"]
    m35_position = bridge.call("get_agent_status")["result"]["position"]

    def m35_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;local c=state.character;"
            "assert(c and c.valid);local inv=c.get_main_inventory();"
            f'local name="{m35_surface}";local s=game.surfaces[name];'
            "local b=storage.m35_backup;" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
        if "m35:" not in raw:
            raise RuntimeError("M35 fixture failed: " + raw)
        return json.loads(raw.split("m35:", 1)[1].strip())

    async def m35_tool(name, arguments):
        response = await m35_server.call_tool(name, arguments)
        value = json.loads(response.content[0].text)
        assert not response.is_error, value
        return value

    def m35_read(target_ref):
        return asyncio.run(m35_tool("get_entity_inventory", {"target_ref": target_ref}))

    def m35_ref(name, x, y):
        observed = bridge.call("observe_nearby", {"names": [name]})["result"]["entities"]
        return next(
            e["target_ref"] for e in observed if abs(e["x"] - x) < 0.01 and abs(e["y"] - y) < 0.01
        )

    try:
        m35_fixture(
            "assert(not s and not b,'M35 fixture already exists');"
            "assert(not state.action and not state.craft_action and c.crafting_queue_size==0);"
            "b={position={x=c.position.x,y=c.position.y},surface=c.surface,"
            "inventory=game.create_inventory(#inv),selected=c.selected,"
            "last_observation=state.last_observation,recent_observations=state.recent_observations};"
            "storage.m35_backup=b;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(b.inventory[i].swap_stack(inv[i])) end end;b.stashed=true;"
            "s=game.create_surface(name,{width=160,height=64});s.peaceful_mode=true;"
            "s.request_to_generate_chunks({0,0},3);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-80,79 do for y=-32,31 do "
            'tiles[#tiles+1]={name="grass-1",position={x,y}} end end;s.set_tiles(tiles);'
            "assert(c.teleport({0.5,0.5},s));"
            "state.last_observation=nil;state.recent_observations={};"
            "assert(inv.insert{name='transport-belt',count=64}==64);"
            "assert(inv.insert{name='burner-inserter',count=4}==4);"
            'b.north=s.create_entity{name="transport-belt",position={-3.5,-2.5},'
            "direction=0,force=c.force};assert(b.north);"
            'b.east=s.create_entity{name="transport-belt",position={3.5,-2.5},'
            "direction=4,force=c.force};assert(b.east);"
            'b.underground=s.create_entity{name="underground-belt",position={0.5,-4.5},'
            "direction=4,type='input',force=c.force};assert(b.underground);"
            "for _,e in ipairs({b.north,b.east}) do "
            "assert(e.get_transport_line(defines.transport_line.left_line).insert_at_back"
            "{name='coal',count=1});"
            "assert(e.get_transport_line(defines.transport_line.right_line).insert_at_back"
            "{name='iron-ore',count=1}) end;"
            # Check the lane constants against world coordinates as well.
            "local n=b.north.get_line_item_position(defines.transport_line.left_line,0.5);"
            "local e=b.east.get_line_item_position(defines.transport_line.left_line,0.5);"
            "assert(n.x<b.north.position.x and e.y<b.east.position.y);"
            "rcon.print('m35:{}')"
        )
        belt_refs = [m35_ref("transport-belt", x, -2.5) for x in (-3.5, 3.5)]
        underground_ref = m35_ref("underground-belt", 0.5, -4.5)
        batch = asyncio.run(
            m35_tool("get_entity_inventories", {"target_refs": [*belt_refs, underground_ref]})
        )
        expected_lanes = {
            "left": [{"name": "coal", "quality": "normal", "count": 1}],
            "right": [{"name": "iron-ore", "quality": "normal", "count": 1}],
        }
        for label, ref, entry in zip(("north", "east"), belt_refs, batch["results"], strict=False):
            single = m35_read(ref)
            record(
                f"M35 CF {label}-facing belt reports left/right lane contents",
                entry["ok"]
                and entry["result"]["belt_lanes"] == single["belt_lanes"] == expected_lanes
                and single["belt_items"] == expected_lanes["left"] + expected_lanes["right"],
                json.dumps(single),
            )
        underground = batch["results"][2]
        record(
            "M35 CF empty underground lanes are lists",
            underground["ok"] and underground["result"]["belt_lanes"] == {"left": [], "right": []},
        )
        observed = bridge.call("observe_nearby", {"names": ["transport-belt"]})["result"]
        record(
            "M35 CF observe_nearby keeps only merged belt_items",
            all("belt_lanes" not in e and "drop_lane" not in e for e in observed["entities"]),
        )

        m35_fixture(
            "assert(c.teleport({3.5,5.5},s));"
            'b.drop_belt=s.create_entity{name="transport-belt",position={0.5,5.5},'
            "direction=4,force=c.force};assert(b.drop_belt);rcon.print('m35:{}')"
        )
        placement = {"item": "burner-inserter", "surface": m35_surface, "x": 0.5}
        north = asyncio.run(m35_tool("place_entity", {**placement, "y": 4.5, "direction": 0}))
        south_batch = asyncio.run(
            m35_tool("place_entities", {"items": [{**placement, "y": 6.5, "direction": 8}]})
        )
        assert south_batch["results"][0]["ok"], south_batch
        south = south_batch["results"][0]["result"]
        record(
            "M35 CF opposite-side inserters report opposite far lanes",
            north.get("drop_lane") == "right"
            and south.get("drop_lane") == "left"
            and m35_read(north["target_ref"]).get("drop_lane") == north.get("drop_lane")
            and m35_read(south["target_ref"]).get("drop_lane") == south.get("drop_lane"),
            json.dumps({"north": north, "south": south}),
        )
        # Reverse the build order too: neither unfuelled inserter has a belt yet.
        later_placement = {**placement, "x": 4.5}
        later_north = asyncio.run(
            m35_tool("place_entity", {**later_placement, "y": 4.5, "direction": 0})
        )
        later_south_batch = asyncio.run(
            m35_tool("place_entities", {"items": [{**later_placement, "y": 6.5, "direction": 8}]})
        )
        assert later_south_batch["results"][0]["ok"], later_south_batch
        later_south = later_south_batch["results"][0]["result"]
        before_belt = [m35_read(e["target_ref"]) for e in (later_north, later_south)]
        asyncio.run(
            m35_tool(
                "place_entity",
                {
                    "item": "transport-belt",
                    "surface": m35_surface,
                    "x": 4.5,
                    "y": 5.5,
                    "direction": 4,
                },
            )
        )
        after_belt = [m35_read(e["target_ref"]) for e in (later_north, later_south)]
        after_belt_batch = asyncio.run(
            m35_tool(
                "get_entity_inventories",
                {"target_refs": [later_north["target_ref"], later_south["target_ref"]]},
            )
        )
        record(
            "M35 CF belt placed after unfuelled inserters updates inventory drop lanes",
            all("drop_lane" not in e for e in (later_north, later_south, *before_belt))
            and [e.get("drop_lane") for e in after_belt] == ["right", "left"]
            and all(e["ok"] for e in after_belt_batch["results"])
            and [e["result"].get("drop_lane") for e in after_belt_batch["results"]]
            == ["right", "left"],
            json.dumps({"before": before_belt, "after": after_belt, "batch": after_belt_batch}),
        )
        m35_fixture(
            "b.inserters={s.find_entity('burner-inserter',{0.5,4.5}),"
            "s.find_entity('burner-inserter',{0.5,6.5})};"
            "for i,e in ipairs(b.inserters) do assert(e);"
            "local chest=s.create_entity{name='wooden-chest',position=e.pickup_position,"
            "force=c.force};assert(chest);assert(chest.insert"
            "{name=i==1 and 'iron-plate' or 'copper-plate',count=1}==1);"
            "assert(e.get_fuel_inventory().insert{name='coal',count=1}==1) end;"
            "rcon.print('m35:{}')"
        )
        drop_ref = m35_ref("transport-belt", 0.5, 5.5)
        deadline = time.monotonic() + 8
        while True:
            dropped = m35_read(drop_ref)
            landed = all(
                any(stack["name"] == item for stack in dropped["belt_lanes"].get(lane, []))
                for lane, item in (
                    (north.get("drop_lane"), "iron-plate"),
                    (south.get("drop_lane"), "copper-plate"),
                )
            )
            if landed or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        record(
            "M35 CF real burner inserter items land on the reported lanes",
            landed,
            json.dumps(dropped),
        )

        # Teleport only to position this independent fixture; continuation uses walking.
        m35_fixture("assert(c.teleport({-15.5,12.5},s));rcon.print('m35:{}')")
        line_args = {
            "item": "transport-belt",
            "surface": m35_surface,
            "from_x": -14.5,
            "from_y": 10.5,
            "to_x": 10.5,
            "to_y": 10.5,
            "end_direction": 0,
        }
        first = asyncio.run(m35_tool("place_line", line_args))
        record(
            "M35 CG long line stops out_of_range with a standing position",
            first.get("stopped") == "out_of_range" and bool(first.get("stand_position")),
            json.dumps(first),
        )
        assert first.get("stand_position"), first
        stand = first["stand_position"]
        safe = m35_fixture(
            f"local p={{x={stand['x']},y={stand['y']}}};"
            "rcon.print('m35:'..helpers.table_to_json({fits=s.can_place_entity"
            "{name=c.name,position=p,force=c.force}}))"
        )
        record(
            "M35 CG suggested position fits the character off the line",
            safe["fits"] and abs(stand["y"] - 10.5) > 1.5,
        )
        walked = action("move_to", {"surface": m35_surface, **stand})
        second = asyncio.run(
            m35_tool(
                "place_line",
                {
                    **line_args,
                    "from_x": first["next_position"]["x"],
                    "from_y": first["next_position"]["y"],
                },
            )
        )
        record(
            "M35 CG walk to stand_position allows the continuation to complete or place more",
            walked["status"] == "succeeded"
            and (second["complete"] or second["placed"] > first["placed"]),
            json.dumps(second),
        )

        m35_fixture(
            "assert(c.teleport({20.5,4.5},s));"
            "b.machine=s.create_entity{name='assembling-machine-1',position={20.5,0.5},"
            "force=c.force};assert(b.machine);b.machine.set_recipe('iron-gear-wheel');"
            "local generator=s.create_entity{name='electric-energy-interface',"
            "position={24.5,2.5},force=c.force};assert(generator);"
            "generator.power_production=1000000/60;generator.power_usage=0;"
            "generator.electric_buffer_size=1000000;generator.energy=1000000;"
            "assert(s.create_entity{name='small-electric-pole',position={22.5,2.5},force=c.force});"
            "assert(b.machine.get_output_inventory().insert{name='iron-gear-wheel',count=100}==100);"
            "assert(b.machine.get_inventory(defines.inventory.assembling_machine_input).insert"
            "{name='iron-plate',count=20}==20);rcon.print('m35:{}')"
        )
        machine_ref = m35_ref("assembling-machine-1", 20.5, 0.5)
        deadline = time.monotonic() + 5
        while True:
            full = m35_read(machine_ref)
            if full.get("status") == "full_output" or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        assert full.get("status") == "full_output", full
        current = sum(stack["count"] for stack in full["inventories"]["result"])
        started = time.monotonic()
        waited = asyncio.run(
            m35_tool(
                "wait_for_items",
                {
                    "target_ref": machine_ref,
                    "item": "iron-gear-wheel",
                    "count": current + 1,
                    "inventory_role": "result",
                    "timeout_seconds": 20,
                },
            )
        )
        elapsed = time.monotonic() - started
        record(
            "M35 CH full_output returns after 3 seconds with output_full and guidance",
            3 <= elapsed < 6
            and 3 <= waited["waited_seconds"] < 5
            and 3 <= waited["stalled_seconds"] < 5
            and waited.get("output_full") is True
            and waited["stalled"]
            and not waited["reached"]
            and not waited["timed_out"]
            and waited["current"] == current
            and waited["status"] == "full_output"
            and waited.get("feeders") == []
            and all(
                t in waited["hint"] for t in ("current count", "only after", "output inserter")
            ),
            json.dumps({"elapsed": elapsed, "result": waited}),
        )
    finally:
        for slot in ("action", "craft_action"):
            pending = m35_fixture(
                f"rcon.print('m35:'..helpers.table_to_json({{id=b and state.{slot} "
                f"and state.{slot}.id}}))"
            )
            if pending.get("id"):
                bridge.call("cancel_action", {"action_id": pending["id"]})
        restored = m35_fixture(
            "if b then assert(c.crafting_queue_size==0);"
            "assert(c.teleport(b.position,b.surface));c.selected=b.selected;"
            "if b.stashed then inv.clear() end;"
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            "for i=1,#b.inventory do assert(not b.inventory[i].valid_for_read) end;"
            "b.inventory.destroy();state.last_observation=b.last_observation;"
            "state.recent_observations=b.recent_observations;"
            "for ref,entry in pairs(state.targets) do if entry.entity and entry.entity.valid "
            "and entry.entity.surface==s then if entry.unit_number then "
            "state.owned_refs[entry.unit_number]=nil end;state.targets[ref]=nil end end;"
            "if s then assert(game.delete_surface(s)) end;state.explored_map[name]=nil;"
            "storage.m35_backup=nil;assert(c.crafting_queue_size==0);"
            "assert(c.position.x==b.position.x and c.position.y==b.position.y "
            "and c.surface==b.surface) end;"
            "rcon.print('m35:'..helpers.table_to_json({restored=true}))"
        )
        observed_map_cells.pop(m35_surface, None)
        record(
            "M35 removes fixtures and restores inventory, queue, position and observation",
            restored.get("restored") is True
            and bridge.call("get_inventory")["result"]["items"] == m35_inventory
            and bridge.call("get_agent_status")["result"]["position"] == m35_position,
        )

    # M36 CI/CJ: shortage hidden by full_output, and lines placed while walking.
    # Fixture-only RCON creates/stashes items and teleports to each test's start.
    m36_surface = "m36-status-walk-line-fixture"
    m36_server = create_server(bridge)
    m36_inventory = bridge.call("get_inventory")["result"]["items"]
    m36_position = bridge.call("get_agent_status")["result"]["position"]

    def m36_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;local c=state.character;"
            "assert(c and c.valid);local inv=c.get_main_inventory();"
            f'local name="{m36_surface}";local s=game.surfaces[name];'
            "local b=storage.m36_backup;" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
        if "m36:" not in raw:
            raise RuntimeError("M36 fixture failed: " + raw)
        return json.loads(raw.split("m36:", 1)[1].strip())

    async def m36_tool(name, arguments):
        response = await m36_server.call_tool(name, arguments)
        value = json.loads(response.content[0].text)
        assert not response.is_error, value
        return value

    def m36_read(target_ref):
        return asyncio.run(m36_tool("get_entity_inventory", {"target_ref": target_ref}))

    async def m36_line(y):
        value = await m36_tool(
            "place_line",
            {
                "item": "transport-belt",
                "surface": m36_surface,
                "from_x": 1.5,
                "from_y": y,
                "to_x": 40.5,
                "to_y": y,
                "end_direction": 0,
                "walk": True,
                "wait_seconds": 50,
            },
        )
        if value.get("status") == "running":
            value = await m36_tool(
                "wait_for_action", {"action_id": value["action_id"], "timeout_seconds": 50}
            )
        assert value.get("status") != "running", value
        assert value.get("type") == "place_line" and value.get("requested") == 40, value
        return value

    try:
        m36_fixture(
            "assert(not s and not b,'M36 fixture already exists');"
            "assert(not state.action and not state.craft_action and c.crafting_queue_size==0);"
            "b={position={x=c.position.x,y=c.position.y},surface=c.surface,"
            "inventory=game.create_inventory(#inv),selected=c.selected,"
            "last_observation=state.last_observation,recent_observations=state.recent_observations};"
            "storage.m36_backup=b;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(b.inventory[i].swap_stack(inv[i])) end end;b.stashed=true;"
            "s=game.create_surface(name,{width=160,height=96});s.peaceful_mode=true;"
            "s.request_to_generate_chunks({0,0},3);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-80,79 do for y=-48,47 do "
            'tiles[#tiles+1]={name="grass-1",position={x,y}} end end;s.set_tiles(tiles);'
            "assert(c.teleport({0.5,4.5},s));"
            "state.last_observation=nil;state.recent_observations={};"
            "b.machine=s.create_entity{name='assembling-machine-1',position={0.5,0.5},"
            "force=c.force};assert(b.machine);b.machine.set_recipe('iron-gear-wheel');"
            "local generator=s.create_entity{name='electric-energy-interface',"
            "position={4.5,2.5},force=c.force};assert(generator);"
            "generator.power_production=1000000/60;generator.power_usage=0;"
            "generator.electric_buffer_size=1000000;generator.energy=1000000;"
            "assert(s.create_entity{name='small-electric-pole',position={2.5,2.5},force=c.force});"
            "assert(b.machine.get_inventory(defines.inventory.crafter_input).insert"
            "{name='iron-plate',count=6}==6);rcon.print('m36:{}')"
        )
        observed = bridge.call("observe_nearby", {"names": ["assembling-machine-1"]})["result"]
        machine_ref = next(e["target_ref"] for e in observed["entities"] if e["x"] == 0.5)
        deadline = time.monotonic() + 15
        while True:
            shortage = m36_read(machine_ref)
            gears = sum(stack["count"] for stack in shortage["inventories"]["result"])
            if (
                gears == 3 and shortage.get("status") == "item_ingredient_shortage"
            ) or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        missing = [{"name": "iron-plate", "count": 2}]
        record(
            "M36 CI three gears from six plates reports shortage of two plates",
            gears == 3
            and shortage.get("status") == "item_ingredient_shortage"
            and shortage.get("missing_ingredients") == missing,
            json.dumps(shortage),
        )
        for compact in (False, True):
            observed = asyncio.run(
                m36_tool("observe_nearby", {"names": ["assembling-machine-1"], "compact": compact})
            )["entities"]
            record(
                f"M36 CI observe_nearby compact={compact} includes shortage details",
                len(observed) == 1
                and observed[0].get("status") == "item_ingredient_shortage"
                and observed[0].get("missing_ingredients") == missing,
                json.dumps(observed),
            )
        waited = asyncio.run(
            m36_tool(
                "wait_for_items",
                {
                    "target_ref": machine_ref,
                    "item": "iron-gear-wheel",
                    "count": 5,
                    "inventory_role": "result",
                    "timeout_seconds": 20,
                },
            )
        )
        record(
            "M36 CI shortage waits ten seconds and stalls without output_full",
            waited.get("status") == "item_ingredient_shortage"
            and waited.get("missing_ingredients") == missing
            and waited["stalled"]
            and not waited.get("output_full")
            and not waited["timed_out"]
            and waited["current"] == 3
            and 10 <= waited["waited_seconds"] < 13,
            json.dumps(waited),
        )
        m36_fixture(
            "b.machine.get_output_inventory().clear();"
            "assert(b.machine.get_output_inventory().insert{name='iron-gear-wheel',count=100}==100);"
            "assert(b.machine.get_inventory(defines.inventory.crafter_input).insert"
            "{name='iron-plate',count=100}==100);rcon.print('m36:{}')"
        )
        deadline = time.monotonic() + 5
        while True:
            full = m36_read(machine_ref)
            if full.get("status") == "full_output" or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        record(
            "M36 CI full stack with ample ingredients stays full_output",
            full.get("status") == "full_output" and "missing_ingredients" not in full,
            json.dumps(full),
        )
        waited = asyncio.run(
            m36_tool(
                "wait_for_items",
                {
                    "target_ref": machine_ref,
                    "item": "iron-gear-wheel",
                    "count": 101,
                    "inventory_role": "result",
                    "timeout_seconds": 20,
                },
            )
        )
        record(
            "M36 CI genuine full output still returns output_full after three seconds",
            waited.get("status") == "full_output"
            and waited.get("output_full") is True
            and waited["stalled"]
            and "missing_ingredients" not in waited
            and 3 <= waited["waited_seconds"] < 6,
            json.dumps(waited),
        )

        m36_fixture(
            "for ref,entry in pairs(state.targets) do if entry.entity and entry.entity.valid "
            "and entry.entity.surface==s then if entry.unit_number then "
            "state.owned_refs[entry.unit_number]=nil end;state.targets[ref]=nil end end;"
            "for _,e in pairs(s.find_entities()) do if e~=c then e.destroy() end end;"
            "assert(c.teleport({0.5,4.5},s));"
            "assert(inv.insert{name='transport-belt',count=100}==100);rcon.print('m36:{}')"
        )
        line = asyncio.run(m36_line(0.5))
        measured = m36_fixture(
            "local correct=0;for i=1,40 do local e=s.find_entity('transport-belt',{i+0.5,0.5});"
            "if e and e.direction==(i==40 and 0 or 4) then correct=correct+1 end end;"
            "rcon.print('m36:'..helpers.table_to_json({correct=correct,"
            "x=c.position.x,y=c.position.y,belts=inv.get_item_count('transport-belt')}))"
        )
        cached_line = asyncio.run(m36_tool("get_action_status", {"action_id": line["action_id"]}))
        record(
            "M36 CJ forty belts complete by walking with correct final corner and action fields",
            line.get("complete") is True
            and line["placed"] == 40
            and line["already_present"] == 0
            and line.get("end_direction") == 0
            and line.get("last_position") == {"x": 40.5, "y": 0.5}
            and cached_line.get("placed") == 40
            and cached_line.get("complete") is True
            and measured["correct"] == 40
            and measured["belts"] == 60
            and (abs(measured["x"] - 0.5) > 1 or abs(measured["y"] - 4.5) > 1),
            json.dumps({"line": line, "measured": measured}),
        )
        m36_fixture(
            "assert(c.teleport({0.5,16.5},s));"
            "assert(s.create_entity{name='wooden-chest',position={24.5,12.5},force=c.force});"
            "rcon.print('m36:{}')"
        )
        blocked = asyncio.run(m36_line(12.5))
        record(
            "M36 CJ walking line stops at an entity midway and preserves progress",
            blocked.get("stopped") == "blocked"
            and blocked.get("next_position") == {"x": 24.5, "y": 12.5}
            and blocked.get("placed") == 23
            and not blocked["complete"],
            json.dumps(blocked),
        )
        m36_fixture(
            "assert(c.teleport({0.5,28.5},s));inv.clear();"
            "assert(inv.insert{name='transport-belt',count=15}==15);rcon.print('m36:{}')"
        )
        short = asyncio.run(m36_line(24.5))
        measured = m36_fixture(
            "rcon.print('m36:'..helpers.table_to_json({belts=inv.get_item_count('transport-belt'),"
            "next_empty=s.find_entity('transport-belt',{16.5,24.5})==nil}))"
        )
        record(
            "M36 CJ walking line exhausts materials without placing free belts",
            short.get("stopped") == "missing_materials"
            and short.get("placed") == 15
            and short.get("next_position") == {"x": 16.5, "y": 24.5}
            and not short["complete"]
            and measured["belts"] == 0
            and measured["next_empty"],
            json.dumps(short),
        )
    finally:
        for slot in ("action", "craft_action"):
            pending = m36_fixture(
                f"rcon.print('m36:'..helpers.table_to_json({{id=b and state.{slot} "
                f"and state.{slot}.id}}))"
            )
            if pending.get("id"):
                bridge.call("cancel_action", {"action_id": pending["id"]})
        restored = m36_fixture(
            "if b then assert(c.crafting_queue_size==0);"
            "assert(c.teleport(b.position,b.surface));c.selected=b.selected;"
            "if b.stashed then inv.clear() end;"
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            "for i=1,#b.inventory do assert(not b.inventory[i].valid_for_read) end;"
            "b.inventory.destroy();state.last_observation=b.last_observation;"
            "state.recent_observations=b.recent_observations;"
            "for ref,entry in pairs(state.targets) do if entry.entity and entry.entity.valid "
            "and entry.entity.surface==s then if entry.unit_number then "
            "state.owned_refs[entry.unit_number]=nil end;state.targets[ref]=nil end end;"
            "if s then assert(game.delete_surface(s)) end;state.explored_map[name]=nil;"
            "storage.m36_backup=nil;assert(c.crafting_queue_size==0);"
            "assert(c.position.x==b.position.x and c.position.y==b.position.y "
            "and c.surface==b.surface) end;"
            "rcon.print('m36:'..helpers.table_to_json({restored=true}))"
        )
        observed_map_cells.pop(m36_surface, None)
        record(
            "M36 removes fixtures and restores inventory, queue, position and observation",
            restored.get("restored") is True
            and bridge.call("get_inventory")["result"]["items"] == m36_inventory
            and bridge.call("get_agent_status")["result"]["position"] == m36_position,
        )

    # M37 CK/CL/CM: all creation, item supply and teleports are fixture-only RCON.
    m37_surface = "m37-belt-batch-fluid-fixture"
    m37_server = create_server(bridge)
    m37_inventory = bridge.call("get_inventory")["result"]["items"]
    m37_position = bridge.call("get_agent_status")["result"]["position"]

    def m37_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;local c=state.character;"
            "assert(c and c.valid);local inv=c.get_main_inventory();"
            f'local name="{m37_surface}";local s=game.surfaces[name];'
            "local b=storage.m37_backup;" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
        if "m37:" not in raw:
            raise RuntimeError("M37 fixture failed: " + raw)
        return json.loads(raw.split("m37:", 1)[1].strip())

    async def m37_tool(name, args):
        response = await m37_server.call_tool(name, args)
        value = json.loads(response.content[0].text)
        assert not response.is_error, value
        return value

    def m37_action(name, args):
        value = asyncio.run(m37_tool(name, {**args, "wait_seconds": 20}))
        if value.get("status") == "running":
            value = asyncio.run(
                m37_tool(
                    "wait_for_action", {"action_id": value["action_id"], "timeout_seconds": 50}
                )
            )
        assert value.get("status") != "running", value
        return value

    def m37_refs(names):
        entities = bridge.call("observe_nearby", {"names": names})["result"]["entities"]
        return {(e["name"], e["x"], e["y"]): e["target_ref"] for e in entities}

    try:
        m37_fixture(
            "assert(not s and not b,'M37 fixture already exists');"
            "assert(not state.action and not state.craft_action and c.crafting_queue_size==0);"
            "b={position={x=c.position.x,y=c.position.y},surface=c.surface,selected=c.selected,"
            "inventory=game.create_inventory(#inv),saved={},fields={'targets','owned_refs',"
            "'recipe_baselines','last_observation','recent_observations','explored_map',"
            "'last_action','completed_actions','completed_order','processed','processed_order',"
            "'next_target_id','next_action_id'}};storage.m37_backup=b;"
            "for _,key in ipairs(b.fields) do b.saved[key]=state[key] end;"
            "for _,key in ipairs({'targets','owned_refs','recipe_baselines','recent_observations',"
            "'explored_map','completed_actions','completed_order','processed','processed_order'}) "
            "do state[key]={} end;state.last_observation=nil;state.last_action=nil;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(b.inventory[i].swap_stack(inv[i])) end end;b.stashed=true;"
            "s=game.create_surface(name,{width=96,height=64});s.peaceful_mode=true;"
            "s.request_to_generate_chunks({0,0},2);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-48,47 do for y=-32,31 do "
            "tiles[#tiles+1]={name='grass-1',position={x,y}} end end;s.set_tiles(tiles);"
            "assert(c.teleport({8.5,4.5},s));"
            "local belt=s.create_entity{name='transport-belt',position={8.5,0.5},force='neutral'};"
            "assert(belt);belt.active=false;rcon.print('m37:{}')"
        )
        moved = m37_action("move_to", {"surface": m37_surface, "x": 8.5, "y": 0.5})
        record(
            "M37 CK belt-centre goal adjusts and finishes off belts",
            moved["status"] == "succeeded"
            and moved.get("original_goal") == {"x": 8.5, "y": 0.5}
            and moved.get("goal_adjusted")
            and not moved.get("standing_on_belt"),
            json.dumps(moved),
        )
        m37_fixture("assert(c.teleport({8.5,0.5},s));rcon.print('m37:{}')")
        within = m37_action("move_to", {"surface": m37_surface, "x": 8.5, "y": 0.5, "within": 2})
        record(
            "M37 CK within arrival walks off the belt while staying inside the radius",
            within["status"] == "succeeded"
            and within["detail"]["distance"] <= 2
            and within["detail"]["within"] == 2
            and not within.get("standing_on_belt"),
            json.dumps(within),
        )
        m37_fixture(
            "for x=16,26 do for y=-5,5 do local e=s.create_entity{name='transport-belt',"
            "position={x+0.5,y+0.5},force=c.force};assert(e);e.active=false end end;"
            "assert(c.teleport({21.5,4.5},s));rcon.print('m37:{}')"
        )
        fallback = m37_action("move_to", {"surface": m37_surface, "x": 21.5, "y": 0.5})
        record(
            "M37 CK wide belt field retains the belt warning when no safe goal exists",
            fallback["status"] == "succeeded"
            and not fallback.get("goal_adjusted")
            and fallback.get("standing_on_belt") is True
            and "belt" in fallback.get("hint", ""),
            json.dumps(fallback),
        )
        m37_fixture(
            "for _,e in pairs(s.find_entities()) do if e~=c then e.destroy() end end;"
            "assert(c.teleport({0.5,4.5},s));inv.clear();"
            "for x=-2,2 do for y=0,1 do assert(s.create_entity{name='transport-belt',"
            "position={x+0.5,y+0.5},force=c.force}) end end;rcon.print('m37:{}')"
        )
        refs = m37_refs(["transport-belt"])
        ordered = [refs[("transport-belt", x + 0.5, y + 0.5)] for x in range(-2, 3) for y in (0, 1)]
        mined = m37_action("mine_entities", {"target_refs": ordered})
        measured = m37_fixture(
            "rcon.print('m37:'..helpers.table_to_json({belts=inv.get_item_count('transport-belt'),"
            "remaining=s.count_entities_filtered{name='transport-belt'}}))"
        )
        record(
            "M37 CL ten belts mined in one action return to inventory",
            mined["status"] == "succeeded"
            and mined["mined"] == mined["requested"] == 10
            and sum(i["count"] for i in mined["gained"] if i["name"] == "transport-belt") == 10
            and measured == {"belts": 10, "remaining": 0},
            json.dumps({"action": mined, "measured": measured}),
        )
        m37_fixture(
            "for _,p in ipairs({{0.5,0.5},{14.5,0.5},{2.5,0.5}}) do "
            "assert(s.create_entity{name='transport-belt',position=p,force=c.force}) end;"
            "assert(s.create_entity{name='iron-ore',position={-2.5,2.5},amount=100});"
            "rcon.print('m37:{}')"
        )
        refs = m37_refs(["transport-belt", "iron-ore"])
        ordered = [refs[("transport-belt", x, 0.5)] for x in (0.5, 14.5, 2.5)]
        partial = m37_action("mine_entities", {"target_refs": ordered})
        measured = m37_fixture(
            "rcon.print('m37:'..helpers.table_to_json({first_gone="
            "s.find_entity('transport-belt',{0.5,0.5})==nil,middle_present="
            "s.find_entity('transport-belt',{14.5,0.5})~=nil,last_present="
            "s.find_entity('transport-belt',{2.5,0.5})~=nil,"
            "belts=inv.get_item_count('transport-belt')}))"
        )
        record(
            "M37 CL first unreachable target fails the batch and leaves later targets untouched",
            partial["status"] == "failed"
            and partial["mined"] == 1
            and partial["requested"] == 3
            and partial["stopped"]["index"] == 2
            and partial["stopped"]["target_ref"] == ordered[1]
            and partial["stopped"]["reason"] == "out_of_range"
            and measured
            == {"first_gone": True, "middle_present": True, "last_present": True, "belts": 11},
            json.dumps({"action": partial, "measured": measured}),
        )
        response = asyncio.run(
            m37_server.call_tool(
                "mine_entities", {"target_refs": [ordered[2], refs[("iron-ore", -2.5, 2.5)]]}
            )
        )
        rejected = json.loads(response.content[0].text)
        record(
            "M37 CL resource validation rejects the entire batch up front with its index",
            response.is_error
            and rejected.get("error") == "invalid_input"
            and rejected["detail"]["errors"][0]["index"] == 2
            and m37_fixture(
                "rcon.print('m37:'..helpers.table_to_json({present="
                "s.find_entity('transport-belt',{2.5,0.5})~=nil}))"
            )["present"],
            json.dumps(rejected),
        )
        # Geometry comes from the 2.0.77 base prototypes: boiler's west water port,
        # north steam port, and engine's south port. No fluids are inserted by script.
        m37_fixture(
            "for _,e in pairs(s.find_entities()) do if e~=c then e.destroy() end end;"
            "local tiles={};for x=-6,-4 do for y=-1,1 do "
            "tiles[#tiles+1]={name='water',position={x,y}} end end;s.set_tiles(tiles);"
            "assert(c.teleport({3.5,-0.5},s));"
            "b.boiler=s.create_entity{name='boiler',position={0.5,0},direction=0,force=c.force};"
            "assert(b.boiler);assert(b.boiler.get_fuel_inventory().insert{name='coal',count=20}==20);"
            "b.engine=s.create_entity{name='steam-engine',position={0.5,-4.5},force=c.force};"
            "assert(b.engine);"
            "assert(s.create_entity{name='pipe',position={0.5,-1.5},force=c.force});"
            "assert(s.create_entity{name='pipe',position={-1.5,0.5},force=c.force});"
            "assert(s.create_entity{name='offshore-pump',position={-2.5,0.5},"
            "direction=12,force=c.force});"
            "assert(s.create_entity{name='small-electric-pole',position={2.5,-3.5},force=c.force});"
            "local load=s.create_entity{name='electric-energy-interface',position={4.5,-4.5},"
            "force=c.force};assert(load);load.power_production=0;load.power_usage=300000/60;"
            "load.electric_buffer_size=1000000;load.energy=0;"
            "b.dry=s.create_entity{name='steam-engine',position={7.5,0.5},force=c.force};"
            "assert(b.dry);rcon.print('m37:{}')"
        )
        refs = m37_refs(["boiler", "steam-engine", "pipe", "offshore-pump"])
        diagnostic_refs = [
            refs[(name, x, y)]
            for name, x, y in (
                ("boiler", 0.5, 0),
                ("steam-engine", 0.5, -4.5),
                ("pipe", -1.5, 0.5),
                ("offshore-pump", -2.5, 0.5),
                ("steam-engine", 7.5, 0.5),
            )
        ]

        def has_fluid(entity, name):
            return any(
                f.get("name") == name
                and f["amount"] > 0
                and f["capacity"] > 0
                and "temperature" in f
                for f in entity.get("fluids", [])
            )

        deadline = time.monotonic() + 20
        while True:
            diagnostic = asyncio.run(
                m37_tool("get_entity_inventories", {"target_refs": diagnostic_refs})
            )
            entries = diagnostic["results"]
            diagnostic_ready = False
            if all(entry["ok"] for entry in entries):
                boiler, engine, pipe, pump, dry = [entry["result"] for entry in entries]
                diagnostic_ready = bool(
                    has_fluid(boiler, "water")
                    and has_fluid(boiler, "steam")
                    and boiler["inventories"]["fuel"]
                    and has_fluid(engine, "steam")
                    and engine.get("power_output_kw", 0) > 0
                    and has_fluid(pipe, "water")
                    and has_fluid(pump, "water")
                )
            if diagnostic_ready or time.monotonic() >= deadline:
                break
            time.sleep(0.1)

        record(
            "M37 CM real water/steam/fuel and last-tick generator output are readable in batch",
            diagnostic_ready,
            json.dumps(diagnostic),
        )
        dry_single = asyncio.run(
            m37_tool("get_entity_inventory", {"target_ref": diagnostic_refs[-1]})
        )
        record(
            "M37 CM no-steam generator reports zero output and empty fluidbox",
            dry_single.get("power_output_kw") == 0
            and dry_single.get("inventories") == {}
            and dry_single.get("fluids")
            and all(
                f["amount"] == 0 and "name" not in f and f["capacity"] > 0
                for f in dry_single["fluids"]
            ),
            json.dumps(dry_single),
        )
    finally:
        for slot in ("action", "craft_action"):
            pending = m37_fixture(
                f"rcon.print('m37:'..helpers.table_to_json({{id=b and state.{slot} "
                f"and state.{slot}.id}}))"
            )
            if pending.get("id"):
                bridge.call("cancel_action", {"action_id": pending["id"]})
        restored = m37_fixture(
            "if b then assert(c.crafting_queue_size==0);"
            "assert(c.teleport(b.position,b.surface));c.selected=b.selected;"
            "if b.stashed then inv.clear() end;"
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            "for i=1,#b.inventory do assert(not b.inventory[i].valid_for_read) end;"
            "b.inventory.destroy();if s then assert(game.delete_surface(s)) end;"
            "for _,key in ipairs(b.fields) do state[key]=b.saved[key] end;"
            "storage.m37_backup=nil;assert(c.crafting_queue_size==0);"
            "assert(c.position.x==b.position.x and c.position.y==b.position.y "
            "and c.surface==b.surface) end;"
            "rcon.print('m37:'..helpers.table_to_json({restored=true}))"
        )
        observed_map_cells.pop(m37_surface, None)
        record(
            "M37 removes fixtures and restores inventory, queue, position and observation state",
            restored.get("restored") is True
            and bridge.call("get_inventory")["result"]["items"] == m37_inventory
            and bridge.call("get_agent_status")["result"]["position"] == m37_position,
        )

    # M38 CN/CO/CP: all creation, item supply and teleports are fixture-only RCON.
    m38_surface = "m38-targets-pump-walk-fixture"
    m38_server = create_server(bridge)
    m38_inventory = bridge.call("get_inventory")["result"]["items"]
    m38_position = bridge.call("get_agent_status")["result"]["position"]

    def m38_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;local c=state.character;"
            "assert(c and c.valid);local inv=c.get_main_inventory();"
            f'local name="{m38_surface}";local s=game.surfaces[name];'
            "local b=storage.m38_backup;" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
        if "m38:" not in raw:
            raise RuntimeError("M38 fixture failed: " + raw)
        return json.loads(raw.split("m38:", 1)[1].strip())

    async def m38_tool(name, args):
        response = await m38_server.call_tool(name, args)
        value = json.loads(response.content[0].text)
        assert not response.is_error, value
        return value

    def m38_action(name, args):
        value = asyncio.run(m38_tool(name, {**args, "wait_seconds": 20}))
        if value.get("status") == "running":
            value = asyncio.run(
                m38_tool(
                    "wait_for_action", {"action_id": value["action_id"], "timeout_seconds": 50}
                )
            )
        assert value.get("status") != "running", value
        return value

    def m38_refs(names):
        entities = bridge.call("observe_nearby", {"names": names})["result"]["entities"]
        return {(e["name"], e["x"], e["y"]): e["target_ref"] for e in entities}

    try:
        m38_fixture(
            "assert(not s and not b,'M38 fixture already exists');"
            "assert(not state.action and not state.craft_action and c.crafting_queue_size==0);"
            "b={position={x=c.position.x,y=c.position.y},surface=c.surface,selected=c.selected,"
            "inventory=game.create_inventory(#inv),saved={},fields={'targets','owned_refs',"
            "'recipe_baselines','last_observation','recent_observations','explored_map',"
            "'last_action','completed_actions','completed_order','processed','processed_order',"
            "'next_target_id','next_action_id','path_request_id','last_heartbeat_tick'}};storage.m38_backup=b;"
            "for _,key in ipairs(b.fields) do b.saved[key]=state[key] end;"
            "for _,key in ipairs({'targets','owned_refs','recipe_baselines','recent_observations',"
            "'explored_map','completed_actions','completed_order','processed','processed_order'}) "
            "do state[key]={} end;state.last_observation=nil;state.last_action=nil;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(b.inventory[i].swap_stack(inv[i])) end end;b.stashed=true;"
            "s=game.create_surface(name,{width=96,height=64});s.peaceful_mode=true;"
            "s.request_to_generate_chunks({0,0},2);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-48,47 do for y=-32,31 do "
            "tiles[#tiles+1]={name='grass-1',position={x,y}} end end;s.set_tiles(tiles);"
            "assert(c.teleport({8.5,4.5},s));rcon.print('m38:{}')"
        )
        # CN: real placements, including the fallback in the placement tick.
        m38_fixture(
            "for _,e in pairs(s.find_entities()) do if e~=c then e.destroy() end end;"
            "assert(c.teleport({3.5,3.5},s));"
            "for _,v in ipairs({{'inserter',2},{'stone-furnace',2},{'electric-mining-drill',1},"
            "{'wooden-chest',5},{'offshore-pump',1},{'coal',10}}) do "
            "assert(inv.insert{name=v[1],count=v[2]}==v[2]) end;"
            "assert(s.create_entity{name='wooden-chest',position={0.5,-1.5},force=c.force});"
            "assert(s.create_entity{name='stone-furnace',position={0,1},force=c.force});"
            "rcon.print('m38:{}')"
        )
        refs = m38_refs(["wooden-chest", "stone-furnace"])
        pickup_ref = refs[("wooden-chest", 0.5, -1.5)]
        drop_ref = refs[("stone-furnace", 0, 1)]
        insertion = asyncio.run(
            m38_tool(
                "place_entity",
                {"item": "inserter", "surface": m38_surface, "x": 0.5, "y": -0.5, "direction": 0},
            )
        )
        record(
            "M38 CN placement returns the chest pickup and furnace drop refs",
            insertion.get("pickup_target") == {"target_ref": pickup_ref, "name": "wooden-chest"}
            and insertion.get("drop_target") == {"target_ref": drop_ref, "name": "stone-furnace"},
            json.dumps(insertion),
        )
        empty = asyncio.run(
            m38_tool(
                "place_entities",
                {
                    "items": [
                        {
                            "item": "inserter",
                            "surface": m38_surface,
                            "x": 3.5,
                            "y": 0.5,
                            "direction": 0,
                        }
                    ]
                },
            )
        )["results"][0]["result"]
        read = asyncio.run(
            m38_tool("get_entity_inventory", {"target_ref": insertion["target_ref"]})
        )
        empty_read = asyncio.run(
            m38_tool("get_entity_inventory", {"target_ref": empty["target_ref"]})
        )
        record(
            "M38 CN empty drop warns, target reads preserve refs and flag the empty drop",
            "drop_target" not in empty
            and "no_drop_target" in empty.get("warnings", [])
            and "ground" in empty.get("hint", "")
            and "placed next" in empty.get("hint", "")
            and read.get("pickup_target") == insertion.get("pickup_target")
            and all(
                read.get("drop_target", {}).get(key) == value
                for key, value in insertion["drop_target"].items()
            )
            and isinstance(read.get("drop_target", {}).get("status"), str)
            and empty_read.get("no_drop_target") is True
            and "drop_target" not in empty_read,
            json.dumps({"empty": empty, "read": read, "empty_read": empty_read}),
        )
        for compact in (False, True):
            observed = bridge.call("observe_nearby", {"names": ["inserter"], "compact": compact})[
                "result"
            ]
            entries = {e["target_ref"]: e for e in observed["entities"]}
            record(
                f"M38 CN observe compact={compact} flags empty drop without target refs",
                entries[empty["target_ref"]].get("no_drop_target") is True
                and not entries[insertion["target_ref"]].get("no_drop_target")
                and all(
                    "pickup_target" not in e and "drop_target" not in e for e in entries.values()
                ),
                json.dumps(observed),
            )
        m38_fixture(
            "assert(c.teleport({-1.5,2.5},s));"
            "assert(s.create_entity{name='iron-ore',position={-5.5,0.5},amount=1000});"
            "rcon.print('m38:{}')"
        )
        drill_args = {
            "item": "electric-mining-drill",
            "surface": m38_surface,
            "x": -5.5,
            "y": 0.5,
            "direction": 0,
        }
        preview = asyncio.run(m38_tool("check_placement", drill_args))
        drop = preview["drop_position"]
        m38_fixture(
            "assert(s.create_entity{name='stone-furnace',position={"
            f"{drop['x']},{drop['y'] - 0.5}"
            "},force=c.force});rcon.print('m38:{}')"
        )
        refs = m38_refs(["stone-furnace"])
        drill_furnace = next(ref for (name, x, y), ref in refs.items() if x < -3)
        drill = asyncio.run(m38_tool("place_entity", drill_args))
        batch_read = asyncio.run(
            m38_tool(
                "get_entity_inventories",
                {"target_refs": [insertion["target_ref"], drill["target_ref"]]},
            )
        )
        record(
            "M38 CN drill drops into furnace and batch inventory reads expose targets",
            drill.get("drop_target") == {"target_ref": drill_furnace, "name": "stone-furnace"}
            and all(e["ok"] for e in batch_read["results"])
            and batch_read["results"][0]["result"].get("pickup_target", {}).get("target_ref")
            == pickup_ref
            and batch_read["results"][1]["result"].get("drop_target") == drill.get("drop_target"),
            json.dumps({"drill": drill, "reads": batch_read}),
        )
        # CO: artificial water exists only on this disposable fixture surface.
        m38_fixture(
            "for _,e in pairs(s.find_entities()) do if e~=c then e.destroy() end end;"
            "local tiles={};for x=8,15 do for y=-4,3 do "
            "tiles[#tiles+1]={name='water',position={x,y}} end end;s.set_tiles(tiles);"
            "assert(c.teleport({8.5,5.5},s));rcon.print('m38:{}')"
        )
        bridge.call("observe_nearby")
        water = asyncio.run(m38_tool("get_explored_map", {}))["water"]
        pump = next((group["pump_position"] for group in water if "pump_position" in group), None)
        record(
            "M38 CO explored water has a verified pump_position",
            pump is not None,
            json.dumps(water),
        )
        if pump:
            args = {"item": "offshore-pump", "surface": m38_surface, **pump}
            check = asyncio.run(m38_tool("check_placement", args))
            placed_pump = asyncio.run(m38_tool("place_entity", args))
            record(
                "M38 CO recorded position and direction check placeable and build successfully",
                check.get("placeable") is True
                and placed_pump.get("name") == "offshore-pump"
                and placed_pump.get("direction") == pump["direction"],
                json.dumps({"check": check, "placed": placed_pump}),
            )
        # CP: item order spans thirty tiles, with normal inventory consumption.
        m38_fixture("assert(c.teleport({-12.5,16.5},s));rcon.print('m38:{}')")
        placement_items = [
            {"item": "wooden-chest", "surface": m38_surface, "x": x, "y": 12.5}
            for x in (-12.5, 17.5)
        ]
        placed = m38_action("place_entities", {"items": placement_items, "walk": True})
        record(
            "M38 CP ordered placement walks between distant items",
            placed.get("status") == "succeeded"
            and placed.get("succeeded") == 2
            and placed.get("walks", 0) >= 1
            and [r["result"]["position"]["x"] for r in placed["results"]] == [-12.5, 17.5],
            json.dumps(placed),
        )
        chest_ref = placed["results"][1]["result"]["target_ref"]
        m38_fixture("assert(c.teleport({-12.5,16.5},s));rcon.print('m38:{}')")
        transfer = {
            "target_ref": chest_ref,
            "inventory_role": "main",
            "direction": "to_entity",
            "item": "coal",
            "count": 3,
        }
        no_walk = asyncio.run(m38_tool("transfer_items", {"items": [transfer]}))
        record(
            "M38 CP transfer walk=false keeps out_of_range",
            no_walk["results"][0].get("error") == "out_of_range",
            json.dumps(no_walk),
        )
        moved = m38_action("transfer_items", {"items": [transfer], "walk": True})
        chest_read = asyncio.run(m38_tool("get_entity_inventory", {"target_ref": chest_ref}))
        record(
            "M38 CP walking transfer reaches the chest and transfers real coal",
            moved.get("walks", 0) >= 1
            and moved.get("succeeded") == 1
            and moved["results"][0]["result"].get("moved") == 3
            and any(
                i["name"] == "coal" and i["count"] == 3 for i in chest_read["inventories"]["main"]
            ),
            json.dumps(moved),
        )
        mine_refs = [r["result"]["target_ref"] for r in placed["results"]]
        no_walk_mine = asyncio.run(
            m38_tool("mine_entities", {"target_refs": mine_refs, "wait_seconds": 1})
        )
        record(
            "M38 CP mine with wait_seconds and no walk stops at its first out_of_range target",
            no_walk_mine.get("status") == "failed"
            and no_walk_mine.get("mined") == 0
            and no_walk_mine.get("error") == "out_of_range",
            json.dumps(no_walk_mine),
        )
        mined = m38_action("mine_entities", {"target_refs": mine_refs, "walk": True})
        record(
            "M38 CP walking mining reaches and removes distant targets in order",
            mined.get("status") == "succeeded"
            and mined.get("mined") == 2
            and mined.get("walks", 0) >= 2,
            json.dumps(mined),
        )
        no_walk_place = asyncio.run(m38_tool("place_entities", {"items": [placement_items[0]]}))
        record(
            "M38 CP placement walk=false keeps out_of_range",
            no_walk_place["results"][0].get("error") == "out_of_range",
            json.dumps(no_walk_place),
        )
        for tool, args in (
            ("place_entities", {"items": placement_items}),
            ("transfer_items", {"items": [transfer]}),
        ):
            response = asyncio.run(m38_server.call_tool(tool, {**args, "wait_seconds": 1}))
            record(
                f"M38 CP {tool} wait_seconds requires walk=true",
                response.is_error
                and json.loads(response.content[0].text).get("error") == "invalid_input",
            )
    finally:
        for slot in ("action", "craft_action"):
            pending = m38_fixture(
                f"rcon.print('m38:'..helpers.table_to_json({{id=b and state.{slot} "
                f"and state.{slot}.id}}))"
            )
            if pending.get("id"):
                bridge.call("cancel_action", {"action_id": pending["id"]})
        restored = m38_fixture(
            "if b then assert(c.crafting_queue_size==0);"
            "assert(c.teleport(b.position,b.surface));c.selected=b.selected;"
            "if b.stashed then inv.clear() end;"
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            "for i=1,#b.inventory do assert(not b.inventory[i].valid_for_read) end;"
            "b.inventory.destroy();if s then assert(game.delete_surface(s)) end;"
            "for _,key in ipairs(b.fields) do state[key]=b.saved[key] end;"
            "storage.m38_backup=nil;assert(c.crafting_queue_size==0);"
            "assert(c.position.x==b.position.x and c.position.y==b.position.y "
            "and c.surface==b.surface) end;"
            "rcon.print('m38:'..helpers.table_to_json({restored=true}))"
        )
        observed_map_cells.pop(m38_surface, None)
        record(
            "M38 removes fixtures and restores inventory, queue, position and observation state",
            restored.get("restored") is True
            and bridge.call("get_inventory")["result"]["items"] == m38_inventory
            and bridge.call("get_agent_status")["result"]["position"] == m38_position,
        )

    # M39 CQ/CR/CS: only this RCON fixture supplies items or teleports characters.
    m39_surface = "m39-line-approach-belt-wait-fixture"
    m39_server = create_server(bridge)
    m39_inventory = bridge.call("get_inventory")["result"]["items"]
    m39_position = bridge.call("get_agent_status")["result"]["position"]

    def m39_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;local c=state.character;"
            "assert(c and c.valid);local inv=c.get_main_inventory();"
            f'local name="{m39_surface}";local s=game.surfaces[name];'
            "local b=storage.m39_backup;" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
        if "m39:" not in raw:
            raise RuntimeError("M39 fixture failed: " + raw)
        return json.loads(raw.split("m39:", 1)[1].strip())

    async def m39_tool(name, args):
        response = await m39_server.call_tool(name, args)
        value = json.loads(response.content[0].text)
        assert not response.is_error, value
        return value

    def m39_line(args):
        value = asyncio.run(m39_tool("place_line", {**args, "walk": True, "wait_seconds": 20}))
        if value.get("status") == "running":
            value = asyncio.run(
                m39_tool(
                    "wait_for_action", {"action_id": value["action_id"], "timeout_seconds": 50}
                )
            )
        assert value.get("status") != "running", value
        return value

    def m39_refs(names):
        entities = bridge.call("observe_nearby", {"names": names})["result"]["entities"]
        return {(e["name"], e["x"], e["y"]): e["target_ref"] for e in entities}

    def m39_read_until(target_ref, predicate):
        deadline = time.monotonic() + 15
        while True:
            value = asyncio.run(m39_tool("get_entity_inventory", {"target_ref": target_ref}))
            if predicate(value) or time.monotonic() >= deadline:
                return value
            time.sleep(0.1)

    try:
        m39_fixture(
            "assert(not s and not b,'M39 fixture already exists');"
            "assert(not state.action and not state.craft_action and c.crafting_queue_size==0);"
            "b={position={x=c.position.x,y=c.position.y},surface=c.surface,selected=c.selected,"
            "electronic_circuit_enabled=c.force.recipes['electronic-circuit'].enabled,"
            "inventory=game.create_inventory(#inv),saved={},fields={'targets','owned_refs',"
            "'recipe_baselines','last_observation','recent_observations','explored_map',"
            "'last_action','completed_actions','completed_order','processed','processed_order',"
            "'next_target_id','next_action_id','path_request_id','last_heartbeat_tick'}};storage.m39_backup=b;"
            "for _,key in ipairs(b.fields) do b.saved[key]=state[key] end;"
            "for _,key in ipairs({'targets','owned_refs','recipe_baselines','recent_observations',"
            "'explored_map','completed_actions','completed_order','processed','processed_order'}) "
            "do state[key]={} end;state.last_observation=nil;state.last_action=nil;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(b.inventory[i].swap_stack(inv[i])) end end;b.stashed=true;"
            "s=game.create_surface(name,{width=192,height=96});s.peaceful_mode=true;"
            "s.request_to_generate_chunks({0,0},3);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-96,95 do for y=-48,47 do "
            "tiles[#tiles+1]={name='grass-1',position={x,y}} end end;s.set_tiles(tiles);"
            "assert(inv.insert{name='transport-belt',count=30}==30);"
            "assert(c.teleport({-20.5,-16.5},s));rcon.print('m39:{}')"
        )
        distant_line = {
            "item": "transport-belt",
            "surface": m39_surface,
            "from_x": 10.5,
            "from_y": -20.5,
            "to_x": 22.5,
            "to_y": -20.5,
        }
        no_walk = asyncio.run(m39_tool("place_line", distant_line))
        record(
            "M39 CQ distant walk=false preserves the synchronous out_of_range result",
            no_walk.get("placed") == 0 and no_walk.get("stopped") == "out_of_range",
            json.dumps(no_walk),
        )
        line = m39_line(distant_line)
        measured = m39_fixture(
            "local n=0;for x=10,22 do local e=s.find_entity('transport-belt',{x+0.5,-20.5});"
            "if e and e.valid then n=n+1 end end;"
            "rcon.print('m39:'..helpers.table_to_json({belts=n,remaining="
            "inv.get_item_count('transport-belt'),idle=state.action==nil}))"
        )
        record(
            "M39 CQ start thirty tiles away approaches and places the entire line",
            line.get("status") == "succeeded"
            and line.get("complete") is True
            and line.get("placed") == 13
            and measured == {"belts": 13, "remaining": 17, "idle": True},
            json.dumps({"line": line, "measured": measured}),
        )
        # All geometrically eligible stand candidates are occupied, but the line
        # itself is empty. A distant approach can reach build range outside the wall.
        m39_fixture(
            "for x=-3,8 do for y=5,12 do if not(y==8 and x>=0 and x<=4) then "
            "assert(s.create_entity{name='stone-wall',position={x+0.5,y+0.5},force=c.force}) "
            "end end end;rcon.print('m39:{}')"
        )
        enclosed_line = {
            "item": "transport-belt",
            "surface": m39_surface,
            "from_x": 0.5,
            "from_y": 8.5,
            "to_x": 4.5,
            "to_y": 8.5,
        }
        # Vanilla 2.0.77 build reach is 10; the near case must still be outside it.
        for start_x, label in ((-13.5, "near"), (-29.5, "distant")):
            m39_fixture(
                f"assert(c.teleport({{{start_x},8.5}},s));"
                "state.last_observation=nil;state.recent_observations={};rcon.print('m39:{}')"
            )
            enclosed = m39_line(enclosed_line)
            measured = m39_fixture(
                "rcon.print('m39:'..helpers.table_to_json({idle=state.action==nil,"
                "remaining=inv.get_item_count('transport-belt'),x=c.position.x,y=c.position.y}))"
            )
            record(
                f"M39 CQ {label} enclosed start stops without looping or consuming items",
                enclosed.get("status") == "succeeded"
                and enclosed.get("complete") is False
                and enclosed.get("placed") == 0
                and enclosed.get("detail", {}).get("reason") == "stand_position_not_found"
                and measured.get("idle") is True
                and measured.get("remaining") == 17
                and (label != "near" or (measured["x"], measured["y"]) == (start_x, 8.5)),
                json.dumps({"line": enclosed, "measured": measured}),
            )
        m39_fixture(
            "assert(c.teleport({-60.5,-20.5},s));state.recent_observations={};"
            "state.last_observation=nil;rcon.print('m39:{}')"
        )
        too_far = m39_line({**distant_line, "from_x": 80.5, "to_x": 81.5})
        record(
            "M39 CQ approach retains the shared 128 tile limit",
            too_far.get("status") == "failed"
            and too_far.get("error") == "out_of_range"
            and too_far.get("placed") == 0,
            json.dumps(too_far),
        )
        # CR: seed real transport lines; no downstream belt can remove the items.
        for kind, x in (("transport-belt", -4.5), ("underground-belt", 0.5), ("splitter", 4.5)):
            seeded = m39_fixture(
                f"assert(c.teleport({{{x},3.5}},s));"
                f"local e=s.create_entity{{name='{kind}',position={{{x},0.5}},"
                "direction=4,force=c.force};assert(e);e.active=false;"
                "for i=1,2 do assert(e.get_transport_line(i).insert_at_back"
                "{name='coal',count=1}) end;"
                "local n=0;for i=1,e.get_max_transport_line_index() do "
                "local l=e.get_transport_line(i);if l and l.valid then "
                "n=n+l.get_item_count('coal') end end;"
                "rcon.print('m39:'..helpers.table_to_json({count=n,x=e.position.x,y=e.position.y}))"
            )
            target_ref = m39_refs([kind])[(kind, seeded["x"], seeded["y"])]
            args = {"target_ref": target_ref, "item": "coal", "count": 2, "timeout_seconds": 1}
            waited = asyncio.run(m39_tool("wait_for_items", args))
            record(
                f"M39 CR {kind} wait counts items on transport lines and reaches the count",
                seeded["count"] >= 2
                and waited.get("belt") is True
                and waited.get("current") == seeded["count"]
                and waited.get("reached") is True,
                json.dumps(waited),
            )
            for extra in ({"produced": True}, {"inventory_role": "main"}):
                response = asyncio.run(m39_server.call_tool("wait_for_items", {**args, **extra}))
                value = json.loads(response.content[0].text)
                record(
                    f"M39 CR {kind} rejects {next(iter(extra))}",
                    response.is_error and value.get("error") == "invalid_input",
                    json.dumps(value),
                )
        # CS: power both machines. A held plate cannot enter an assembler with no
        # recipe, or one already holding enough plates while copper cable is missing.
        # Keep the source and both consumers within the pole's supply area.
        m39_fixture(
            "assert(c.teleport({3.5,18.5},s));"
            "b.machine=s.create_entity{name='assembling-machine-1',position={0.5,16.5},"
            "force=c.force};assert(b.machine);"
            "b.inserter=s.create_entity{name='inserter',position={0.5,14.5},"
            "direction=0,force=c.force};assert(b.inserter);"
            "assert(b.inserter.held_stack.set_stack{name='iron-plate',count=1});"
            "local generator=s.create_entity{name='electric-energy-interface',"
            "position={4.5,14.5},force=c.force};assert(generator);"
            "generator.power_production=1000000/60;generator.power_usage=0;"
            "generator.electric_buffer_size=1000000;generator.energy=1000000;"
            "assert(s.create_entity{name='small-electric-pole',position={2.5,14.5},force=c.force});"
            "rcon.print('m39:{}')"
        )
        refs = m39_refs(["inserter", "assembling-machine-1"])
        inserter_ref = refs[("inserter", 0.5, 14.5)]
        assembler_ref = refs[("assembling-machine-1", 0.5, 16.5)]
        no_recipe = m39_read_until(
            inserter_ref,
            lambda value: (
                value.get("drop_target", {}).get("status") == "no_recipe"
                and value.get("status") == "waiting_for_space_in_destination"
            ),
        )
        record(
            "M39 CS single inventory read shows inserter destination no_recipe",
            no_recipe.get("drop_target", {}).get("target_ref") == assembler_ref
            and no_recipe.get("drop_target", {}).get("status") == "no_recipe"
            and no_recipe.get("status") == "waiting_for_space_in_destination",
            json.dumps(no_recipe),
        )
        m39_fixture(
            "c.force.recipes['electronic-circuit'].enabled=true;"
            "assert(b.machine.set_recipe('electronic-circuit'));"
            "local input=b.machine.get_inventory(defines.inventory.crafter_input);input.clear();"
            "assert(input.insert{name='iron-plate',count=100}==100);"
            "assert(b.machine.get_output_inventory().insert{name='electronic-circuit',count=200}==200);"
            "assert(b.inserter.held_stack.set_stack{name='iron-plate',count=1});rcon.print('m39:{}')"
        )
        shortage = m39_read_until(
            inserter_ref,
            lambda value: (
                value.get("drop_target", {}).get("status") == "item_ingredient_shortage"
                and value.get("status") == "waiting_for_space_in_destination"
            ),
        )
        missing = [{"name": "copper-cable", "count": 3}]
        batch = asyncio.run(m39_tool("get_entity_inventories", {"target_refs": [inserter_ref]}))
        batch_record = batch["results"][0]["result"]
        record(
            "M39 CS single and batch reads show corrected destination shortage and missing items",
            all(
                value.get("drop_target", {}).get("status") == "item_ingredient_shortage"
                and value.get("drop_target", {}).get("missing_ingredients") == missing
                and value.get("status") == "waiting_for_space_in_destination"
                for value in (shortage, batch_record)
            ),
            json.dumps({"single": shortage, "batch": batch}),
        )
        record(
            "M39 CS waiting_for_space inserter explains destination diagnostics",
            shortage.get("status") == "waiting_for_space_in_destination"
            and all(
                phrase in shortage.get("hint", "")
                for phrase in (
                    "drop_target.status",
                    "no_recipe",
                    "full_output",
                    "missing_ingredients",
                    "missing_fluids",
                )
            ),
            json.dumps(shortage),
        )
    finally:
        for slot in ("action", "craft_action"):
            pending = m39_fixture(
                f"rcon.print('m39:'..helpers.table_to_json({{id=b and state.{slot} "
                f"and state.{slot}.id}}))"
            )
            if pending.get("id"):
                bridge.call("cancel_action", {"action_id": pending["id"]})
        restored = m39_fixture(
            "if b then assert(c.crafting_queue_size==0);"
            "c.force.recipes['electronic-circuit'].enabled=b.electronic_circuit_enabled;"
            "assert(c.teleport(b.position,b.surface));c.selected=b.selected;"
            "if b.stashed then inv.clear() end;"
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            "for i=1,#b.inventory do assert(not b.inventory[i].valid_for_read) end;"
            "b.inventory.destroy();if s then assert(game.delete_surface(s)) end;"
            "for _,key in ipairs(b.fields) do state[key]=b.saved[key] end;"
            "storage.m39_backup=nil;assert(c.crafting_queue_size==0);"
            "assert(c.position.x==b.position.x and c.position.y==b.position.y "
            "and c.surface==b.surface) end;"
            "rcon.print('m39:'..helpers.table_to_json({restored=true}))"
        )
        observed_map_cells.pop(m39_surface, None)
        record(
            "M39 removes fixtures and restores inventory, queue, position and observation state",
            restored.get("restored") is True
            and bridge.call("get_inventory")["result"]["items"] == m39_inventory
            and bridge.call("get_agent_status")["result"]["position"] == m39_position,
        )

    # M40 CT/CU: only fixture RCON creates resources, supplies items or teleports.
    m40_surface = "m40-oil-entities-fixture"
    m40_server = create_server(bridge)
    m40_inventory = bridge.call("get_inventory")["result"]["items"]
    m40_position = bridge.call("get_agent_status")["result"]["position"]

    def m40_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;local c=state.character;"
            "assert(c and c.valid);local inv=c.get_main_inventory();"
            f'local name="{m40_surface}";local s=game.surfaces[name];'
            "local b=storage.m40_backup;" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
        if "m40:" not in raw:
            raise RuntimeError("M40 fixture failed: " + raw)
        return json.loads(raw.split("m40:", 1)[1].strip())

    def m40_tool(name, args):
        response = asyncio.run(m40_server.call_tool(name, args))
        value = json.loads(response.content[0].text)
        assert not response.is_error, value
        return value

    def m40_error(name, args, code, reason):
        response = asyncio.run(m40_server.call_tool(name, args))
        value = json.loads(response.content[0].text)
        record(
            f"M40 {name} rejects {reason}",
            response.is_error
            and value.get("error") == code
            and value.get("detail", {}).get("reason") == reason,
            json.dumps(value),
        )
        return value

    def m40_read_until(target_ref, predicate):
        deadline = time.monotonic() + 12
        while True:
            value = m40_tool("get_entity_inventory", {"target_ref": target_ref})
            if predicate(value) or time.monotonic() >= deadline:
                return value
            time.sleep(0.1)

    def m40_place(item, x, direction=0, fluid=False):
        m40_fixture(f"assert(c.teleport({{{x},5.5}},s));rcon.print('m40:{{}}')")
        args = {"item": item, "surface": m40_surface, "x": x, "y": 0.5, "direction": direction}
        preview = m40_tool("check_placement", args)
        placed = m40_tool("place_entity", args)
        record(
            f"M40 CT {item} checks and places a real inventory item",
            preview.get("placeable") is True and placed.get("consumed_item") == item,
            json.dumps({"preview": preview, "placed": placed}),
        )
        if fluid:
            # Compare prototype ports against live API ports, including the unusual
            # direction-dependent pumpjack output. Do not synthesize hidden boxes.
            def ports(value, relative=False):
                centre = value.get("position", value)
                x, y = (centre["x"], centre["y"]) if relative else (0, 0)
                return {
                    (
                        p["position"]["x"] - x,
                        p["position"]["y"] - y,
                        p["target_position"]["x"] - x,
                        p["target_position"]["y"] - y,
                    )
                    for p in value.get("fluid_connections", [])
                }

            record(
                f"M40 CT {item} preview matches actual direction-dependent fluid ports",
                bool(ports(placed)) and ports(placed) == ports(preview),
            )
            # Measure the native API cycle instead of assuming a 90-degree step.
            # supports_direction does not guarantee four reachable orientations:
            # tanks use two_direction_only and placed 1x2 pumps retain their axis.
            native = m40_fixture(
                f"local e=s.find_entities_filtered{{name='{item}'}}[1];assert(e);"
                "local original=e.direction;local seen={[original]=true};local next_direction;"
                "for i=1,4 do if not e.rotate{reverse=false} then break end;"
                "next_direction=next_direction or e.direction;"
                "if seen[e.direction] then break end;seen[e.direction]=true end;"
                "e.direction=original;local reachable={};"
                "for d in pairs(seen) do reachable[#reachable+1]=d end;table.sort(reachable);"
                "rcon.print('m40:'..helpers.table_to_json({direction=original,"
                "next_direction=next_direction,reachable=reachable}))"
            )
            rotated_direction = native.get("next_direction", native["direction"])
            # A rotation regression must be recorded without skipping the remaining
            # inventory/category checks; the outer finally always restores the fixture.
            response = asyncio.run(
                m40_server.call_tool(
                    "rotate_entity",
                    {"target_ref": placed["target_ref"], "direction": rotated_direction},
                )
            )
            rotated = json.loads(response.content[0].text)
            rotated_preview = m40_tool("check_placement", {**args, "direction": rotated_direction})
            entities = m40_tool("observe_nearby", {"names": [item]})["entities"]
            observed = next(e for e in entities if e["target_ref"] == placed["target_ref"])
            record(
                f"M40 CT {item} rotates and live fluid ports follow the new direction",
                not response.is_error
                and rotated.get("direction") == rotated_direction
                and ports(observed, relative=True) == ports(rotated_preview, relative=True),
                json.dumps({"native": native, "rotated": rotated, "observed": observed}),
            )
            if item == "pump":
                unreachable = next(d for d in (0, 4, 8, 12) if d not in native["reachable"])
                rejected = m40_error(
                    "rotate_entity",
                    {"target_ref": placed["target_ref"], "direction": unreachable},
                    "invalid_target",
                    "direction_unreachable",
                )
                actual = next(
                    e
                    for e in m40_tool("observe_nearby", {"names": [item]})["entities"]
                    if e["target_ref"] == placed["target_ref"]
                )
                detail = rejected.get("detail", {})
                record(
                    "M40 CT pump rejects the other axis and restores its original direction",
                    native["reachable"] == [4, 12]
                    and detail.get("requested_direction") == unreachable
                    and detail.get("direction") == actual.get("direction") == observed["direction"]
                    and detail.get("reachable_directions") == native["reachable"],
                    json.dumps(rejected),
                )
                for reachable in detail.get("reachable_directions", []):
                    response = asyncio.run(
                        m40_server.call_tool(
                            "rotate_entity",
                            {"target_ref": placed["target_ref"], "direction": reachable},
                        )
                    )
                    result = json.loads(response.content[0].text)
                    record(
                        f"M40 CT pump reaches reported direction {reachable}",
                        not response.is_error and result.get("direction") == reachable,
                        json.dumps(result),
                    )
        return placed

    def m40_power(placed):
        p = placed["position"]
        m40_fixture(
            f"local x,y={p['x']},{p['y']};"
            "local g=s.create_entity{name='electric-energy-interface',"
            "position={x+4,y-2},force=c.force};assert(g);"
            "g.power_production=1000000/60;g.power_usage=0;"
            "g.electric_buffer_size=1000000;g.energy=1000000;"
            "assert(s.create_entity{name='small-electric-pole',"
            "position={x+2,y-2},force=c.force});rcon.print('m40:{}')"
        )

    try:
        m40_fixture(
            "assert(not s and not b,'M40 fixture already exists');"
            "assert(not state.action and not state.craft_action and c.crafting_queue_size==0);"
            "b={position={x=c.position.x,y=c.position.y},surface=c.surface,selected=c.selected,"
            "inventory=game.create_inventory(#inv),saved={},recipes={},technologies={},queue={},"
            "research_enabled=c.force.research_enabled,progress=c.force.research_progress,"
            "fields={'targets','owned_refs','recipe_baselines','last_observation',"
            "'recent_observations','explored_map','last_action','completed_actions',"
            "'completed_order','processed','processed_order','next_target_id','next_action_id',"
            "'path_request_id','last_heartbeat_tick'}};storage.m40_backup=b;"
            "for _,t in ipairs(c.force.research_queue or {}) do b.queue[#b.queue+1]=t.name end;"
            "for n,r in pairs(c.force.recipes) do b.recipes[n]=r.enabled end;"
            "for n,t in pairs(c.force.technologies) do b.technologies[n]={researched=t.researched,"
            "enabled=t.enabled,level=t.level,progress=t.saved_progress} end;"
            "c.force.disable_research();"
            "for _,key in ipairs(b.fields) do b.saved[key]=state[key] end;"
            "for _,key in ipairs({'targets','owned_refs','recipe_baselines','recent_observations',"
            "'explored_map','completed_actions','completed_order','processed','processed_order'}) "
            "do state[key]={} end;state.last_observation=nil;state.last_action=nil;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(b.inventory[i].swap_stack(inv[i])) end end;b.stashed=true;"
            "s=game.create_surface(name,{width=128,height=64});s.peaceful_mode=true;"
            "s.request_to_generate_chunks({0,0},3);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-64,63 do for y=-32,31 do "
            "tiles[#tiles+1]={name=(x>=20 and x<=23 and y>=20 and y<=23) and 'water' or 'grass-1',"
            "position={x,y}} end end;s.set_tiles(tiles);"
            "assert(s.create_entity{name='crude-oil',position={-32.5,0.5},amount=300000});"
            "for _,n in ipairs({'basic-oil-processing','plastic-bar','sulfur','sulfuric-acid',"
            "'lubricant','iron-gear-wheel','engine-unit','electric-engine-unit'}) do "
            "assert(c.force.recipes[n]);c.force.recipes[n].enabled=true end;"
            "for n,count in pairs({['pumpjack']=2,['oil-refinery']=1,['chemical-plant']=1,"
            "['assembling-machine-2']=1,['steel-chest']=1,['storage-tank']=1,['pump']=1,"
            "['assembling-machine-1']=1,['coal']=20,['iron-plate']=20}) do "
            "assert(inv.insert{name=n,count=count}==count) end;"
            "assert(c.teleport({-32.5,5.5},s));rcon.print('m40:{}')"
        )
        oil_observation = m40_tool("observe_nearby", {"include_resources": True})
        oil = next(e for e in oil_observation["entities"] if e["name"] == "crude-oil")
        summary = next(r for r in oil_observation["resources"] if r["name"] == "crude-oil")
        explored = m40_tool("get_explored_map", {"resource_names": ["crude-oil"]})
        record(
            "M40 CU crude oil is observed and remembered as a deposit count and yield",
            summary.get("count") == 1
            and "total_amount" not in summary
            and math.isclose(summary.get("yield_percent", -1), 100)
            and len(explored["resources"]) == 1
            and explored["resources"][0].get("count") == 1
            and "total_amount" not in explored["resources"][0],
            json.dumps({"summary": summary, "map": explored}),
        )
        m40_fixture("assert(c.teleport({-32.5,2.5},s));rcon.print('m40:{}')")
        rejected = m40_error(
            "mine_entity", {"target_ref": oil["target_ref"]}, "invalid_target", "fluid_resource"
        )
        record(
            "M40 CU hand mining hint directs the AI to a pumpjack",
            "pumpjack" in rejected.get("hint", ""),
        )
        tech = m40_tool("get_technology_info", {"technology": "oil-processing"})
        record(
            "M40 CU oil-processing exposes its mining trigger even before research",
            tech.get("trigger") == {"type": "mine-entity", "entity": "crude-oil"},
            json.dumps(tech),
        )

        pumpjack = m40_place("pumpjack", -32.5, direction=4, fluid=True)
        record(
            "M40 CU pumpjack placement reports crude oil in its mining area",
            pumpjack.get("mining_area_resources") == [{"name": "crude-oil", "tiles": 1}],
        )
        m40_power(pumpjack)
        pumped = m40_read_until(
            pumpjack["target_ref"],
            lambda v: any(
                f.get("name") == "crude-oil" and f.get("amount", 0) > 0 for f in v.get("fluids", [])
            ),
        )
        record(
            "M40 CU a powered pumpjack actually extracts crude oil",
            pumped.get("mining_target") == "crude-oil"
            and any(
                f.get("name") == "crude-oil" and f.get("amount", 0) > 0
                for f in pumped.get("fluids", [])
            ),
            json.dumps(pumped),
        )
        compact = m40_tool("observe_nearby", {"names": ["pumpjack"], "compact": True})["entities"][
            0
        ]
        record(
            "M40 CU compact pumpjack observation keeps mining_target and omits fluids",
            compact.get("mining_target") == "crude-oil" and "fluids" not in compact,
        )
        full = m40_tool("observe_nearby", {"names": ["pumpjack"], "compact": False})["entities"][0]
        record(
            "M40 CU full pumpjack observation retains fluids and mining_target",
            full.get("mining_target") == "crude-oil" and bool(full.get("fluids")),
        )
        off_oil = {"item": "pumpjack", "surface": m40_surface, "x": -32.5, "y": 10.5}
        preview = m40_tool("check_placement", off_oil)
        record(
            "M40 CU pumpjack preview rejects placement off crude oil clearly",
            preview.get("placeable") is False
            and preview.get("blocked_reason") == "no_resource"
            and preview.get("mining_area_resources") == []
            and "crude oil" in preview.get("hint", ""),
            json.dumps(preview),
        )
        m40_error("place_entity", off_oil, "blocked", "no_resource")

        refinery = m40_place("oil-refinery", -20.5, direction=8, fluid=True)
        configured = m40_tool(
            "set_recipe", {"target_ref": refinery["target_ref"], "recipe": "basic-oil-processing"}
        )
        read = m40_tool("get_entity_inventory", {"target_ref": refinery["target_ref"]})
        record(
            "M40 CT refinery accepts basic-oil-processing and reports recipe and fluids",
            configured.get("recipe") == read.get("recipe") == "basic-oil-processing"
            and bool(read.get("fluids"))
            and "source" in read["inventories"]
            and "result" in read["inventories"],
            json.dumps(read),
        )

        plant = m40_place("chemical-plant", -8.5, direction=12, fluid=True)
        m40_power(plant)
        m40_tool("set_recipe", {"target_ref": plant["target_ref"], "recipe": "plastic-bar"})
        transfer = m40_tool(
            "transfer_item",
            {
                "target_ref": plant["target_ref"],
                "direction": "to_entity",
                "inventory_role": "input",
                "item": "coal",
                "count": 5,
            },
        )
        read = m40_tool("get_entity_inventory", {"target_ref": plant["target_ref"]})
        record(
            "M40 CT chemical plant accepts coal in source for plastic-bar",
            transfer.get("moved") == 5
            and read.get("recipe") == "plastic-bar"
            and bool(read.get("status"))
            and sum(i["count"] for i in read["inventories"]["source"] if i["name"] == "coal") == 5,
            json.dumps(read),
        )
        m40_fixture(
            "local e=assert(s.find_entity('chemical-plant',{-8.5,0.5}));"
            "e.get_inventory(defines.inventory.crafter_input).clear();"
            "assert(e.get_output_inventory().insert{name='plastic-bar',count=10000}>0);"
            "rcon.print('m40:{}')"
        )
        shortage = m40_read_until(
            plant["target_ref"], lambda v: v.get("status") == "item_ingredient_shortage"
        )
        record(
            "M40 CT chemical plant shortage correction counts coal and excludes fluids",
            shortage.get("status") == "item_ingredient_shortage"
            and shortage.get("missing_ingredients") == [{"name": "coal", "count": 1}],
            json.dumps(shortage),
        )

        assembler = m40_place("assembling-machine-2", 3.5)
        m40_tool(
            "set_recipe", {"target_ref": assembler["target_ref"], "recipe": "electric-engine-unit"}
        )
        fluid_read = m40_tool("get_entity_inventory", {"target_ref": assembler["target_ref"]})
        fluid_observed = m40_tool("observe_nearby", {"names": ["assembling-machine-2"]})[
            "entities"
        ][0]
        record(
            "M40 CT assembling-machine-2 exposes live fluid boxes for a fluid recipe",
            fluid_read.get("recipe") == "electric-engine-unit"
            and bool(fluid_read.get("fluids"))
            and bool(fluid_observed.get("fluid_connections")),
            json.dumps(fluid_read),
        )
        m40_tool("set_recipe", {"target_ref": assembler["target_ref"], "recipe": "iron-gear-wheel"})
        transfer = m40_tool(
            "transfer_item",
            {
                "target_ref": assembler["target_ref"],
                "direction": "to_entity",
                "inventory_role": "source",
                "item": "iron-plate",
                "count": 5,
            },
        )
        read = m40_tool("get_entity_inventory", {"target_ref": assembler["target_ref"]})
        record(
            "M40 CT assembling-machine-2 supports recipe, inventory read and transfer",
            transfer.get("moved") == 5
            and read.get("recipe") == "iron-gear-wheel"
            and sum(i["count"] for i in read["inventories"]["source"] if i["name"] == "iron-plate")
            == 5,
        )
        chest = m40_place("steel-chest", 15.5)
        transfer = m40_tool(
            "transfer_item",
            {
                "target_ref": chest["target_ref"],
                "direction": "to_entity",
                "inventory_role": "main",
                "item": "coal",
                "count": 2,
            },
        )
        read = m40_tool("get_entity_inventory", {"target_ref": chest["target_ref"]})
        record(
            "M40 CT steel-chest supports main inventory and real item transfer",
            transfer.get("moved") == 2
            and sum(i["count"] for i in read["inventories"]["main"] if i["name"] == "coal") == 2,
        )
        for item, x, direction in (("storage-tank", 27.5, 0), ("pump", 39.5, 12)):
            placed = m40_place(item, x, direction=direction, fluid=True)
            read = m40_tool("get_entity_inventory", {"target_ref": placed["target_ref"]})
            batch = m40_tool("get_entity_inventories", {"target_refs": [placed["target_ref"]]})
            record(
                f"M40 CT {item} single and batch reads report status and fluid boxes",
                read.get("inventories") == {}
                and bool(read.get("fluids"))
                and bool(read.get("status"))
                and batch["results"][0].get("result", {}).get("fluids") == read.get("fluids"),
                json.dumps(read),
            )
        old_assembler = m40_place("assembling-machine-1", 51.5)
        m40_error(
            "set_recipe",
            {"target_ref": old_assembler["target_ref"], "recipe": "plastic-bar"},
            "invalid_input",
            "unsupported_recipe_category",
        )
    finally:
        for slot in ("action", "craft_action"):
            pending = m40_fixture(
                f"rcon.print('m40:'..helpers.table_to_json({{id=b and state.{slot} "
                f"and state.{slot}.id}}))"
            )
            if pending.get("id"):
                bridge.call("cancel_action", {"action_id": pending["id"]})
        restored = m40_fixture(
            "if b then assert(c.crafting_queue_size==0);c.force.research_queue={};"
            "for n,v in pairs(b.technologies) do local t=c.force.technologies[n];"
            "if t.level~=v.level then t.level=v.level end;"
            "if t.researched~=v.researched then t.researched=v.researched end;"
            "t.enabled=v.enabled end;"
            "for n,enabled in pairs(b.recipes) do c.force.recipes[n].enabled=enabled end;"
            "c.force.research_queue=b.queue;"
            "for n,v in pairs(b.technologies) do "
            "c.force.technologies[n].saved_progress=v.progress end;"
            "if c.force.current_research then c.force.research_progress=b.progress end;"
            "if b.research_enabled then c.force.enable_research() "
            "else c.force.disable_research() end;"
            "for n,v in pairs(b.technologies) do local t=c.force.technologies[n];"
            "assert(t.level==v.level and t.researched==v.researched and t.enabled==v.enabled "
            "and t.saved_progress==v.progress) end;"
            "for n,enabled in pairs(b.recipes) do assert(c.force.recipes[n].enabled==enabled) end;"
            "local queue=c.force.research_queue or {};assert(#queue==#b.queue);"
            "for i,t in ipairs(queue) do assert(t.name==b.queue[i]) end;"
            "assert(c.force.research_progress==b.progress "
            "and c.force.research_enabled==b.research_enabled);"
            "assert(c.teleport(b.position,b.surface));c.selected=b.selected;"
            "if b.stashed then inv.clear() end;"
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            "for i=1,#b.inventory do assert(not b.inventory[i].valid_for_read) end;"
            "b.inventory.destroy();if s then assert(game.delete_surface(s)) end;"
            "for _,key in ipairs(b.fields) do state[key]=b.saved[key] end;"
            "for _,key in ipairs(b.fields) do assert(state[key]==b.saved[key]) end;"
            "storage.m40_backup=nil;assert(c.crafting_queue_size==0);"
            "assert(c.position.x==b.position.x and c.position.y==b.position.y "
            "and c.surface==b.surface) end;"
            "rcon.print('m40:'..helpers.table_to_json({restored=true}))"
        )
        # Surface deletion completes at the end of the tick, so verify removal
        # in a separate RCON request after the restoration command has completed.
        removed = m40_fixture(
            "rcon.print('m40:'..helpers.table_to_json({backup=b~=nil,surface=s~=nil}))"
        )
        observed_map_cells.pop(m40_surface, None)
        record(
            "M40 restores inventory, queue, position, observations and force recipes/technologies",
            restored.get("restored") is True
            and removed == {"backup": False, "surface": False}
            and bridge.call("get_inventory")["result"]["items"] == m40_inventory
            and bridge.call("get_agent_status")["result"]["position"] == m40_position,
        )

    # M41 CV/CW/CX: all world mutation outside tools is disposable fixture setup/cleanup.
    m41_surface = "m41-fluid-recipes-fixture"
    m41_server = create_server(bridge)
    m41_inventory = bridge.call("get_inventory")["result"]["items"]
    m41_position = bridge.call("get_agent_status")["result"]["position"]

    def m41_fixture(body):
        command = (
            "/c __factorio_ai_mcp__ local state=storage.ai_mcp;local c=state.character;"
            "assert(c and c.valid);local inv=c.get_main_inventory();"
            f'local name="{m41_surface}";local s=game.surfaces[name];'
            "local b=storage.m41_backup;" + body
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=10, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            raw = client.run(command)
        if "m41:" not in raw:
            raise RuntimeError("M41 fixture failed: " + raw)
        return json.loads(raw.split("m41:", 1)[1].strip())

    def m41_tool(name, args):
        response = asyncio.run(m41_server.call_tool(name, args))
        value = json.loads(response.content[0].text)
        assert not response.is_error, value
        return value

    def m41_near(x, y):
        m41_fixture(f"assert(c.teleport({{{x},{y + 5}}},s));rcon.print('m41:{{}}')")

    def m41_place(item, x, y, direction=0):
        m41_near(x, y)
        return m41_tool(
            "place_entity",
            {"item": item, "surface": m41_surface, "x": x, "y": y, "direction": direction},
        )

    def m41_read(placed):
        p = placed["position"]
        m41_near(p["x"], p["y"])
        return m41_tool("get_entity_inventory", {"target_ref": placed["target_ref"]})

    def m41_until(placed, predicate):
        deadline = time.monotonic() + 20
        while True:
            value = m41_read(placed)
            if predicate(value) or time.monotonic() >= deadline:
                return value
            time.sleep(0.1)

    def m41_power(placed):
        p = placed["position"]
        m41_fixture(
            f"local x,y={p['x']},{p['y']};"
            "local g=s.create_entity{name='electric-energy-interface',"
            "position={x+4,y-2},force=c.force};assert(g);"
            "g.power_production=1000000/60;g.power_usage=0;"
            "g.electric_buffer_size=1000000;g.energy=1000000;"
            "assert(s.create_entity{name='small-electric-pole',"
            "position={x+2,y-2},force=c.force});rcon.print('m41:{}')"
        )

    def m41_route(start, end, via_y):
        # Endpoints come from the returned port targets; run clear of machine footprints.
        x, y = start["x"], start["y"]
        points = [(x, y)]
        for tx, ty in ((x, via_y), (end["x"], via_y), (end["x"], end["y"])):
            while (x, y) != (tx, ty):
                if x != tx:
                    x += 1 if tx > x else -1
                else:
                    y += 1 if ty > y else -1
                points.append((x, y))
        return [m41_place("pipe", x, y) for x, y in points]

    try:
        m41_fixture(
            "assert(not s and not b,'M41 fixture already exists');"
            "assert(not state.action and not state.craft_action and c.crafting_queue_size==0);"
            "b={position={x=c.position.x,y=c.position.y},surface=c.surface,selected=c.selected,"
            "inventory=game.create_inventory(#inv),saved={},recipes={},technologies={},queue={},"
            "research_enabled=c.force.research_enabled,progress=c.force.research_progress,"
            "fields={'targets','owned_refs','recipe_baselines','last_observation',"
            "'recent_observations','explored_map','last_action','completed_actions',"
            "'completed_order','processed','processed_order','next_target_id','next_action_id',"
            "'path_request_id','last_heartbeat_tick'}};storage.m41_backup=b;"
            "for _,t in ipairs(c.force.research_queue or {}) do b.queue[#b.queue+1]=t.name end;"
            "for n,r in pairs(c.force.recipes) do b.recipes[n]=r.enabled end;"
            "for n,t in pairs(c.force.technologies) do b.technologies[n]={researched=t.researched,"
            "enabled=t.enabled,level=t.level,progress=t.saved_progress} end;"
            "c.force.disable_research();"
            "for _,key in ipairs(b.fields) do b.saved[key]=state[key] end;"
            "for _,key in ipairs({'targets','owned_refs','recipe_baselines','recent_observations',"
            "'explored_map','completed_actions','completed_order','processed','processed_order'}) "
            "do state[key]={} end;state.last_observation=nil;state.last_action=nil;"
            "for i=1,#inv do if inv[i].valid_for_read then "
            "assert(b.inventory[i].swap_stack(inv[i])) end end;b.stashed=true;"
            "s=game.create_surface(name,{width=128,height=64});s.peaceful_mode=true;"
            "s.request_to_generate_chunks({0,0},3);s.force_generate_chunk_requests();"
            "for _,e in pairs(s.find_entities()) do e.destroy() end;"
            "local tiles={};for x=-64,63 do for y=-32,31 do "
            "tiles[#tiles+1]={name=(x>=20 and x<=23 and y>=20 and y<=23) and 'water' or 'grass-1',"
            "position={x,y}} end end;s.set_tiles(tiles);"
            "assert(s.create_entity{name='crude-oil',position={-14.5,0.5},amount=300000});"
            "for _,n in ipairs({'basic-oil-processing','plastic-bar','sulfur','sulfuric-acid',"
            "'lubricant','iron-gear-wheel','engine-unit','electric-engine-unit'}) do "
            "assert(c.force.recipes[n]);c.force.recipes[n].enabled=true end;"
            "for n,count in pairs({['pumpjack']=1,['oil-refinery']=2,['chemical-plant']=1,"
            "['pipe']=150,['pipe-to-ground']=2,['coal']=20}) do "
            "assert(inv.insert{name=n,count=count}==count) end;"
            "assert(c.teleport({-14.5,5.5},s));rcon.print('m41:{}')"
        )
        jack = m41_place("pumpjack", -14.5, 0.5, direction=4)
        m41_power(jack)
        refinery = m41_place("oil-refinery", -2.5, 0.5)
        m41_power(refinery)
        configured = m41_tool(
            "set_recipe", {"target_ref": refinery["target_ref"], "recipe": "basic-oil-processing"}
        )
        ports = configured.get("fluid_connections", [])
        crude = next(p for p in ports if p.get("fluid") == "crude-oil")
        gas = next(p for p in ports if p.get("fluid") == "petroleum-gas")
        repeated = m41_tool(
            "set_recipe", {"target_ref": refinery["target_ref"], "recipe": "basic-oil-processing"}
        )
        record(
            "M41 CV set_recipe and no-op return recipe fluids and production_type per port",
            crude.get("production_type") == "input"
            and gas.get("production_type") == "output"
            and repeated.get("fluid_connections") == ports,
            json.dumps(configured),
        )
        oil_pipes = m41_route(
            jack["fluid_connections"][0]["target_position"], crude["target_position"], 8.5
        )
        read = m41_until(
            refinery,
            lambda v: any(
                f.get("name") == "petroleum-gas" and f["amount"] > 0 for f in v.get("fluids", [])
            ),
        )
        record(
            "M41 CV pumpjack through pipes supplies refinery and produces petroleum-gas",
            any(
                f.get("name") == "petroleum-gas" and f["amount"] > 0 for f in read.get("fluids", [])
            ),
            json.dumps(read),
        )
        starved = m41_place("oil-refinery", -2.5, 18.5)
        m41_power(starved)
        m41_tool(
            "set_recipe", {"target_ref": starved["target_ref"], "recipe": "basic-oil-processing"}
        )
        shortage = m41_until(starved, lambda v: v.get("status") == "fluid_ingredient_shortage")
        record(
            "M41 CW starved refinery reports crude-oil missing for one craft",
            shortage.get("status") == "fluid_ingredient_shortage"
            and shortage.get("missing_fluids") == [{"name": "crude-oil", "amount": 100}],
            json.dumps(shortage),
        )
        for compact in (False, True):
            observed = m41_tool("observe_nearby", {"names": ["oil-refinery"], "compact": compact})
            entity = next(
                e for e in observed["entities"] if e["target_ref"] == starved["target_ref"]
            )
            record(
                f"M41 CW observe compact={compact} retains missing_fluids",
                entity.get("missing_fluids") == shortage.get("missing_fluids"),
            )
        # Seed only this isolated diagnostic machine, then clear it before production checks.
        m41_fixture(
            "local e=s.find_entity('oil-refinery',{-2.5,18.5});"
            "e.fluidbox[1]={name='crude-oil',amount=25};rcon.print('m41:{}')"
        )
        partial = m41_read(starved)
        record(
            "M41 CW missing_fluids subtracts available input from one craft",
            partial.get("missing_fluids") == [{"name": "crude-oil", "amount": 75}],
            json.dumps(partial),
        )
        m41_fixture(
            "local e=s.find_entity('oil-refinery',{-2.5,18.5});"
            "e.fluidbox[1]={name='crude-oil',amount=e.fluidbox.get_capacity(1)};"
            "e.fluidbox[2]={name='petroleum-gas',amount=e.fluidbox.get_capacity(2)};"
            "rcon.print('m41:{}')"
        )
        full = m41_until(starved, lambda v: v.get("status") == "full_output")
        observed_full = m41_tool("observe_nearby", {"names": ["oil-refinery"]})
        record(
            "M41 CW full fluid output names appear in inventory and observation",
            full.get("status") == "full_output"
            and full.get("full_fluids") == ["petroleum-gas"]
            and "missing_fluids" not in full
            and any(e.get("full_fluids") == ["petroleum-gas"] for e in observed_full["entities"]),
            json.dumps(full),
        )
        m41_fixture(
            "local e=s.find_entity('oil-refinery',{-2.5,18.5});"
            "e.fluidbox[1]=nil;e.fluidbox[2]=nil;rcon.print('m41:{}')"
        )
        plant = m41_place("chemical-plant", 12.5, 0.5)
        m41_power(plant)
        configured = m41_tool(
            "set_recipe", {"target_ref": plant["target_ref"], "recipe": "plastic-bar"}
        )
        plant_port = next(
            p for p in configured["fluid_connections"] if p.get("fluid") == "petroleum-gas"
        )
        m41_route(gas["target_position"], plant_port["target_position"], -8.5)
        m41_read(plant)
        moved = m41_tool(
            "transfer_item",
            {
                "target_ref": plant["target_ref"],
                "direction": "to_entity",
                "inventory_role": "source",
                "item": "coal",
                "count": 10,
            },
        )
        produced = m41_tool(
            "wait_for_items",
            {
                "target_ref": plant["target_ref"],
                "inventory_role": "result",
                "item": "plastic-bar",
                "count": 2,
                "timeout_seconds": 20,
            },
        )
        record(
            "M41 CV connected petroleum and transferred coal produce plastic-bar",
            moved.get("moved") == 10 and produced.get("reached") is True,
            json.dumps(produced),
        )
        # Snapshot both immediate neighbours, remove a real pipe through mine_entity,
        # then compare references and open-port counts through single/batch reads.
        middle = len(oil_pipes) // 2
        removed_pipe = oil_pipes[middle]
        neighbours = [oil_pipes[middle - 1], oil_pipes[middle + 1]]
        before = [m41_read(p) for p in neighbours]
        m41_read(removed_pipe)
        started = m41_tool("mine_entity", {"target_ref": removed_pipe["target_ref"]})
        finished = m41_tool(
            "wait_for_action", {"action_id": started["action_id"], "timeout_seconds": 10}
        )
        assert finished.get("status") == "succeeded", finished
        after = [m41_read(p) for p in neighbours]
        batch = m41_tool(
            "get_entity_inventories", {"target_refs": [p["target_ref"] for p in neighbours]}
        )["results"]
        record(
            "M41 CX mining middle pipe removes connected_to and increases unconnected_ports",
            all(
                any(
                    c["target_ref"] == removed_pipe["target_ref"]
                    for c in a["fluids"][0]["connected_to"]
                )
                and all(
                    c["target_ref"] != removed_pipe["target_ref"]
                    for c in b["fluids"][0]["connected_to"]
                )
                and b["fluids"][0]["unconnected_ports"] == a["fluids"][0]["unconnected_ports"] + 1
                and all(
                    p.get("connected_to", {}).get("target_ref") != removed_pipe["target_ref"]
                    for p in b["fluid_connections"]
                )
                for a, b in zip(before, after, strict=True)
            )
            and all(entry["ok"] for entry in batch),
            json.dumps({"before": before, "after": after}),
        )
        # Independent water/crude segments. Only the fixture seeds fluids for this
        # focused placement probe; the production chain above uses extracted oil.
        left = m41_place("pipe", 24.5, 12.5)
        right = m41_place("pipe", 26.5, 12.5)
        m41_near(25.5, 12.5)
        native_mixing = m41_fixture(
            "local a=s.find_entity('pipe',{24.5,12.5});"
            "local b=s.find_entity('pipe',{26.5,12.5});"
            "a.fluidbox[1]={name='water',amount=100};"
            "b.fluidbox[1]={name='crude-oil',amount=100};"
            "rcon.print('m41:'..helpers.table_to_json({character=c.can_place_entity{"
            "name='pipe',position={25.5,12.5}},surface=s.can_place_entity{"
            "name='pipe',position={25.5,12.5},force=c.force}}))"
        )
        m41_near(25.5, 12.5)
        args = {"item": "pipe", "surface": m41_surface, "x": 25.5, "y": 12.5}
        inventory_before = bridge.call("get_inventory")["result"]["items"]
        preview = m41_tool("check_placement", args)
        response = asyncio.run(m41_server.call_tool("place_entity", args))
        rejected = json.loads(response.content[0].text)
        record(
            "M41 CX mixed-fluid placement is rejected before item use or fluid loss",
            native_mixing == {"character": False, "surface": True}
            and preview.get("blocked_reason") == "fluid_mixing"
            and not preview.get("placeable")
            and preview.get("fluids") == ["crude-oil", "water"]
            and response.is_error
            and rejected.get("detail", {}).get("reason") == "fluid_mixing"
            and rejected.get("detail", {}).get("fluids") == ["crude-oil", "water"]
            and bool(rejected.get("hint"))
            and bridge.call("get_inventory")["result"]["items"] == inventory_before
            and m41_read(left)["fluids"][0]["name"] == "water"
            and m41_read(right)["fluids"][0]["name"] == "crude-oil",
            json.dumps({"preview": preview, "rejected": rejected}),
        )
        water_end = m41_place("pipe", -40.5, -16.5)
        oil_end = m41_place("pipe", -33.5, -16.5)
        underground = m41_place("pipe-to-ground", -34.5, -16.5, direction=4)
        m41_fixture(
            "s.find_entity('pipe',{-40.5,-16.5}).fluidbox[1]={name='water',amount=100};"
            "s.find_entity('pipe',{-33.5,-16.5}).fluidbox[1]={name='crude-oil',amount=100};"
            "rcon.print('m41:{}')"
        )
        m41_near(-39.5, -16.5)
        underground_args = {
            "item": "pipe-to-ground",
            "surface": m41_surface,
            "x": -39.5,
            "y": -16.5,
            "direction": 12,
        }
        preview = m41_tool("check_placement", underground_args)
        record(
            "M41 CX underground endpoint identifies mixing with a visible distant partner",
            preview.get("blocked_reason") == "fluid_mixing"
            and preview.get("fluids") == ["crude-oil", "water"],
            json.dumps(preview),
        )
        m41_fixture("s.find_entity('pipe',{-40.5,-16.5}).fluidbox[1]=nil;rcon.print('m41:{}')")
        paired = m41_tool("place_entity", underground_args)
        paired_read = m41_read(paired)
        other_read = m41_read(underground)
        record(
            "M41 CX pipe-to-ground reads include the paired endpoint and open-port counts",
            any(
                p.get("connection_type") == "underground"
                and p.get("connected_to", {}).get("target_ref") == underground["target_ref"]
                for p in paired_read["fluid_connections"]
            )
            and all(v["fluids"][0]["unconnected_ports"] == 0 for v in (paired_read, other_read))
            and any(
                c["target_ref"] == oil_end["target_ref"]
                for c in other_read["fluids"][0]["connected_to"]
            )
            and m41_read(water_end)["fluids"][0].get("name") == "crude-oil",
        )
        owned_pipe = m41_place("pipe", 24.5, 20.5)
        m41_place("pipe", 25.5, 20.5)
        m41_fixture("s.find_entity('pipe',{25.5,20.5}).force='neutral';rcon.print('m41:{}')")
        unowned = m41_read(owned_pipe)
        neighbour = next(
            p["connected_to"] for p in unowned["fluid_connections"] if p.get("connected")
        )
        record(
            "M41 CV connected_to includes only the adjacent entity even for another force",
            set(neighbour) == {"target_ref", "name"}
            and neighbour["name"] == "pipe"
            and bool(neighbour["target_ref"])
            and unowned["fluids"][0]["connected_to"] == [neighbour],
        )
    finally:
        for slot in ("action", "craft_action"):
            pending = m41_fixture(
                f"rcon.print('m41:'..helpers.table_to_json({{id=b and state.{slot} "
                f"and state.{slot}.id}}))"
            )
            if pending.get("id"):
                bridge.call("cancel_action", {"action_id": pending["id"]})
        restored = m41_fixture(
            "if b then assert(c.crafting_queue_size==0);c.force.research_queue={};"
            "for n,v in pairs(b.technologies) do local t=c.force.technologies[n];"
            "if t.level~=v.level then t.level=v.level end;"
            "if t.researched~=v.researched then t.researched=v.researched end;"
            "t.enabled=v.enabled end;"
            "for n,enabled in pairs(b.recipes) do c.force.recipes[n].enabled=enabled end;"
            "c.force.research_queue=b.queue;"
            "for n,v in pairs(b.technologies) do "
            "c.force.technologies[n].saved_progress=v.progress end;"
            "if c.force.current_research then c.force.research_progress=b.progress end;"
            "if b.research_enabled then c.force.enable_research() "
            "else c.force.disable_research() end;"
            "for n,v in pairs(b.technologies) do local t=c.force.technologies[n];"
            "assert(t.level==v.level and t.researched==v.researched and t.enabled==v.enabled "
            "and t.saved_progress==v.progress) end;"
            "for n,enabled in pairs(b.recipes) do assert(c.force.recipes[n].enabled==enabled) end;"
            "local queue=c.force.research_queue or {};assert(#queue==#b.queue);"
            "for i,t in ipairs(queue) do assert(t.name==b.queue[i]) end;"
            "assert(c.force.research_progress==b.progress "
            "and c.force.research_enabled==b.research_enabled);"
            "assert(c.teleport(b.position,b.surface));c.selected=b.selected;"
            "if b.stashed then inv.clear() end;"
            "for i=1,#b.inventory do if b.inventory[i].valid_for_read then "
            "assert(inv[i].swap_stack(b.inventory[i])) end end;"
            "for i=1,#b.inventory do assert(not b.inventory[i].valid_for_read) end;"
            "b.inventory.destroy();if s then assert(game.delete_surface(s)) end;"
            "for _,key in ipairs(b.fields) do state[key]=b.saved[key] end;"
            "for _,key in ipairs(b.fields) do assert(state[key]==b.saved[key]) end;"
            "storage.m41_backup=nil;assert(c.crafting_queue_size==0);"
            "assert(c.position.x==b.position.x and c.position.y==b.position.y "
            "and c.surface==b.surface) end;"
            "rcon.print('m41:'..helpers.table_to_json({restored=true}))"
        )
        # Surface deletion completes at the end of the tick, so verify removal
        # in a separate RCON request after the restoration command has completed.
        removed = m41_fixture(
            "rcon.print('m41:'..helpers.table_to_json({backup=b~=nil,surface=s~=nil}))"
        )
        observed_map_cells.pop(m41_surface, None)
        record(
            "M41 restores inventory, queue, position, observations and force recipes/technologies",
            restored.get("restored") is True
            and removed == {"backup": False, "surface": False}
            and bridge.call("get_inventory")["result"]["items"] == m41_inventory
            and bridge.call("get_agent_status")["result"]["position"] == m41_position,
        )

    m42_chunk_generation_checks()

    failed = [label for label, passed, _ in results if not passed]
    if failed:
        raise RuntimeError("Failed checks: " + ", ".join(failed))
    print("NEGATIVE_CHECKS_SUCCEEDED", len(results), flush=True)


if __name__ == "__main__":
    original_call = bridge.call

    def track_observation(operation, params=None):
        response = original_call(operation, params)
        if operation == "observe_nearby":
            remember_map_observation(response["result"])
        return response

    with patch.object(bridge, "call", side_effect=track_observation):
        main()
