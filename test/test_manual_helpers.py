import unittest

from manual.helpers import position_in_tile_footprint


class TileFootprintTests(unittest.TestCase):
    def test_drill_drop_just_outside_collision_box_occupies_furnace_tile(self):
        for center_x, center_y in [(-21, -12), (6, 4)]:
            with self.subTest(center=(center_x, center_y)):
                box = {
                    "left_top": {"x": center_x - 0.699, "y": center_y - 0.699},
                    "right_bottom": {"x": center_x + 0.699, "y": center_y + 0.699},
                }
                drop = {"x": center_x - 0.5, "y": center_y + 0.703125}
                self.assertGreater(drop["y"], box["right_bottom"]["y"])
                self.assertTrue(position_in_tile_footprint(drop, box))
                self.assertFalse(
                    position_in_tile_footprint({"x": drop["x"], "y": center_y + 1}, box)
                )
                self.assertFalse(
                    position_in_tile_footprint({"x": center_x - 1.01, "y": drop["y"]}, box)
                )

    def test_integer_box_edges_exclude_neighboring_tiles_despite_roundoff(self):
        box = {
            "left_top": {"x": -2 - 1e-9, "y": 3 - 1e-9},
            "right_bottom": {"x": 0 + 1e-9, "y": 5 + 1e-9},
        }
        self.assertTrue(position_in_tile_footprint({"x": -2, "y": 3}, box))
        self.assertTrue(position_in_tile_footprint({"x": -0.01, "y": 4.99}, box))
        for position in [
            {"x": -2.01, "y": 3.5},
            {"x": 0, "y": 3.5},
            {"x": -1.5, "y": 2.99},
            {"x": -1.5, "y": 5},
        ]:
            with self.subTest(position=position):
                self.assertFalse(position_in_tile_footprint(position, box))


if __name__ == "__main__":
    unittest.main()
