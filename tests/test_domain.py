import tempfile
import unittest
from pathlib import Path

from wardogs.domain import Buffer, Identity, Message, Store, validate_decision

ID = "76561190000000001"
WHO = Identity("FixtureBot", "g", "u")


def message(mid, at=100, who=WHO, **kwargs):
    return Message(who, mid, at, **kwargs)


def combat_fixture(ref="e1"):
    return dict(
        killer_name="Example",
        victim_name="Victim",
        killfeed_ref=ref,
        position_ref=ref,
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


def decision(**changes):
    d = dict(
        combat_evidence=combat_fixture(),
        verdict="substantiated",
        rule_id="outside_combat",
        rule_version="1",
        target_steam_id=ID,
        identity_confidence=0.999,
        violation_confidence=0.99,
        evidence_refs=["e1"],
        observed_at=100,
        identity_evidence=["e1"],
        observed_facts=["target shown firing outside marked boundary"],
        missing_facts=[],
        contradictions=[],
        reason="黄圈外杀人",
        english="Combat outside the marked zone",
        japanese="区域外での戦闘",
    )
    d.update(changes)
    return d


class BufferTests(unittest.TestCase):
    def test_prior_and_following_only_same_sender_and_group(self):
        b = Buffer()
        b.add(message("prior", at=90, images=("https://cdn.example/a",)))
        b.add(
            message(
                "other",
                who=Identity("FixtureBot", "g", "v"),
                images=("https://cdn.example/b",),
            )
        )
        b.add(
            message(
                "elsewhere",
                who=Identity("FixtureBot", "x", "u"),
                images=("https://cdn.example/c",),
            )
        )
        report = message("report", at=100, text="举报玩家")
        b.add(report)
        self.assertEqual([m.id for m in b.previous(report)], ["prior"])

    def test_explicit_quote_can_reference_other_sender_but_not_group(self):
        b = Buffer()
        b.add(
            message(
                "photo",
                who=Identity("FixtureBot", "g", "v"),
                images=("https://cdn.example/a",),
            )
        )
        quote = message("report", reply_to="photo")
        self.assertEqual(b.quoted(quote).id, "photo")
        self.assertIsNone(
            b.quoted(
                message("cross", who=Identity("FixtureBot", "x", "u"), reply_to="photo")
            )
        )

    def test_expiry_dedupe_and_limit(self):
        b = Buffer(per_sender=2)
        self.assertTrue(b.add(message("a", at=1)))
        self.assertFalse(b.add(message("a", at=1)))
        b.add(message("b", at=2))
        b.add(message("c", at=3))
        self.assertEqual([m.id for m in b.previous(message("d", at=4))], ["b", "c"])
        self.assertEqual(b.previous(message("e", at=200)), [])


class DecisionTests(unittest.TestCase):
    def test_supported_evidence_and_exact_identity_can_pass(self):
        self.assertEqual(
            validate_decision(decision(), {"e1"}, {ID}, {"outside_combat": "1"}, 105)[
                "target_steam_id"
            ],
            ID,
        )

    def test_high_scores_do_not_replace_proof(self):
        bad = [
            dict(evidence_refs=["invented"]),
            dict(identity_evidence=[]),
            dict(target_steam_id="123"),
            dict(violation_confidence=True),
            dict(violation_confidence=float("nan")),
            dict(contradictions=["unclear zone"]),
            dict(missing_facts=["identity"]),
            dict(observed_at=999),
            dict(observed_at=0),
            dict(rule_version="old"),
            dict(rule_id="undefined"),
            dict(verdict="insufficient"),
            dict(english=""),
            dict(evidence_refs=[]),
            dict(identity_confidence=0.5),
        ]
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_decision(
                    decision(**changes), {"e1"}, {ID}, {"outside_combat": "1"}, 105
                )

    def test_cheating_requires_enabled_rule_and_specific_evidence(self):
        d = decision(rule_id="cheating")
        self.assertEqual(
            validate_decision(d, {"e1"}, {ID}, {"cheating": "1"}, 105)["rule_id"],
            "cheating",
        )


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.db"
        self.s = Store(self.path)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_platform_message_replay_returns_same_case(self):
        a = self.s.create(message("r"), "report")
        b = self.s.create(message("r"), "report")
        self.assertEqual(a, b)
        self.assertEqual(self.s.get(a)["status"], "collecting")

    def test_execution_claim_is_atomic_persistent_and_unknown_after_restart(self):
        cid = self.s.create(message("r"), "report")
        self.s.update(cid, status="eligible", target=ID)
        self.assertTrue(self.s.claim(cid))
        self.assertFalse(self.s.claim(cid))
        self.s.close()
        self.s = Store(self.path)
        self.assertEqual(self.s.get(cid)["status"], "outcome_unknown")
        self.assertFalse(self.s.claim(cid))

    def test_case_visibility_is_owner_and_group_scoped(self):
        cid = self.s.create(message("r"), "report")
        self.assertIsNotNone(self.s.visible(cid, WHO))
        self.assertIsNone(self.s.visible(cid, Identity("FixtureBot", "g", "v")))
        self.assertIsNone(
            self.s.visible(cid, Identity("FixtureBot", "x", "u"), admin=True)
        )

    def test_accepted_backend_operation_remains_locked(self):
        a = self.s.create(message("a"), "report")
        b = self.s.create(message("b"), "report")
        self.s.update(a, status="submitted", target=ID)
        self.s.update(b, status="eligible", target=ID)
        self.assertFalse(self.s.claim(b))

    def test_target_lock_blocks_parallel_or_unknown_cases(self):
        a = self.s.create(message("a"), "report")
        b = self.s.create(message("b"), "report")
        for cid in [a, b]:
            self.s.update(cid, status="eligible", target=ID)
        self.assertTrue(self.s.claim(a))
        self.assertFalse(self.s.claim(b))
