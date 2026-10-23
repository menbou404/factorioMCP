import unittest

from manual.helpers import position_in_tile_footprint, select_automation_technology


class TechnologySelectionTests(unittest.TestCase):
    @staticmethod
    def technology(name, count=10, seconds=15, ingredients=None, **extra):
        return {
            "name": name,
            "unit_count": count,
            "unit_time": seconds,
            "ingredients": (
                [{"name": "automation-science-pack", "amount": 1}]
                if ingredients is None
                else ingredients
            ),
            **extra,
        }

    def test_minimizes_total_unit_time_then_name(self):
        slower = self.technology("a-slower", count=1, seconds=151)
        tied_z = self.technology("z-tied", count=5, seconds=30)
        tied_a = self.technology("a-tied", count=10, seconds=15)
        for available in ([slower, tied_z, tied_a], [tied_a, tied_z, slower]):
            self.assertIs(select_automation_technology(available), tied_a)

    def test_excludes_triggers_empty_and_other_science_ingredients(self):
        valid = self.technology("valid")
        invalid = [
            self.technology(
                "trigger", count=0, trigger={"type": "craft-item", "item": "lab", "count": 1}
            ),
            self.technology("no-ingredients", count=0, ingredients=[]),
            self.technology(
                "green-only", count=1, ingredients=[{"name": "logistic-science-pack", "amount": 1}]
            ),
            self.technology(
                "mixed",
                count=1,
                ingredients=[
                    {"name": "automation-science-pack", "amount": 1},
                    {"name": "logistic-science-pack", "amount": 1},
                ],
            ),
        ]
        self.assertIs(select_automation_technology([*invalid, valid]), valid)
        for available in ([], invalid):
            with self.assertRaisesRegex(ValueError, "No available non-trigger red-science-only"):
                select_automation_technology(available)

    def test_preserves_pack_amount_for_required_pack_calculation(self):
        technology = self.technology(
            "two-packs", count=7, ingredients=[{"name": "automation-science-pack", "amount": 2}]
        )
        chosen = select_automation_technology([technology])
        self.assertEqual(chosen["unit_count"] * chosen["ingredients"][0]["amount"], 14)


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
