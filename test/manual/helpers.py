"""Pure geometry helpers shared by the manual integration scripts."""

import math


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
