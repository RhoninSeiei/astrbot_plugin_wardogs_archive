import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from test_domain import ID, combat_fixture, decision
from wardogs.domain import Identity, Message, Store
from wardogs.flow import Reports


class Model:
    def __init__(self):
        self.changes = {}
        self.calls = 0

    async def assess(self, case, images, players, rules):
        self.calls += 1
        return decision(
            combat_evidence=combat_fixture(next(iter(images))),
            observed_at=time.time(),
            evidence_refs=list(images),
            identity_evidence=list(images),
            **self.changes,
        )


class Service:
    def __init__(self):
        self.kicks = []
        self.error = False

    async def players(self):
        return {
            "players": [{"steamId": ID, "name": "Example"}],
            "observedAt": time.time(),
        }

    async def kick(self, case, d):
        self.kicks.append((case, d))
        if self.error:
            raise TimeoutError()
        return {"operationId": "op1", "status": "accepted"}

    async def operation(self, oid):
        return {
            "operationId": oid,
            "kickStatus": "confirmed",
            "announcementStatus": "queued",
        }


class Media:
    async def load(self, url):
        return "data:image/png;base64,example"


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.tmp.name) / "db")
        self.model = Model()
        self.service = Service()
        self.replies = []
        self.r = Reports(
            self.s,
            self.model,
            self.service,
            Media(),
            rules={
                "outside_combat": {"version": "1", "text": "No combat outside boundary"}
            },
            auto_execute=True,
        )

    async def asyncTearDown(self):
        await self.r.close()
        self.s.close()
        self.tmp.cleanup()

    async def reply(self, text):
        self.replies.append(text)

    def m(self, mid="r", **kw):
        return Message(Identity("FixtureBot", "g", "u"), mid, time.time(), **kw)

    async def test_first_person_death_derives_victim_and_keeps_qq_ids_out_of_model(
        self,
    ):
        captured = []

        class DeathModel:
            async def assess(self, case, images, players, rules):
                captured.append(case)
                ref = next(iter(images))
                c = combat_fixture(ref)
                c.update(
                    position_subject_name="",
                    victim_link_basis="first_person_death_view",
                    first_person_death_confirmed=True,
                    first_person_death_ref=ref,
                    first_person_death_basis="death_ui",
                    first_person_death_bbox=[0.2, 0.8, 0.8, 0.95],
                    first_person_death_confidence=0.999,
                )
                return decision(
                    combat_evidence=c,
                    observed_at=time.time(),
                    evidence_refs=list(images),
                    identity_evidence=[ref],
                )

        self.r.model = DeathModel()
        m = Message(
            Identity("FixtureBot", "private-group-openid", "private-member-openid"),
            "death",
            time.time(),
            "举报",
            ("https://cdn.example/death",),
            sender_nickname="Victim",
        )
        cid = await self.r.ingest(m, True, self.reply)
        await self.r.finish(cid)
        self.assertEqual(len(self.service.kicks), 1)
        self.assertEqual(
            self.s.get(cid)["decision"]["combat_evidence"]["position_subject_name"],
            "Victim",
        )
        context = captured[0]["reporter_context"]
        self.assertEqual(context["display_name"], "Victim")
        self.assertEqual(self.s.get(cid)["reporter_context"], context)
        self.assertEqual(
            set(context["own_evidence_refs"]), set(self.s.get(cid)["evidence_ids"])
        )
        import json

        encoded = json.dumps(captured)
        self.assertNotIn("private-member-openid", encoded)
        self.assertNotIn("private-group-openid", encoded)

    async def nickname_quote_case(self, known_author):
        import base64

        class UniqueMedia:
            async def load(self, url):
                return (
                    "data:image/png;base64," + base64.b64encode(url.encode()).decode()
                )

        captured = []

        class NicknameModel:
            async def assess(self, case, images, players, rules):
                captured.append(case)
                refs = list(images)
                c = combat_fixture(refs[0])
                c.update(
                    position_ref=refs[1],
                    position_subject_name="",
                    victim_link_basis="reporter_nickname_first_person",
                    first_person_death_confirmed=True,
                    first_person_death_ref=refs[1],
                    first_person_death_basis="white_self_death_marker",
                    first_person_death_bbox=[0.2, 0.5, 0.25, 0.55],
                    first_person_death_confidence=0.999,
                    same_event_basis="visible_event_context",
                    same_event_refs=refs,
                    same_event_confidence=0.999,
                )
                return decision(
                    combat_evidence=c,
                    observed_at=time.time(),
                    evidence_refs=refs,
                    identity_evidence=[refs[0]],
                )

        self.r.media = UniqueMedia()
        self.r.model = NicknameModel()
        if known_author is True:
            q = Message(
                Identity("FixtureBot", "g", "other"),
                "quoted",
                time.time(),
                images=("https://cdn.example/other-map",),
                sender_nickname="Other",
            )
            await self.r.ingest(q, False, self.reply)
        if known_author is None:
            report = self.m(
                images=("https://cdn.example/feed", "https://cdn.example/other-map"),
                sender_nickname="Victim",
            )
        else:
            report = self.m(
                images=("https://cdn.example/feed",),
                reply_to="quoted",
                reply_images=() if known_author else ("https://cdn.example/other-map",),
                sender_nickname="Victim",
            )
        cid = await self.r.ingest(report, True, self.reply)
        await self.r.finish(cid)
        direct = known_author is None
        self.assertEqual(
            len(captured[0]["reporter_context"]["own_evidence_refs"]),
            2 if direct else 1,
        )
        self.assertEqual(len(self.service.kicks), 1 if direct else 0)
        self.assertEqual(
            self.s.get(cid)["status"], "submitted" if direct else "needs_evidence"
        )

    async def test_reporter_nickname_with_own_first_person_pair_submits_killer(self):
        await self.nickname_quote_case(None)

    async def test_other_authors_quoted_image_cannot_borrow_reporter_nickname(self):
        await self.nickname_quote_case(True)

    async def test_unknown_quoted_image_cannot_borrow_reporter_nickname(self):
        await self.nickname_quote_case(False)

    async def test_normal_report_previous_image_and_exactly_one_submission(self):
        await self.r.ingest(
            self.m("image", images=("https://cdn.example/a",)), False, self.reply
        )
        cid = await self.r.ingest(
            self.m(text="举报 Example 黄圈外杀人"), True, self.reply
        )
        await self.r.finish(cid)
        self.assertEqual(len(self.service.kicks), 1)
        self.assertEqual(self.s.get(cid)["status"], "submitted")
        await self.r.finish(cid)
        self.assertEqual(len(self.service.kicks), 1)

    async def test_following_image_and_inline_quote(self):
        for quoted in [False, True]:
            cid = await self.r.ingest(
                self.m(
                    "r" + str(quoted),
                    text="举报",
                    reply_images=("https://cdn.example/q",) if quoted else (),
                ),
                True,
                self.reply,
            )
            if not quoted:
                await self.r.ingest(
                    self.m("follow", images=("https://cdn.example/f",)),
                    False,
                    self.reply,
                )
            await self.r.finish(cid)
            self.assertNotEqual(self.s.get(cid)["status"], "needs_evidence")

    async def test_other_sender_image_not_used(self):
        cid = await self.r.ingest(self.m(text="举报"), True, self.reply)
        await self.r.ingest(
            Message(
                Identity("FixtureBot", "g", "v"),
                "v",
                time.time(),
                images=("https://cdn.example/a",),
            ),
            False,
            self.reply,
        )
        await self.r.finish(cid)
        self.assertEqual(self.s.get(cid)["status"], "needs_evidence")
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(self.service.kicks, [])

    async def test_duplicate_platform_message_never_creates_second_submission(self):
        m = self.m(text="举报", images=("https://cdn.example/a",))
        cid = await self.r.ingest(m, True, self.reply)
        self.assertIsNone(await self.r.ingest(m, True, self.reply))
        await self.r.finish(cid)
        self.assertEqual(len(self.service.kicks), 1)

    async def test_ambiguous_two_cases_do_not_share_followup_images(self):
        a = await self.r.ingest(self.m("a", text="举报 A"), True, self.reply)
        b = await self.r.ingest(self.m("b", text="举报 B"), True, self.reply)
        await self.r.ingest(
            self.m("pic", images=("https://cdn.example/a",)), False, self.reply
        )
        await self.r.finish(a)
        await self.r.finish(b)
        self.assertEqual(self.service.kicks, [])

    async def test_text_only_high_score_or_invented_evidence_cannot_execute(self):
        self.model.changes = {"missing_facts": ["zone location"]}
        cid = await self.r.ingest(
            self.m(text="举报", images=("https://cdn.example/a",)), True, self.reply
        )
        await self.r.finish(cid)
        self.assertEqual(self.s.get(cid)["status"], "needs_evidence")
        self.assertEqual(self.service.kicks, [])

    async def test_execution_timeout_is_unknown_and_not_replayed(self):
        self.service.error = True
        cid = await self.r.ingest(
            self.m(text="举报", images=("https://cdn.example/a",)), True, self.reply
        )
        await self.r.finish(cid)
        await self.r.finish(cid)
        self.assertEqual(self.s.get(cid)["status"], "outcome_unknown")
        self.assertEqual(len(self.service.kicks), 1)

    async def event_log_case(self, at, self_range=False, range_confirmed=True):
        class LogModel:
            async def assess(self, case, images, players, rules):
                ref = next(iter(images))
                c = combat_fixture(ref)
                c.update(
                    combat_source="event_log",
                    combat_log_ref=ref,
                    combat_log_bbox=[0.1, 0.6, 0.9, 0.7],
                    combat_log_text="Example击杀了Victim。",
                    combat_log_event_kind="kill",
                    killer_bbox=[],
                    weapon_bbox=[],
                    victim_bbox=[],
                )
                if self_range:
                    c.update(
                        victim_marker_type="self_ecg_view_range",
                        self_view_range_ref=ref,
                        self_view_range_confirmed=range_confirmed,
                        self_view_range_bbox=[0.15, 0.45, 0.3, 0.6],
                        self_view_range_confidence=0.999,
                    )
                return decision(
                    combat_evidence=c,
                    observed_at=at,
                    evidence_refs=[ref],
                    identity_evidence=[ref],
                )

        self.r.model = LogModel()
        cid = await self.r.ingest(
            self.m(text="举报", images=("https://cdn.example/a",)), True, self.reply
        )
        await self.r.finish(cid)
        return self.s.get(cid)

    async def test_event_log_normal_path_submits_fixed_attacker(self):
        case = await self.event_log_case(time.time())
        self.assertEqual(case["status"], "submitted")
        self.assertEqual(len(self.service.kicks), 1)
        self.assertEqual(self.service.kicks[0][1]["target_steam_id"], ID)

    async def test_event_log_with_self_ecg_view_range_submits_attacker(self):
        case = await self.event_log_case(time.time(), self_range=True)
        self.assertEqual(case["status"], "submitted")
        self.assertEqual(self.service.kicks[0][1]["target_steam_id"], ID)

    async def test_event_log_with_unconfirmed_view_range_requests_evidence(self):
        case = await self.event_log_case(
            time.time(), self_range=True, range_confirmed=False
        )
        self.assertEqual(case["status"], "needs_evidence")
        self.assertEqual(self.service.kicks, [])

    async def test_event_log_missing_real_event_time_requests_evidence(self):
        case = await self.event_log_case(0)
        self.assertEqual(case["status"], "needs_evidence")
        self.assertEqual(self.service.kicks, [])

    async def test_disabled_execution_still_assesses_without_kicking(self):
        self.r.auto_execute = False
        cid = await self.r.ingest(
            self.m(text="举报", images=("https://cdn.example/a",)), True, self.reply
        )
        await self.r.finish(cid)
        self.assertEqual(self.s.get(cid)["status"], "eligible")
        self.assertEqual(self.service.kicks, [])

    async def test_unresolved_quote_must_request_evidence(self):
        cid = await self.r.ingest(
            self.m(text="举报", reply_to="not-delivered"), True, self.reply
        )
        await self.r.finish(cid)
        self.assertEqual(self.s.get(cid)["status"], "needs_evidence")
        self.assertTrue(any("图片" in x for x in self.replies))

    async def test_close_cancels_pending_collection(self):
        cid = await self.r.ingest(self.m(text="举报"), True, self.reply)
        await self.r.close()
        self.assertFalse(self.r.tasks)
        self.assertEqual(self.s.get(cid)["status"], "needs_evidence")

    async def test_late_explicit_supplement_reassesses_same_case(self):
        cid = await self.r.ingest(self.m(text="举报"), True, self.reply)
        await self.r.finish(cid)
        self.assertEqual(self.s.get(cid)["status"], "needs_evidence")
        self.assertTrue(
            await self.r.supplement(
                cid, self.m("late", images=("https://cdn.example/a",)), self.reply
            )
        )
        await self.r.finish(cid)
        self.assertEqual(len(self.service.kicks), 1)
        self.assertEqual(self.s.get(cid)["evidence_version"], 2)
        self.assertFalse(await self.r.supplement(cid, self.m("again"), self.reply))

    async def test_followup_evidence_is_not_reused_for_another_target(self):
        a = await self.r.ingest(self.m("a", text="举报 A"), True, self.reply)
        await self.r.ingest(
            self.m("pic", images=("https://cdn.example/a",)), False, self.reply
        )
        b = await self.r.ingest(self.m("b", text="举报 B"), True, self.reply)
        await self.r.finish(a)
        await self.r.finish(b)
        self.assertEqual(self.s.get(b)["status"], "needs_evidence")

    async def test_local_evidence_archive_dedupes_content_and_has_no_urls(self):
        cid = await self.r.ingest(
            self.m(text="举报", images=("https://cdn.example/a",)), True, self.reply
        )
        await self.r.finish(cid)
        c = self.s.get(cid)
        self.assertTrue(c["evidence_ids"])
        files = list((self.s.evidence_root / cid).glob("*"))
        self.assertTrue(files)
        self.assertNotIn("cdn.example", files[0].read_text())

    async def test_confirmed_kick_keeps_partial_announcements_separate(self):
        cid = await self.r.ingest(
            self.m(text="举报", images=("https://cdn.example/a",)), True, self.reply
        )
        await self.r.finish(cid)

        async def operation(_):
            return {"kickStatus": "confirmed", "announcementStatus": "partial"}

        self.service.operation = operation
        text = await self.r.progress(cid, self.m().who)
        self.assertIn("踢出已确认", text)
        self.assertEqual(self.s.get(cid)["status"], "kicked")
        self.assertEqual(len(self.service.kicks), 1)

    async def test_offline_result_does_not_claim_a_successful_kick(self):
        cid = await self.r.ingest(
            self.m(text="举报", images=("https://cdn.example/a",)), True, self.reply
        )
        await self.r.finish(cid)

        async def operation(_):
            return {"kickStatus": "offline", "announcementStatus": "not_queued"}

        self.service.operation = operation
        await self.r.progress(cid, self.m().who)
        self.assertEqual(self.s.get(cid)["status"], "target_offline")

    async def test_real_collection_deadline_fires_without_manual_finish(self):
        self.r.wait = 5
        cid = await self.r.ingest(
            self.m(text="举报", images=("https://cdn.example/a",)), True, self.reply
        )
        await asyncio.sleep(5.1)
        self.assertEqual(self.s.get(cid)["status"], "submitted")
        self.assertEqual(len(self.service.kicks), 1)
        self.assertNotIn(cid, self.r.tasks)

    async def test_images_delivered_during_intent_classification_are_collected(self):
        report = self.m("slow", text="这个玩家在黄圈外杀人，我要举报")
        await self.r.ingest(
            self.m("later", images=("https://cdn.example/later",)), False, self.reply
        )
        cid = await self.r.ingest(report, True, self.reply)
        await self.r.finish(cid)
        self.assertEqual(len(self.service.kicks), 1)

    async def test_explicit_reply_resolves_a_multi_case_ambiguity(self):
        a = await self.r.ingest(self.m("a", text="举报 A"), True, self.reply)
        b = await self.r.ingest(self.m("b", text="举报 B"), True, self.reply)
        await self.r.ingest(
            self.m("ambiguous", images=("https://cdn.example/unknown",)),
            False,
            self.reply,
        )
        await self.r.ingest(
            self.m("selected", reply_to="a", images=("https://cdn.example/a",)),
            False,
            self.reply,
        )
        await self.r.finish(a)
        await self.r.finish(b)
        self.assertEqual(self.s.get(a)["status"], "submitted")
        self.assertEqual(self.s.get(b)["status"], "needs_evidence")

    async def test_background_tracking_reports_confirmed_result_without_repeating_kick(
        self,
    ):
        cid = await self.r.ingest(
            self.m(text="举报", images=("https://cdn.example/a",)), True, self.reply
        )
        await self.r.finish(cid)
        await self.r.poll()
        self.assertTrue(any("踢出已确认" in text for text in self.replies))
        self.assertEqual(len(self.service.kicks), 1)

    async def test_kick_uses_fixed_rule_reason_not_model_announcement_text(self):
        self.model.changes = {
            "reason": "模型自由发挥的公告",
            "english": "arbitrary text",
            "japanese": "自由文",
        }
        cid = await self.r.ingest(
            self.m(text="举报", images=("https://cdn.example/a",)), True, self.reply
        )
        await self.r.finish(cid)
        sent = self.service.kicks[0][1]
        self.assertEqual(sent["reason"], "黄圈外违规交战")
        self.assertNotEqual(sent["english"], "arbitrary text")
