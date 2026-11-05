"""Manual logistics test for a disposable save on RCON port 27277 only.

Run after production_line.py and negative_checks.py on the same server.
Creates only raw-resource fixtures with /c; all items come from normal actions.
"""

import json
import math
import time

from helpers import position_in_tile_footprint
from rcon.source import Client

from factorio_mcp.bridge import RCON_FRAG_THRESHOLD, BridgeError, parse_bridge_response
from production_line import action, amount, bridge, call, password, reference


def observe():
    observed = bridge.call("observe_nearby")["result"]
    entities = list(observed["entities"] or [])
    offset = observed.get("next_entity_offset")
    while offset is not None:
        page = bridge.call("observe_nearby", {"entity_offset": offset})["result"]
        entities.extend(page["entities"] or [])
        offset = page.get("next_entity_offset")
    observed["entities"] = entities
    return observed


def entity_at(observed, name, position):
    x, y = position
    for entity in observed["entities"]:
        if entity["name"] == name and entity["x"] == x and entity["y"] == y:
            return entity
    raise RuntimeError(f"{name} at {position} not observed")


def tile_center(position):
    return math.floor(position["x"]) + 0.5, math.floor(position["y"]) + 0.5


def place(item, position, direction=0):
    step = "observe"
    try:
        observe()
        x, y = position
        params = {"item": item, "surface": "nauvis", "x": x, "y": y, "direction": direction}
        step = "check_placement"
        checked = call("check_placement", params)
        assert checked["placeable"], f"Cannot place {item} at {position}: {checked}"
        assert checked["has_item"], f"Missing {item} at {position}: {checked}"
        step = "place_entity"
        placed = call("place_entity", params)
        step = "verify_placement"
        entity = entity_at(observe(), item, position)
        if "pickup_position" in entity:
            # The occupied footprint must still return the same preview.
            blocked = call("check_placement", params)
            assert not blocked["placeable"], blocked
            for field in ("pickup_position", "drop_position"):
                for axis in ("x", "y"):
                    for result in (placed, entity, blocked):
                        assert abs(checked[field][axis] - result[field][axis]) <= 0.01, (
                            params,
                            checked,
                            result,
                        )
            print("PASS M21-AV inserter preview, placement and observation", direction, flush=True)
        return entity
    except Exception as error:
        print(
            f"placement failed: step={step}, item={item}, position={position}, "
            f"direction={direction}: {error}",
            flush=True,
        )
        raise


def held_items():
    return {item["name"]: item["count"] for item in bridge.call("get_inventory")["result"]["items"]}


def replay_batch(operation, entries):
    request_id = f"m14v-{operation}-{time.time_ns()}"
    command = "/ai_mcp " + json.dumps(
        {
            "protocol_version": 1,
            "request_id": request_id,
            "agent_id": "agent-1",
            "operation": operation,
            "params": {"items": entries},
        },
        separators=(",", ":"),
    )

    def send():
        raw = bridge.send(bridge.host, bridge.port, bridge.password, command)
        return parse_bridge_response(raw, request_id)

    first = send()
    after = held_items()
    assert send() == first, f"{operation} duplicate request did not return cached response"
    assert held_items() == after, f"{operation} duplicate request changed held items"
    cached = bridge.call("get_request_result", {"request_id": request_id})["result"]
    assert cached["original_response"] == first, f"{operation} result was not cached"
    return first["result"]


def batch_checks():
    # These items were acquired normally above. Recover every temporary placement
    # before building the logistics line, leaving its fuel and materials intact.
    before = held_items()
    assert before.get("coal", 0) >= 2 and before.get("transport-belt", 0) >= 2, before
    furnace_position = (1, 6)
    furnace = place("stone-furnace", furnace_position)
    params = {
        "target_ref": furnace["target_ref"],
        "inventory_role": "fuel",
        "item": "coal",
        "count": 2,
    }
    try:
        target = {"target_ref": furnace["target_ref"]}
        empty = bridge.call("get_entity_inventory", target)["result"]["inventories"]
        held_before = held_items()
        transfers = [
            {**params, "direction": "to_entity"},
            {
                **params,
                "direction": "from_entity",
                "inventory_role": "result",
                "item": "iron-plate",
            },
            {**params, "direction": "from_entity"},
        ]
        moved = replay_batch("transfer_items", transfers)
        assert moved["succeeded"] == 2 and moved["failed"] == 1, moved
        assert [entry["index"] for entry in moved["results"]] == [1, 2, 3], moved
        assert [entry["ok"] for entry in moved["results"]] == [True, False, True], moved
        assert moved["results"][1]["error"] == "missing_materials", moved
        for index in (0, 2):
            assert moved["results"][index]["result"]["moved"] == 2, moved
        assert held_items() == held_before, "Batch transfers changed held items"
        assert bridge.call("get_entity_inventory", target)["result"]["inventories"] == empty
        print("PASS batch transfer runs entries in order and continues after a failure", flush=True)

        # The first entry would mutate inventory if validation ran lazily.
        placement = {
            "item": "transport-belt",
            "surface": "nauvis",
            "x": 1.5,
            "y": 3.5,
            "direction": 4,
        }
        for operation, entry in (("transfer_items", transfers[0]), ("place_entities", placement)):
            for invalid in (None, {}, [], {"first": entry}, [entry] * 11, [entry, False]):
                try:
                    bridge.call(operation, {"items": invalid})
                except BridgeError as error:
                    assert error.code == "invalid_input", (operation, invalid, error)
                else:
                    raise AssertionError(f"{operation} accepted malformed batch: {invalid}")
                assert held_items() == held_before, f"{operation} malformed batch changed items"
                assert bridge.call("get_entity_inventory", target)["result"]["inventories"] == empty
                checked = call("check_placement", placement)
                assert checked["placeable"], f"{operation} malformed batch placed an entity"
        print(
            "PASS batch rejects more than 10 entries and malformed lists without changes",
            flush=True,
        )
    finally:
        action("mine_entity", {"target_ref": furnace["target_ref"], "count": 1})
    assert held_items() == before, "Batch transfer checks changed materials"

    placements = [placement, placement, {**placement, "x": 2.5}]
    placed = replay_batch("place_entities", placements)
    try:
        assert placed["succeeded"] == 2 and placed["failed"] == 1, placed
        assert [entry["index"] for entry in placed["results"]] == [1, 2, 3], placed
        assert [entry["ok"] for entry in placed["results"]] == [True, False, True], placed
        assert placed["results"][1]["error"] == "blocked", placed
        assert held_items().get("transport-belt", 0) == before["transport-belt"] - 2
        observed = observe()
        for entry, position in zip(
            (placed["results"][0], placed["results"][2]), ((1.5, 3.5), (2.5, 3.5)), strict=True
        ):
            entity = entity_at(observed, "transport-belt", position)
            assert entry["result"]["name"] == entity["name"], entry
            assert entry["result"]["direction"] == entity["direction"] == 4, entry
    finally:
        for position in ((1.5, 3.5), (2.5, 3.5)):
            entity = entity_at(observe(), "transport-belt", position)
            action("mine_entity", {"target_ref": entity["target_ref"], "count": 1})
    assert held_items() == before, "Batch placements did not return held items"
    print("PASS batch placement places several entities and recovers their items", flush=True)


def main():
    action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
    before = bridge.call("get_inventory")["result"]["items"]
    for name, position in [("stone-furnace", (6, 4)), ("burner-mining-drill", (6, 6))]:
        target = entity_at(observe(), name, position)
        action("mine_entity", {"target_ref": target["target_ref"], "count": 1})
        remaining = observe()["entities"]
        assert not any(
            entity["name"] == name and (entity["x"], entity["y"]) == position
            for entity in remaining
        ), f"{name} was not picked up"
        after = bridge.call("get_inventory")["result"]["items"]
        assert amount(after, name) == amount(before, name) + 1, f"{name} item not recovered"
    print("pickup ok", flush=True)

    fixture = (
        "/c local s=game.surfaces.nauvis;local n=0;"
        "for _,p in ipairs({{-12.5,10.5},{-11.5,10.5},{-12.5,11.5},{-11.5,11.5}}) do "
        'local e=s.find_entity("iron-ore",p);if not e then '
        'e=s.create_entity{name="iron-ore",position=p,amount=300} end;'
        'if e then n=n+1 end end;rcon.print("fixture:"..n)'
    )
    with Client(
        "127.0.0.1", 27277, passwd=password, timeout=5, frag_threshold=RCON_FRAG_THRESHOLD
    ) as client:
        result = client.run(fixture)
        if "fixture:" not in result:
            result = client.run(fixture)
        print(result.strip(), flush=True)
        assert "fixture:4" in result, f"Iron-ore fixture failed: {result}"

    action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
    action("mine_entity", {"target_ref": reference("coal"), "count": 6})
    items = bridge.call("get_inventory")["result"]["items"]
    gears_needed = max(0, 2 - amount(items, "iron-gear-wheel"))
    plates_needed = 2 * gears_needed + 2  # One plate each for the belt and inserter crafts.
    plates = amount(items, "iron-plate")
    if plates < plates_needed:
        raise RuntimeError(
            f"Insufficient iron plates from pickup: need {plates_needed}, have {plates}"
        )
    if gears_needed:
        action("craft", {"recipe": "iron-gear-wheel", "count": gears_needed})
    action("craft", {"recipe": "transport-belt", "count": 1})
    action("craft", {"recipe": "burner-inserter", "count": 1})

    batch_checks()

    action("move_to", {"surface": "nauvis", "x": -9, "y": 13})
    drill_position = (-12, 11)
    drill = place("burner-mining-drill", drill_position, 4)
    belt1_position = tile_center(drill["drop_position"])
    belt2_position = (belt1_position[0] + 1, belt1_position[1])
    place("transport-belt", belt1_position, 4)
    place("transport-belt", belt2_position, 4)
    inserter_position = (belt2_position[0] + 1, belt2_position[1])
    inserter = place("burner-inserter", inserter_position, 12)
    # Preview all inserter prototypes, including ones outside the build allowlist.
    fast_preview = call(
        "check_placement",
        {
            "item": "fast-inserter",
            "surface": "nauvis",
            "x": inserter_position[0],
            "y": inserter_position[1],
            "direction": 12,
        },
    )
    assert not fast_preview["placeable"] and fast_preview["reason"] == "invalid_target", (
        fast_preview
    )
    # Base fast and burner inserters share the same pickup/drop vectors.
    for field in ("pickup_position", "drop_position"):
        for axis in ("x", "y"):
            assert abs(fast_preview[field][axis] - inserter[field][axis]) <= 0.01, fast_preview
    for _ in range(3):
        if tile_center(inserter["pickup_position"]) == belt2_position:
            break
        call("rotate_entity", {"target_ref": inserter["target_ref"]})
        inserter = entity_at(observe(), "burner-inserter", inserter_position)
    assert tile_center(inserter["pickup_position"]) == belt2_position, inserter
    drop = inserter["drop_position"]
    assert tile_center(drop) == (inserter_position[0] + 1, inserter_position[1]), inserter
    furnace_position = (math.floor(drop["x"]) + 1, math.floor(drop["y"]) + 1)
    furnace = place("stone-furnace", furnace_position)
    assert position_in_tile_footprint(drop, furnace["bounding_box"]), (
        f"logistics verify_drop_tile failed: inserter={inserter_position}, "
        f"drop={drop}, furnace={furnace_position}, bounding_box={furnace['bounding_box']}"
    )

    for name, position, count in [
        ("burner-mining-drill", drill_position, 2),
        ("burner-inserter", inserter_position, 1),
        ("stone-furnace", furnace_position, 2),
    ]:
        target = entity_at(observe(), name, position)
        moved = call(
            "transfer_item",
            {
                "target_ref": target["target_ref"],
                "direction": "to_entity",
                "inventory_role": "fuel",
                "item": "coal",
                "count": count,
            },
        )
        assert moved["moved"] == count, moved

    deadline = time.monotonic() + 120
    next_report = time.monotonic()
    last_statuses = {}
    plates = 0
    while time.monotonic() < deadline:
        observed = observe()
        drill = entity_at(observed, "burner-mining-drill", drill_position)
        inserter = entity_at(observed, "burner-inserter", inserter_position)
        furnace = entity_at(observed, "stone-furnace", furnace_position)
        last_statuses = {
            "drill": drill.get("status"),
            "inserter": inserter.get("status"),
            "furnace": furnace.get("status"),
        }
        result = bridge.call("get_entity_inventory", {"target_ref": furnace["target_ref"]})[
            "result"
        ]
        if result["products_finished"] > 0:
            assert "iron-plate" in result["recipe_products"], result
            assert result["recipe_product_amounts"]["iron-plate"] == 1, result
        plates = amount(result["inventories"]["result"], "iron-plate")
        now = time.monotonic()
        if now >= next_report:
            iron = next(
                (entry for entry in observed["resources"] or [] if entry["name"] == "iron-ore"),
                None,
            )
            belts = [
                entity_at(observed, "transport-belt", position).get("belt_items", [])
                for position in (belt1_position, belt2_position)
            ]
            print(
                "logistics",
                json.dumps(
                    {"iron-ore": iron, "belt_items": belts, "status": last_statuses},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            next_report = now + 5
        if plates >= 5:
            print("LOGISTICS_LINE_SUCCEEDED", plates, flush=True)
            return
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise RuntimeError(f"Only {plates} automated plates; last statuses: {last_statuses}")


if __name__ == "__main__":
    main()
