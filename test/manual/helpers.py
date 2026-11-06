"""Pure helpers shared by the manual integration scripts."""

import math


def assert_inserter_direction(entity, position, direction):
    """Back the tool wording with actual game pickup/drop sides, for either arm length."""
    axis, sign = {0: ("y", -1), 4: ("x", 1), 8: ("y", 1), 12: ("x", -1)}[direction]
    other = "y" if axis == "x" else "x"
    center = dict(zip(("x", "y"), position, strict=True))
    for field, side in (("pickup_position", sign), ("drop_position", -sign)):
        assert (entity[field][axis] - center[axis]) * side > 0, (direction, field, entity)
        assert abs(entity[field][other] - center[other]) <= 0.01, (direction, field, entity)


def select_automation_technology(available):
    """Choose the cheapest non-trigger research that consumes only red science."""
    candidates = [
        technology
        for technology in available
        if "trigger" not in technology
        and len(technology.get("ingredients", [])) == 1
        and technology["ingredients"][0]["name"] == "automation-science-pack"
    ]
    if not candidates:
        raise ValueError(f"No available non-trigger red-science-only technology: {available}")
    return min(
        candidates,
        key=lambda technology: (
            technology["unit_count"] * technology["unit_time"],
            technology["name"],
        ),
    )


def position_in_tile_footprint(position, bounding_box):
    """Check the position's tile against the entity's whole-tile footprint.

    Drop/pickup positions can fall just outside the collision box while still
    addressing a tile occupied by the entity. Exact box edges exclude the
    adjacent tile, with epsilon absorbing floating point noise at those edges.
    """
    epsilon = 1e-6
    return all(
        math.floor(bounding_box["left_top"][axis] + epsilon)
        <= math.floor(position[axis])
        <= math.ceil(bounding_box["right_bottom"][axis] - epsilon) - 1
        for axis in ("x", "y")
    )


def successful_batch_results(batch, expected_count):
    """Require every ordered batch entry to succeed, including nested errors."""
    entries = batch.get("results", [])
    assert (
        batch.get("succeeded") == expected_count
        and batch.get("failed") == 0
        and len(entries) == expected_count
        and [entry.get("index") for entry in entries] == list(range(1, expected_count + 1))
        and all(entry.get("ok") is True and "result" in entry for entry in entries)
    ), f"Batch did not complete all {expected_count} entries: {batch}"
    return [entry["result"] for entry in entries]
