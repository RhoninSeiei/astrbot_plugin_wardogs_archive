"""Passive intake with real OneBot events and persistent local deduplication."""

import asyncio
import json
import tempfile
import types
import unittest
from pathlib import Path

import test_flow as flow_tests
import test_plugin as plugin_tests


class PassiveTests(unittest.IsolatedAsyncioTestCase):
    event = plugin_tests.PluginTests.event

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        self.sent = []
        self.answer = False
        self.provider_fails = False
        parent = self

        class Context:
            async def llm_generate(self, **kw):
                parent.calls.append(kw)
                await asyncio.sleep(0)
                if parent.provider_fails:
                    raise TimeoutError()
                payload = json.loads(kw["prompt"])
                materials = payload.get("tool_result", {}).get("messages", [])
                if materials and parent.answer:
                    ref = materials[0]["ref"]
                    result = (
                        {"action": "finish", "is_report": True, "selected_refs": [ref]}
                        if any(x["action"] == "read" for x in payload["previous_tools"])
                        else {"action": "read", "refs": [ref]}
                    )
                else:
                    result = {"is_report": parent.answer}
                return types.SimpleNamespace(completion_text=json.dumps(result))

        self.context = Context()
        self.p = await self.make_plugin()

    async def make_plugin(self):
        p = plugin_tests.WardogsPlugin(self.context, {})
        p.config = {
            "platform_id": "default",
            "bot_account_id": plugin_tests.BOT_ID,
            "allowed_groups": ["g"],
        }
        p.data_root = Path(self.tmp.name)
        await p.initialize()
        return p

    async def asyncTearDown(self):
        await self.p.terminate()
        self.tmp.cleanup()

    async def send(self, text, mid, images=False, **kw):
        event = self.event(text, images=images, **kw)
        event.is_at_or_wake_command = False
        event.message_obj.message_id = mid

        async def reply(value):
            self.sent.append(value)

        event.send = reply
        await self.p.on_message(event)
        return event

    async def test_nonmentioned_keyword_starts_report(self):
        self.answer = True
        await self.send("刚才那位圈外开火，这个需要举报", "trigger", images=True)
        self.assertEqual(len(self.p.reports.pending), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.sent)

    async def test_question_is_silent_and_cached_across_restart(self):
        text = "举报 怎么提交材料？"
        await self.send(text, "question")
        await self.send(text, "question")
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.sent)
        await self.p.terminate()
        self.p = await self.make_plugin()
        await self.send(text, "question")
        await self.send(text, "question-again")
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.p.reports.pending)

    async def test_recent_own_context_is_bounded_and_other_sender_excluded(self):
        for i in range(16):
            await self.send("相关线索" + str(i) + "X" * 2500, "prior" + str(i))
        other = self.event("其他人独有线索", images=False)
        other.is_at_or_wake_command = False
        other.message_obj.message_id = "other-author"
        other.message_obj.sender.user_id = "other-member"
        await self.p.on_message(other)
        self.answer = True
        await self.send("这件事需要举报", "trigger", images=True)
        prompt = self.calls[0]["prompt"]
        self.assertLess(len(prompt), 2600)
        self.assertIn("相关线索15", prompt)
        self.assertNotIn("相关线索0X", prompt)
        self.assertNotIn("其他人独有线索", prompt)
        self.assertNotIn('"g"', prompt)
        b = next(iter(self.p.reports.pending.values()))
        self.assertLessEqual(sum(map(len, b.messages)), 2000)

    async def test_concurrent_redelivery_only_classifies_once(self):
        await asyncio.gather(
            self.send("有人知道怎么举报吗", "same"),
            self.send("有人知道怎么举报吗", "same"),
        )
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.sent)

    async def test_failures_are_not_cached_as_nonreport(self):
        self.provider_fails = True
        await self.send("这个需要举报", "first")
        self.assertFalse(self.sent)
        self.provider_fails = False
        self.answer = True
        await self.send("这个需要举报", "retry", images=True)
        self.assertEqual(len(self.p.reports.pending), 1)
        self.assertEqual(len(self.calls), 3)

    async def test_prior_picture_survives_plugin_restart(self):
        await self.send("", "picture", images=True)
        await self.p.terminate()
        self.p = await self.make_plugin()
        self.answer = True
        await self.send("刚才的图片我要举报", "trigger")
        b = next(iter(self.p.reports.pending.values()))
        self.assertEqual(len(b.urls), 1)

    async def test_other_bot_group_and_bot_self_never_enter_history(self):
        await self.send("这件事需要举报", "other-bot", bot="10000002")
        await self.send("这件事需要举报", "other-group", group="other")
        event = self.event("这件事需要举报")
        event.message_obj.sender.user_id = plugin_tests.BOT_ID
        await self.p.on_message(event)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.p.reports.pending)

    async def test_actual_waking_stage_accepts_custom_handler_without_mention(self):
        from unittest.mock import AsyncMock, patch

        from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
        from astrbot.core.star.session_plugin_manager import SessionPluginManager

        event = self.event("这个需要举报", images=False)
        event.is_at_or_wake_command = False
        stage = WakingCheckStage()
        await stage.initialize(
            types.SimpleNamespace(
                db_helper=None,
                astrbot_config_id="test",
                astrbot_config={
                    "admins_id": [],
                    "wake_prefix": ["/"],
                    "platform_settings": {},
                    "plugin_set": ["*"],
                },
            )
        )
        with patch.object(
            SessionPluginManager,
            "filter_handlers_by_session",
            AsyncMock(side_effect=lambda event, handlers: handlers),
        ):
            await stage.process(event)
        names = [h.handler_name for h in event.get_extra("activated_handlers")]
        self.assertIn("on_message", names)
        self.assertFalse(event.is_at_or_wake_command)
        self.assertFalse(event.is_stopped())


class ReviewDedupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = flow_tests.FlowTests.asyncSetUp
    asyncTearDown = flow_tests.FlowTests.asyncTearDown
    m = flow_tests.FlowTests.m
    reply = flow_tests.FlowTests.reply

    async def submit(self, mid, text="举报 Example", url="https://cdn.example/a"):
        cid = await self.r.ingest(
            self.m(mid, text=text, images=(url,)), True, self.reply
        )
        await self.r.finish(cid)
        return cid

    async def test_identical_bundle_is_not_reassessed_but_new_context_is(self):
        self.model.changes = {"verdict": "insufficient"}
        first = await self.submit("first")
        second = await self.submit("second")
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(self.s.get(second)["status"], "duplicate_evidence")
        self.assertEqual(self.s.get(second)["related_case_id"], first)
        await self.submit("new-context", text="举报 Example，补充事件时间线索")
        self.assertEqual(self.model.calls, 2)

    async def test_same_image_at_new_url_deduplicates_by_content(self):
        self.model.changes = {"verdict": "not_substantiated"}
        await self.submit("first")
        await self.submit("again", url="https://cdn.example/new-signed-url")
        self.assertEqual(self.model.calls, 1)

    async def test_technical_failure_is_retried_and_new_image_is_reviewed(self):
        parent = self

        class Bad:
            async def assess(self, *args):
                parent.model.calls += 1
                raise TimeoutError()

        self.r.model = Bad()
        await self.submit("first")
        await self.submit("retry")
        self.assertEqual(self.model.calls, 2)
        self.r.model = self.model
        self.r.rates.clear()  # Start an independent evidence-change scenario.
        self.model.changes = {"verdict": "insufficient"}

        class DifferentMedia:
            async def load(self, url):
                return "data:image/png;base64," + url

        self.r.media = DifferentMedia()
        await self.submit("image-a")
        await self.submit("image-b", url="https://cdn.example/b")
        self.assertEqual(self.model.calls, 4)

    async def test_consumed_content_not_inherited_by_next_report(self):
        await self.r.ingest(
            self.m("old", text="旧案独有线索", images=("https://cdn.example/old",)),
            False,
            self.reply,
        )
        await self.submit("first")
        cid = await self.r.ingest(
            self.m("next", text="举报另一个事件", images=("https://cdn.example/new",)),
            True,
            self.reply,
        )
        b = self.r.pending[cid]
        self.assertNotIn("旧案独有线索", b.messages)
        self.assertNotIn("https://cdn.example/old", b.urls.values())

    async def test_supplement_new_time_reuses_local_picture_without_redownload(self):
        self.model.changes = {"verdict": "insufficient"}
        cid = await self.submit("first")

        class OfflineMedia:
            async def load(self, url):
                raise AssertionError("supplement must reuse archived evidence")

        self.r.media = OfflineMedia()
        self.assertTrue(
            await self.r.supplement(
                cid, self.m("supplement", text="补充可核验事件时间线索"), self.reply
            )
        )
        await self.r.finish(cid)
        self.assertEqual(self.model.calls, 2)
        self.assertEqual(self.s.get(cid)["evidence_version"], 2)
        self.assertEqual(self.s.get(cid)["status"], "needs_evidence")

    async def test_invalid_model_output_is_not_a_cached_verdict(self):
        self.model.changes = {"verdict": "invalid"}
        await self.submit("first")
        await self.submit("retry")
        self.assertEqual(self.model.calls, 2)

    async def test_review_cache_survives_reopen(self):
        self.model.changes = {"verdict": "not_substantiated"}
        await self.submit("first")
        await self.r.close()
        self.s.close()
        from wardogs.domain import Store
        from wardogs.flow import Reports

        self.s = Store(Path(self.tmp.name) / "db")
        self.r = Reports(
            self.s,
            self.model,
            self.service,
            flow_tests.Media(),
            rules={
                "outside_combat": {"version": "1", "text": "No combat outside boundary"}
            },
            auto_execute=True,
        )
        cid = await self.submit("reopened")
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(self.s.get(cid)["status"], "duplicate_evidence")

    async def test_older_explicit_quote_uses_local_receipt_not_broad_history(self):
        old = self.m(
            "old-quote", text="地图位置", images=("https://cdn.example/old-quote",)
        )
        import time
        from dataclasses import replace

        old = replace(old, at=time.time() - 180)
        await self.r.ingest(old, False, self.reply)
        self.r.buffer.messages.clear()
        cid = await self.r.ingest(
            self.m("quoted-report", text="举报 Example", reply_to=old.id),
            True,
            self.reply,
        )
        self.assertIn(
            "https://cdn.example/old-quote", self.r.pending[cid].urls.values()
        )
        self.assertFalse(self.r.pending[cid].unresolved_quote)

    async def test_scope_separates_identical_evidence(self):
        self.model.changes = {"verdict": "not_substantiated"}
        await self.submit("first")
        from dataclasses import replace

        from wardogs.domain import Identity

        m = replace(
            self.m("other", text="举报 Example", images=("https://cdn.example/a",)),
            who=Identity("FixtureBot", "another-group", "u"),
        )
        cid = await self.r.ingest(m, True, self.reply)
        await self.r.finish(cid)
        self.assertEqual(self.model.calls, 2)

    async def test_concurrent_identical_reviews_only_call_model_once(self):
        parent = self

        class Slow:
            async def assess(self, *args):
                await asyncio.sleep(0.02)
                return await parent.model.assess(*args)

        self.r.model = Slow()
        first = await self.r.ingest(
            self.m("first", text="举报 Example", images=("https://cdn.example/a",)),
            True,
            self.reply,
        )
        second = await self.r.ingest(
            self.m("second", text="举报 Example", images=("https://cdn.example/a",)),
            True,
            self.reply,
        )
        await asyncio.gather(self.r.finish(first), self.r.finish(second))
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(len(self.service.kicks), 1)
        self.assertEqual(self.s.get(second)["status"], "duplicate_evidence")


class HistoryTests(unittest.TestCase):
    def test_retention_and_cached_false_are_scoped_and_expire(self):
        import time
        from unittest.mock import patch

        from wardogs.domain import Identity, Message, Store
        from wardogs.history import LocalHistory

        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "db")
            try:
                h = LocalHistory(store)
                who = Identity("p", "g", "u")
                m = Message(who, "old", time.time() - 90000, "旧内容")
                h.record(m)
                h.remember(who, "intent", "key", False)
                self.assertIs(h.cached(who, "intent", "key"), False)
                self.assertIsNone(
                    h.cached(Identity("p", "other", "u"), "intent", "key")
                )
                with patch("wardogs.history.time.time", return_value=time.time() + 601):
                    self.assertIsNone(h.cached(who, "intent", "key"))
                h.next_prune = 0
                h.prune()
                self.assertEqual(
                    store.db.execute("SELECT count(*) FROM wd_messages").fetchone()[0],
                    0,
                )
            finally:
                store.close()

    def test_corrupt_archive_is_not_used_for_supplement(self):
        import time

        from wardogs.domain import Identity, Message, Store

        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "db")
            try:
                cid = store.create(
                    Message(Identity("p", "g", "u"), "m", time.time()), "举报"
                )
                ref = store.archive(cid, "ref", "data:image/png;base64,example")
                (store.evidence_root / cid / (ref + ".json")).write_text("[]")
                self.assertEqual(store.read_evidence(cid, [ref]), {})
                self.assertEqual(store.read_evidence(cid, ["../other"]), {})
            finally:
                store.close()
