"""Manual failure-path checks for a disposable save on RCON port 27277 only.

Run right after production_line.py on the same server: it relies on the
fixture's stone patch and the running stone furnace at (6, 4).
"""

import json
import time

from factorio_mcp.bridge import BridgeError, parse_bridge_response

from production_line import action, bridge, reference


results = []


def record(label, passed, info=""):
    results.append((label, bool(passed), info))
    print(("PASS " if passed else "FAIL ") + label + (f"  [{info}]" if info else ""), flush=True)


def expect_error(label, operation, params, codes):
    try:
        result = bridge.call(operation, params)["result"]
    except BridgeError as error:
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
        "protocol_version": 1, "request_id": request_id, "agent_id": "agent-1",
        "operation": operation, "params": params,
    }
    command = "/ai_mcp " + json.dumps(request, separators=(",", ":"))
    raw = bridge.send(bridge.host, bridge.port, bridge.password, command)
    return parse_bridge_response(raw, request_id)


def main():
    before = items()
    expect_error("craft with missing materials", "craft",
                 {"recipe": "burner-mining-drill", "count": 1}, {"missing_materials"})
    record("failed craft keeps inventory", items() == before)

    # A spare furnace is needed for the occupied-tile check; the drill recipe consumed the last one.
    action("move_to", {"surface": "nauvis", "x": 2, "y": 2.5})
    action("mine_entity", {"target_ref": reference("stone"), "count": 5})
    action("craft", {"recipe": "stone-furnace", "count": 1})
    action("move_to", {"surface": "nauvis", "x": 4, "y": 4})

    # place_entity requires an observation from the last 10 seconds.
    before = items()
    bridge.call("observe_nearby")
    expect_error("place an item not in inventory", "place_entity",
                 {"item": "wooden-chest", "surface": "nauvis", "x": 0, "y": 8, "direction": 0},
                 {"missing_materials"})
    furnaces = count_visible("stone-furnace")
    expect_error("place on an occupied tile", "place_entity",
                 {"item": "stone-furnace", "surface": "nauvis", "x": 6, "y": 4, "direction": 0},
                 {"blocked"})
    record("failed placements keep inventory and entities",
           items() == before and count_visible("stone-furnace") == furnaces,
           f"furnaces {furnaces}")

    expect_error("unknown target_ref", "mine_entity", {"target_ref": "999999", "count": 1},
                 {"target_gone"})

    furnace = reference("stone-furnace")
    request_id = f"negative-resend-{int(time.time())}"
    params = {"target_ref": furnace, "direction": "from_entity",
              "inventory_role": "result", "item": "iron-plate", "count": 1}
    plates = items().get("iron-plate", 0)
    first = send_with_id("transfer_item", params, request_id)
    after_first = items().get("iron-plate", 0)
    second = send_with_id("transfer_item", params, request_id)
    after_second = items().get("iron-plate", 0)
    record("first transfer moves one plate", after_first - plates == 1, f"{plates}->{after_first}")
    record("resent request is not executed again", after_second == after_first,
           f"{after_first}->{after_second}")
    record("resent request returns the cached response", first == second)
    stored = bridge.call("get_request_result", {"request_id": request_id})["result"]
    record("get_request_result returns the original response",
           stored.get("original_response") == first)

    action("move_to", {"surface": "nauvis", "x": 13, "y": 4})
    before = items()
    expect_error("mine out of reach", "mine_entity",
                 {"target_ref": reference("stone"), "count": 1}, {"out_of_range"})
    record("out-of-reach mining keeps inventory", items() == before)

    time.sleep(11)
    expect_error("expired target_ref", "get_entity_inventory", {"target_ref": furnace},
                 {"target_gone"})

    failed = [label for label, passed, _ in results if not passed]
    if failed:
        raise RuntimeError("Failed checks: " + ", ".join(failed))
    print("NEGATIVE_CHECKS_SUCCEEDED", len(results), flush=True)


if __name__ == "__main__":
    main()
