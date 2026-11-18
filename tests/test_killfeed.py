import unittest

from wardogs.combat import bind_combat_evidence
from wardogs.model import DEFAULT_PROVIDER, ContextModel

KILLER = "76561190000000001"
VICTIM = "76561190000000002"
PLAYERS = [
    {"steamId": KILLER, "name": "Example"},
    {"steamId": VICTIM, "name": "Victim"},
]


def combat(feed="e1", position="e1", **changes):
    c = dict(
        killer_name="Example",
        victim_name="Victim",
        killfeed_ref=feed,
        position_ref=position,
        killer_bbox=[0.05, 0.1, 0.25, 0.15],
        weapon_bbox=[0.3, 0.1, 0.4, 0.15],
        victim_bbox=[0.5, 0.1, 0.7, 0.15],
        position_subject_name="Victim",
        position_subject_role="victim_self",
        victim_outside_yellow=True,
        position_confidence=0.99,
        victim_marker_type="white_ecg_circle",
        victim_marker_bbox=[0.2, 0.5, 0.25, 0.55],
        yellow_boundary_visible=True,
        victim_link_basis="same_frame_death_view",
        victim_link_confidence=0.999,
        killfeed_confidence=0.999,
        position_identity_ref="",
        position_name_bbox=[],
    )
    c.update(changes)
    return c


def result(c=None, **changes):
    d = dict(
        rule_id="outside_combat",
        combat_evidence=c or combat(),
        evidence_refs=["e1", "e2"],
        target_steam_id="",
        identity_evidence=["e1"],
        identity_confidence=0.999,
    )
    d.update(changes)
    return d


class CombatTests(unittest.TestCase):
    def test_single_image_death_view_resolves_killer_not_victim(self):
        d = bind_combat_evidence(result(), {"e1"}, PLAYERS)
        self.assertEqual(d["target_steam_id"], KILLER)
        self.assertEqual(d["combat_evidence"]["victim_name"], "Victim")

    def test_two_images_with_visible_same_victim_identity_can_pass(self):
        c = combat(
            position="e2",
            victim_link_basis="visible_victim_name",
            position_identity_ref="e2",
            position_name_bbox=[0.1, 0.1, 0.3, 0.2],
        )
        self.assertEqual(
            bind_combat_evidence(result(c), {"e1", "e2"}, PLAYERS)["target_steam_id"],
            KILLER,
        )

    def test_two_images_uploaded_by_same_user_are_not_identity_proof(self):
        for basis in [
            "same_uploader",
            "similar_map",
            "same_frame_death_view",
            "unproven",
        ]:
            with self.subTest(basis=basis), self.assertRaises(ValueError):
                bind_combat_evidence(
                    result(combat(position="e2", victim_link_basis=basis)),
                    {"e1", "e2"},
                    PLAYERS,
                )

    def test_map_subject_mismatch_or_missing_name_blocks_two_image_punishment(self):
        for change in [
            dict(position_subject_name="Other"),
            dict(position_subject_name=""),
            dict(position_identity_ref="e1"),
            dict(position_name_bbox=[]),
            dict(victim_link_confidence=0.5),
        ]:
            c = combat(
                position="e2",
                victim_link_basis="visible_victim_name",
                position_identity_ref="e2",
                position_name_bbox=[0.1, 0.1, 0.3, 0.2],
            )
            c.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                bind_combat_evidence(result(c), {"e1", "e2"}, PLAYERS)

    def test_left_right_geometry_and_victim_map_marker_are_required(self):
        bad = [
            dict(killer_bbox=[0.6, 0.1, 0.7, 0.2]),
            dict(weapon_bbox=[]),
            dict(position_subject_role="teammate"),
            dict(victim_outside_yellow=False),
            dict(victim_marker_type="blue_ecg_circle"),
            dict(victim_marker_type="blue_triangle"),
            dict(yellow_boundary_visible=False),
            dict(victim_marker_bbox=[]),
            dict(killfeed_confidence=True),
            dict(victim_link_confidence=float("nan")),
            dict(position_confidence=0.5),
            dict(position_ref="invented"),
            dict(killer_name="Victim", victim_name="Victim"),
        ]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValueError):
                bind_combat_evidence(result(combat(**change)), {"e1", "e2"}, PLAYERS)

    def test_model_cannot_redirect_kick_to_the_victim_id(self):
        with self.assertRaises(ValueError):
            bind_combat_evidence(result(target_steam_id=VICTIM), {"e1"}, PLAYERS)

    def test_exact_roster_resolution_rejects_fuzzy_or_duplicate_names(self):
        for players in [
            [{"steamId": KILLER, "name": "ExampleLonger"}],
            PLAYERS + [{"steamId": "76561190000000003", "name": " EXAMPLE "}],
        ]:
            with self.assertRaises(ValueError):
                bind_combat_evidence(result(), {"e1"}, players)

    def test_minimal_unicode_and_space_normalization_is_allowed(self):
        d = bind_combat_evidence(
            result(combat(killer_name=" EXAMPLE ")), {"e1"}, PLAYERS
        )
        self.assertEqual(d["target_steam_id"], KILLER)

    def test_other_rules_are_not_forced_to_use_a_killfeed(self):
        d = {"rule_id": "cheating", "target_steam_id": KILLER}
        self.assertEqual(bind_combat_evidence(d, set(), PLAYERS), d)


class KillerPositionTests(unittest.TestCase):
    def attacker(self, **changes):
        c = combat(
            position="e2",
            position_subject_role="killer",
            position_subject_name="Example",
            position_identity_ref="e2",
            position_name_bbox=[0.3, 0.4, 0.5, 0.45],
            position_link_basis="visible_killer_name",
            position_link_confidence=0.999,
            position_outside_yellow=True,
            position_marker_type="named_player_marker",
            position_marker_bbox=[0.5, 0.45, 0.55, 0.5],
            marker_name_link_confidence=0.999,
            same_event_basis="visible_event_context",
            same_event_refs=["e1", "e2"],
            same_event_confidence=0.999,
            victim_outside_yellow=False,
            victim_marker_type="unproven",
            victim_marker_bbox=[],
            victim_link_basis="unproven",
            victim_link_confidence=0,
        )
        c.update(changes)
        return result(c)

    def test_killer_outside_victim_inside_or_unknown_resolves_killer(self):
        for victim_outside in [False, True, None]:
            with self.subTest(victim_outside=victim_outside):
                d = bind_combat_evidence(
                    self.attacker(victim_outside_yellow=victim_outside),
                    {"e1", "e2"},
                    PLAYERS,
                )
                self.assertEqual(d["target_steam_id"], KILLER)

    def test_killer_inside_or_unknown_is_not_an_outside_violation(self):
        for value in [False, None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                bind_combat_evidence(
                    self.attacker(position_outside_yellow=value), {"e1", "e2"}, PLAYERS
                )

    def test_unrelated_arrow_or_victim_name_cannot_prove_killer_position(self):
        for change in [
            dict(position_subject_name="Victim"),
            dict(position_subject_name=""),
            dict(position_link_basis="same_uploader"),
            dict(position_marker_type="unproven"),
            dict(marker_name_link_confidence=0.5),
            dict(position_name_bbox=[]),
            dict(position_identity_ref="e1"),
            dict(position_marker_bbox=[]),
            dict(yellow_boundary_visible=False),
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                bind_combat_evidence(self.attacker(**change), {"e1", "e2"}, PLAYERS)

    def test_different_or_unproven_event_cannot_submit_killer(self):
        for change in [
            dict(same_event_basis="same_uploader"),
            dict(same_event_refs=["e1"]),
            dict(same_event_confidence=0.5),
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                bind_combat_evidence(self.attacker(**change), {"e1", "e2"}, PLAYERS)

    def test_killer_branch_cannot_redirect_to_victim(self):
        d = self.attacker()
        d["target_steam_id"] = VICTIM
        with self.assertRaises(ValueError):
            bind_combat_evidence(d, {"e1", "e2"}, PLAYERS)


class FullDeathViewTests(unittest.TestCase):
    def full_frame(self, **changes):
        c = combat(
            outside_basis="complete_death_minimap_no_yellow",
            yellow_boundary_visible=False,
            victim_marker_type="death_view_minimap",
            victim_marker_bbox=[],
            full_death_view_ref="e1",
            full_death_view_confirmed=True,
            full_death_view_confidence=0.999,
            minimap_ref="e1",
            minimap_bbox=[0.01, 0.68, 0.15, 0.96],
            minimap_complete=True,
            minimap_readable=True,
            minimap_confidence=0.999,
            yellow_region_present=False,
            yellow_arc_present=False,
            hz_distance_m=449,
            damage_log_ref="e1",
            damage_log_attacker_name="Example",
            damage_log_bbox=[0.85, 0.67, 0.99, 0.73],
        )
        c.update(changes)
        return result(c)

    def test_complete_death_frame_without_yellow_is_outside_evidence(self):
        d = bind_combat_evidence(self.full_frame(), {"e1"}, PLAYERS)
        self.assertEqual(d["target_steam_id"], KILLER)

    def test_absence_rule_does_not_require_hz_distance_or_white_marker(self):
        d = self.full_frame()
        del d["combat_evidence"]["hz_distance_m"]
        self.assertEqual(
            bind_combat_evidence(d, {"e1"}, PLAYERS)["target_steam_id"], KILLER
        )

    def test_cropped_unreadable_or_unknown_minimap_cannot_prove_absence(self):
        for change in [
            dict(minimap_complete=False),
            dict(minimap_readable=False),
            dict(minimap_confidence=0.5),
            dict(minimap_bbox=[]),
            dict(full_death_view_confirmed=False),
            dict(full_death_view_confidence=0.5),
            dict(full_death_view_ref="e2"),
            dict(minimap_ref="e2"),
            dict(yellow_region_present=None),
            dict(yellow_arc_present=None),
            dict(yellow_region_present=True),
            dict(yellow_arc_present=True),
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                bind_combat_evidence(self.full_frame(**change), {"e1", "e2"}, PLAYERS)

    def test_center_distance_alone_cannot_replace_minimap_evidence(self):
        with self.assertRaises(ValueError):
            bind_combat_evidence(
                result(combat(yellow_boundary_visible=False, hz_distance_m=449)),
                {"e1"},
                PLAYERS,
            )

    def test_full_frame_and_damage_log_must_agree_with_killfeed(self):
        for change in [
            dict(damage_log_attacker_name="Other"),
            dict(damage_log_ref="e2"),
            dict(damage_log_bbox=[]),
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                bind_combat_evidence(self.full_frame(**change), {"e1", "e2"}, PLAYERS)

    def test_absence_in_an_unlinked_separate_map_is_not_full_death_frame(self):
        with self.assertRaises(ValueError):
            bind_combat_evidence(
                self.full_frame(position_ref="e2"), {"e1", "e2"}, PLAYERS
            )


class ReporterIdentityTests(unittest.TestCase):
    def death_view(self, position="e1", **changes):
        c = combat(
            position=position,
            position_subject_name="",
            victim_link_basis="first_person_death_view",
            first_person_death_confirmed=True,
            first_person_death_ref=position,
            first_person_death_basis="death_ui",
            first_person_death_bbox=[0.2, 0.8, 0.8, 0.95],
            first_person_death_confidence=0.999,
        )
        c.update(changes)
        return result(c)

    def nickname_view(self, **changes):
        d = self.death_view(
            position="e2",
            victim_link_basis="reporter_nickname_first_person",
            same_event_basis="visible_event_context",
            same_event_refs=["e1", "e2"],
            same_event_confidence=0.999,
        )
        d["combat_evidence"].update(changes)
        return d

    def test_same_frame_first_person_death_needs_no_repeated_map_name(self):
        d = bind_combat_evidence(self.death_view(), {"e1"}, PLAYERS)
        self.assertEqual(d["target_steam_id"], KILLER)
        self.assertEqual(d["combat_evidence"]["position_subject_name"], "Victim")

    def test_first_person_claim_needs_visible_death_evidence(self):
        for change in [
            dict(first_person_death_confirmed=False),
            dict(first_person_death_confidence=0.5),
            dict(first_person_death_ref="e2"),
            dict(first_person_death_bbox=[]),
            dict(first_person_death_basis="reporter_says_so"),
            dict(position_subject_name="Other"),
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                bind_combat_evidence(self.death_view(**change), {"e1", "e2"}, PLAYERS)

    def test_reporter_nickname_corroborates_own_first_person_pair(self):
        d = bind_combat_evidence(
            self.nickname_view(),
            {"e1", "e2"},
            PLAYERS,
            reporter_context={
                "display_name": " Victim ",
                "own_evidence_refs": ["e1", "e2"],
            },
        )
        self.assertEqual(d["target_steam_id"], KILLER)

    def test_nickname_alone_or_other_authors_images_do_not_bind_victim(self):
        for context in [
            None,
            {},
            {"display_name": "Other", "own_evidence_refs": ["e1", "e2"]},
            {"display_name": "Victim", "own_evidence_refs": ["e1"]},
            {"display_name": "Victim", "own_evidence_refs": []},
        ]:
            with self.subTest(context=context), self.assertRaises(ValueError):
                bind_combat_evidence(
                    self.nickname_view(),
                    {"e1", "e2"},
                    PLAYERS,
                    reporter_context=context,
                )
        for change in [
            dict(first_person_death_confirmed=False),
            dict(same_event_basis="same_uploader"),
            dict(same_event_confidence=0.2),
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                bind_combat_evidence(
                    self.nickname_view(**change),
                    {"e1", "e2"},
                    PLAYERS,
                    reporter_context={
                        "display_name": "Victim",
                        "own_evidence_refs": ["e1", "e2"],
                    },
                )

    def test_different_qq_name_does_not_veto_independent_death_view(self):
        d = bind_combat_evidence(
            self.death_view(),
            {"e1"},
            PLAYERS,
            reporter_context={
                "display_name": "Different QQ Name",
                "own_evidence_refs": ["e1"],
            },
        )
        self.assertEqual(d["target_steam_id"], KILLER)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_step_preview_is_selected_even_when_conversation_model_differs(self):
        calls = []

        class Context:
            async def get_current_chat_provider_id(self, umo):
                raise AssertionError("no fallback")

            async def llm_generate(self, **kw):
                calls.append(kw)
                return type("R", (), {"completion_text": '{"is_report":false}'})()

        self.assertFalse(await ContextModel(Context(), "umo").intent("hello"))
        self.assertEqual(calls[0]["chat_provider_id"], DEFAULT_PROVIDER)
        self.assertEqual(DEFAULT_PROVIDER, "阶越星辰/step-5-preview")
        self.assertIsNone(calls[0]["tools"])


class IntegrationGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_map_images_of_different_victims_never_submit_a_kick(self):
        import base64
        import tempfile
        import time
        from pathlib import Path

        from test_domain import decision
        from wardogs.domain import Identity, Message, Store
        from wardogs.flow import Reports

        class Service:
            def __init__(self):
                self.kicks = 0

            async def players(self):
                return {"players": PLAYERS, "observedAt": time.time()}

            async def kick(self, *args):
                self.kicks += 1
                return {"operationId": "op"}

        class Media:
            async def load(self, url):
                return (
                    "data:image/png;base64," + base64.b64encode(url.encode()).decode()
                )

        class Model:
            async def assess(self, case, images, players, rules):
                refs = list(images)
                graph = combat(
                    feed=refs[0],
                    position=refs[1],
                    position_subject_name="OtherPlayer",
                    victim_link_basis="visible_victim_name",
                    position_identity_ref=refs[1],
                    position_name_bbox=[0.1, 0.1, 0.3, 0.2],
                )
                return decision(
                    observed_at=time.time(),
                    combat_evidence=graph,
                    evidence_refs=refs,
                    identity_evidence=[refs[0]],
                )

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db")
            service = Service()
            reports = Reports(
                store,
                Model(),
                service,
                Media(),
                rules={"outside_combat": {"version": "1", "text": "rule"}},
                auto_execute=True,
            )
            replies = []

            async def notify(text):
                replies.append(text)

            try:
                cid = await reports.ingest(
                    Message(
                        Identity("FixtureBot", "g", "u"),
                        "m",
                        time.time(),
                        "举报",
                        ("https://cdn.example/feed", "https://cdn.example/map"),
                    ),
                    True,
                    notify,
                )
                await reports.finish(cid)
                self.assertEqual(store.get(cid)["status"], "needs_evidence")
                self.assertEqual(service.kicks, 0)
                self.assertTrue(any("同一被击杀玩家" in t for t in replies))
            finally:
                await reports.close()
                store.close()


class KillerFlowTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, linked, full_frame=False):
        import base64
        import tempfile
        import time
        from pathlib import Path

        from test_domain import decision
        from wardogs.domain import Identity, Message, Store
        from wardogs.flow import Reports

        class Service:
            def __init__(self):
                self.targets = []

            async def players(self):
                return {"players": PLAYERS, "observedAt": time.time()}

            async def kick(self, case, d):
                self.targets.append(d["target_steam_id"])
                return {"operationId": "op"}

        class Media:
            async def load(self, url):
                return (
                    "data:image/png;base64," + base64.b64encode(url.encode()).decode()
                )

        class Model:
            async def assess(self, case, images, players, rules):
                refs = list(images)
                if full_frame:
                    c = FullDeathViewTests().full_frame()["combat_evidence"]
                    c.update(
                        killfeed_ref=refs[0],
                        position_ref=refs[0],
                        full_death_view_ref=refs[0],
                        minimap_ref=refs[0],
                        damage_log_ref=refs[0],
                    )
                    if not linked:
                        c["minimap_complete"] = False
                else:
                    c = KillerPositionTests().attacker()["combat_evidence"]
                    c.update(
                        killfeed_ref=refs[0],
                        position_ref=refs[1],
                        position_identity_ref=refs[1],
                        same_event_refs=refs,
                    )
                    if not linked:
                        c["position_subject_name"] = "Other"
                return decision(
                    target_steam_id="",
                    observed_at=time.time(),
                    combat_evidence=c,
                    evidence_refs=refs,
                    identity_evidence=[refs[0]],
                )

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db")
            service = Service()
            reports = Reports(
                store,
                Model(),
                service,
                Media(),
                rules={"outside_combat": {"version": "1", "text": "either outside"}},
                auto_execute=True,
            )
            replies = []

            async def notify(text):
                replies.append(text)

            try:
                cid = await reports.ingest(
                    Message(
                        Identity("FixtureBot", "g", "u"),
                        "m",
                        time.time(),
                        "report",
                        ("https://cdn.example/feed", "https://cdn.example/map"),
                    ),
                    True,
                    notify,
                )
                await reports.finish(cid)
                self.assertEqual(service.targets, [KILLER] if linked else [])
                self.assertEqual(
                    store.get(cid)["status"],
                    "submitted" if linked else "needs_evidence",
                )
                if not linked:
                    self.assertTrue(
                        any(
                            ("完整死亡画面" if full_frame else "圈外标记属于击杀者")
                            in t
                            for t in replies
                        )
                    )
            finally:
                await reports.close()
                store.close()

    async def test_named_killer_outside_submits_existing_action_once(self):
        await self.run_case(True)

    async def test_unrelated_named_marker_requests_evidence_without_action(self):
        await self.run_case(False)

    async def test_full_death_minimap_absence_submits_existing_action(self):
        await self.run_case(True, full_frame=True)

    async def test_incomplete_death_minimap_requests_full_frame(self):
        await self.run_case(False, full_frame=True)


class EventLogTests(unittest.TestCase):
    def log_combat(self, **changes):
        c = combat(
            position=changes.pop("position", "e1"),
            combat_source="event_log",
            combat_log_ref="e1",
            combat_log_bbox=[0.05, 0.7, 0.95, 0.8],
            combat_log_text="Example击杀了Victim。",
            combat_log_event_kind="kill",
            killer_bbox=[],
            weapon_bbox=[],
            victim_bbox=[],
        )
        c.update(changes)
        return c

    def test_explicit_log_kill_resolves_attacker_without_weapon_icon(self):
        d = bind_combat_evidence(result(self.log_combat()), {"e1"}, PLAYERS)
        self.assertEqual(d["target_steam_id"], KILLER)

    def test_log_does_not_bypass_map_or_cross_image_identity(self):
        for changes in [
            {"position": "e2", "victim_link_basis": "same_uploader"},
            {"victim_marker_type": "green_ecg_circle"},
            {"victim_outside_yellow": False},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                bind_combat_evidence(
                    result(self.log_combat(**changes)), {"e1", "e2"}, PLAYERS
                )

    def test_paired_log_and_named_position_require_visible_same_event(self):
        c = self.log_combat(
            position="e2",
            victim_link_basis="visible_victim_name",
            position_identity_ref="e2",
            position_name_bbox=[0.1, 0.1, 0.3, 0.2],
            same_event_basis="visible_event_context",
            same_event_refs=["e1", "e2"],
            same_event_confidence=0.999,
        )
        self.assertEqual(
            bind_combat_evidence(result(c), {"e1", "e2"}, PLAYERS)["target_steam_id"],
            KILLER,
        )
        for change in [
            {"same_event_basis": "same_uploader"},
            {"same_event_refs": ["e1"]},
            {"same_event_confidence": 0.5},
            {"position_subject_name": "Other"},
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                bind_combat_evidence(result(dict(c, **change)), {"e1", "e2"}, PLAYERS)

    def test_log_requires_explicit_consistent_kill_row(self):
        for changes in [
            {"combat_log_text": "Victim击杀了Example。"},
            {"combat_log_text": "Example的SVD对Victim造成了86.24点伤害"},
            {"combat_log_text": "处于Hot Zone - $240"},
            {"combat_log_text": "Other击杀了Victim。"},
            {"combat_log_text": "Example击杀了Other。"},
            {"combat_log_ref": "e2"},
            {"combat_log_bbox": []},
            {"combat_log_event_kind": "damage"},
            {"combat_source": "unknown"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                bind_combat_evidence(
                    result(self.log_combat(**changes)), {"e1", "e2"}, PLAYERS
                )


class SelfViewRangeTests(unittest.TestCase):
    def own_marker(self, **changes):
        c = combat(
            victim_marker_type="self_ecg_view_range",
            victim_link_basis="first_person_death_view",
            first_person_death_confirmed=True,
            first_person_death_ref="e1",
            first_person_death_basis="self_ecg_view_range",
            first_person_death_bbox=[0.2, 0.5, 0.25, 0.55],
            first_person_death_confidence=0.999,
            self_view_range_ref="e1",
            self_view_range_confirmed=True,
            self_view_range_bbox=[0.15, 0.45, 0.3, 0.6],
            self_view_range_confidence=0.999,
        )
        c.update(changes)
        return c

    def test_self_ecg_with_visible_view_range_can_bind_death_position(self):
        d = bind_combat_evidence(result(self.own_marker()), {"e1"}, PLAYERS)
        self.assertEqual(d["target_steam_id"], KILLER)

    def test_color_or_center_alone_and_unrelated_glow_cannot_bind(self):
        for change in [
            {"self_view_range_confirmed": False},
            {"self_view_range_ref": "e2"},
            {"self_view_range_bbox": []},
            {"self_view_range_bbox": [0.7, 0.7, 0.9, 0.9]},
            {"self_view_range_bbox": [0.21, 0.51, 0.24, 0.54]},
            {"self_view_range_confidence": 0.7},
            {"victim_marker_type": "green_ecg_circle"},
            {"first_person_death_bbox": [0.7, 0.7, 0.9, 0.9]},
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                bind_combat_evidence(
                    result(self.own_marker(**change)), {"e1", "e2"}, PLAYERS
                )
