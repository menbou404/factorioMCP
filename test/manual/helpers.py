"""Pure helpers shared by the manual integration scripts."""

import math


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
