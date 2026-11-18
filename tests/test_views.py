import base64
import io
import unittest

from PIL import Image

from wardogs.views import normalize_view_refs, prepare_views


def picture(w, h):
    image = Image.new("RGB", (w, h), (10, 30, 20))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


class ViewTests(unittest.TestCase):
    def test_tall_capture_keeps_original_and_bounded_hud_details(self):
        views, meta = prepare_views({"frame": picture(520, 914)})
        self.assertIn("frame", views)
        self.assertIn("frame:killfeed", views)
        self.assertIn("frame:map", views)
        self.assertEqual(meta["frame:map"]["source_ref"], "frame")
        self.assertEqual(meta["frame"]["crop"], [0, 0, 1, 1])
        for data in views.values():
            img = Image.open(io.BytesIO(base64.b64decode(data.split(",", 1)[1])))
            self.assertLessEqual(max(img.size), 1600)

    def test_phone_log_detail_includes_lower_rows_and_maps_to_original(self):
        views, meta = prepare_views({"phone": picture(1080, 2358)})
        crop = meta["phone:log"]["crop"]
        self.assertEqual(crop[0], 0)
        self.assertEqual(crop[2], 1)
        self.assertLess(crop[1], 0.6)
        self.assertGreater(crop[3], 0.65)
        d = normalize_view_refs(
            {
                "combat_evidence": {
                    "combat_log_ref": "phone:log",
                    "combat_log_bbox": [0.1, 0.2, 0.9, 0.4],
                }
            },
            meta,
        )["combat_evidence"]
        self.assertEqual(d["combat_log_ref"], "phone")
        self.assertAlmostEqual(
            d["combat_log_bbox"][1], crop[1] + 0.2 * (crop[3] - crop[1])
        )
        self.assertIn("phone", views)

    def test_landscape_death_frame_keeps_full_view_and_readable_hud_details(self):
        views, meta = prepare_views({"frame": picture(1920, 1080)})
        self.assertEqual(
            set(views), {"frame", "frame:killfeed", "frame:map", "frame:damage"}
        )
        self.assertEqual(meta["frame"]["crop"], [0, 0, 1, 1])
        # Representative HUD extents are contained by the deterministic crops.
        for ref, extent in [
            ("frame:map", [0.016, 0.69, 0.15, 0.961]),
            ("frame:killfeed", [0.016, 0.46, 0.187, 0.48]),
            ("frame:damage", [0.86, 0.68, 0.98, 0.73]),
        ]:
            crop = meta[ref]["crop"]
            self.assertLessEqual(crop[0], extent[0])
            self.assertLessEqual(crop[1], extent[1])
            self.assertGreaterEqual(crop[2], extent[2])
            self.assertGreaterEqual(crop[3], extent[3])

    def test_full_frame_minimap_and_damage_refs_map_to_same_source(self):
        _, meta = prepare_views({"frame": picture(1920, 1080)})
        c = normalize_view_refs(
            {
                "combat_evidence": {
                    "full_death_view_ref": "frame",
                    "minimap_ref": "frame:map",
                    "minimap_bbox": [0.1, 0.1, 0.8, 0.8],
                    "damage_log_ref": "frame:damage",
                    "damage_log_bbox": [0.1, 0.1, 0.9, 0.9],
                }
            },
            meta,
        )["combat_evidence"]
        for key in ["full_death_view_ref", "minimap_ref", "damage_log_ref"]:
            self.assertEqual(c[key], "frame")
        self.assertAlmostEqual(c["minimap_bbox"][0], 0.1 * 422 / 1920)
        self.assertAlmostEqual(c["damage_log_bbox"][0], 0.8 + 0.1 * 0.2)

    def test_feed_and_map_images_remain_separate_sources(self):
        views, meta = prepare_views(
            {"feed": picture(216, 45), "map": picture(545, 439)}
        )
        self.assertEqual(set(views), {"feed", "map"})
        self.assertNotEqual(meta["feed"]["source_ref"], meta["map"]["source_ref"])

    def test_coordinates_and_references_map_back_to_original_image(self):
        _, meta = prepare_views({"frame": picture(520, 914)})
        d = {
            "evidence_refs": ["frame:map", "frame:killfeed"],
            "identity_evidence": ["frame:killfeed"],
            "combat_evidence": {
                "killfeed_ref": "frame:killfeed",
                "position_ref": "frame:map",
                "position_identity_ref": "",
                "killer_bbox": [0, 0.1, 0.2, 0.2],
                "weapon_bbox": [0.3, 0.1, 0.4, 0.2],
                "victim_bbox": [0.5, 0.1, 0.7, 0.2],
                "victim_marker_bbox": [0.2, 0.2, 0.3, 0.3],
                "first_person_death_ref": "frame:map",
                "first_person_death_bbox": [0.2, 0.2, 0.3, 0.3],
                "position_name_bbox": [],
            },
        }
        result = normalize_view_refs(d, meta)
        self.assertEqual(result["evidence_refs"], ["frame"])
        self.assertEqual(result["combat_evidence"]["position_ref"], "frame")
        crop = meta["frame:map"]["crop"]
        expected_y = crop[1] + 0.2 * (crop[3] - crop[1])
        self.assertAlmostEqual(
            result["combat_evidence"]["victim_marker_bbox"][1], expected_y
        )
        self.assertEqual(result["combat_evidence"]["first_person_death_ref"], "frame")
        self.assertAlmostEqual(
            result["combat_evidence"]["first_person_death_bbox"][1], expected_y
        )
        self.assertIn("derived_views_used", result)

    def test_killer_marker_name_and_event_refs_keep_source_coordinates(self):
        _, meta = prepare_views({"frame": picture(520, 914)})
        d = normalize_view_refs(
            {
                "combat_evidence": {
                    "position_ref": "frame:map",
                    "position_identity_ref": "frame:map",
                    "self_view_range_ref": "frame:map",
                    "self_view_range_bbox": [0.1, 0.1, 0.4, 0.4],
                    "position_marker_bbox": [0.2, 0.2, 0.3, 0.3],
                    "position_name_bbox": [0.1, 0.1, 0.2, 0.2],
                    "same_event_refs": ["frame:killfeed", "frame:map"],
                }
            },
            meta,
        )["combat_evidence"]
        crop = meta["frame:map"]["crop"]
        self.assertAlmostEqual(d["position_marker_bbox"][0], 0.2 * crop[2])
        self.assertAlmostEqual(
            d["position_name_bbox"][1], crop[1] + 0.1 * (1 - crop[1])
        )
        self.assertEqual(d["same_event_refs"], ["frame"])
        self.assertEqual(d["position_identity_ref"], "frame")
        self.assertEqual(d["self_view_range_ref"], "frame")
        self.assertAlmostEqual(d["self_view_range_bbox"][0], 0.1 * crop[2])

    def test_unknown_view_is_not_promoted_to_valid_evidence(self):
        d = normalize_view_refs(
            {"evidence_refs": ["invented"], "combat_evidence": {}}, {}
        )
        self.assertEqual(d["evidence_refs"], ["invented"])

    def test_oversized_pixel_dimensions_are_rejected_before_processing(self):
        with self.assertRaises(ValueError):
            prepare_views({"frame": picture(4001, 4001)})


class ModelViewIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_model_view_references_are_normalized_before_target_binding(self):
        import json

        from test_domain import ID, combat_fixture, decision
        from wardogs.combat import bind_combat_evidence
        from wardogs.model import ContextModel

        calls = []

        class Context:
            async def llm_generate(self, **kwargs):
                calls.append(kwargs)
                payload = json.loads(kwargs["prompt"])
                self_meta = payload["image_views"]
                self.assertion = bool(self_meta["frame:map"]["source_ref"] == "frame")
                c = combat_fixture("frame:killfeed")
                c["position_ref"] = "frame:map"
                d = decision(
                    target_steam_id="",
                    evidence_refs=["frame:killfeed", "frame:map"],
                    identity_evidence=["frame:killfeed"],
                    combat_evidence=c,
                )
                return type("Response", (), {"completion_text": json.dumps(d)})()

        ctx = Context()
        d = await ContextModel(ctx, "scope").assess(
            {"description": "举报"},
            {"frame": picture(520, 914)},
            [{"name": "Example", "steamId": ID}],
            {"outside_combat": {"version": "1", "text": "rule"}},
        )
        bound = bind_combat_evidence(d, {"frame"}, [{"name": "Example", "steamId": ID}])
        self.assertEqual(bound["target_steam_id"], ID)
        self.assertEqual(d["combat_evidence"]["position_ref"], "frame")
        self.assertEqual(len(calls[0]["image_urls"]), 4)
        self.assertTrue(ctx.assertion)
