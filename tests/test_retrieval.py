"""Bounded local group-search protocol and evidence provenance."""

import asyncio
import json
import tempfile
import time
import types
import unittest
from dataclasses import replace
from pathlib import Path

import test_flow as ft
import test_plugin as pt

from wardogs.domain import Identity, Message, Store
from wardogs.history import LocalHistory
from wardogs.model import FALLBACK_PROVIDER, ContextModel


class ScriptContext:
    def __init__(self, report=True):
        self.calls = []
        self.report = report
        self.fail_primary = False

    async def llm_generate(self, **kw):
        payload = json.loads(kw["prompt"])
        self.calls.append((kw["chat_provider_id"], payload))
        if self.fail_primary and kw["chat_provider_id"] != FALLBACK_PROVIDER:
            raise TimeoutError()
        turn = payload.get("turn", 0)
        result = payload.get("tool_result", {})
        if turn == 0:
            d = {
                "action": "search",
                "query": "圈外",
                "window_seconds": 86400,
                "author": "all",
                "images_only": False,
                "cursor": "",
            }
        elif turn == 1 and result.get("messages"):
            d = {"action": "read", "refs": [result["messages"][0]["ref"]]}
        elif turn == 2 and result.get("messages"):
            d = {
                "action": "finish",
                "is_report": self.report,
                "selected_refs": [result["messages"][0]["ref"]] if self.report else [],
            }
        else:
            d = {"action": "finish", "is_report": False, "selected_refs": []}
        return types.SimpleNamespace(completion_text=json.dumps(d))


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db")
        self.h = LocalHistory(self.store)
        self.who = Identity("aiocqhttp:test:10000001", "g", "reporter")
        self.trigger = Message(self.who, "trigger", time.time(), "管理在吗")
        self.context = ScriptContext()
        self.model = ContextModel(self.context, "test")
        from unittest.mock import patch

        self.wait_patch = patch("wardogs.retrieval.FOLLOWUP_WINDOW", 0)
        self.wait_patch.start()

    async def asyncTearDown(self):
        self.wait_patch.stop()
        self.store.close()
        self.tmp.cleanup()

    def seed(self, source="picture", **kw):
        defaults = dict(
            text="Example 圈外击杀了 Victim",
            images=("https://gchat.qpic.cn/evidence.png",),
        )
        defaults.update(kw)
        m = Message(
            Identity(self.who.platform, self.who.group, "other"),
            source,
            self.trigger.at - 60,
            **defaults,
        )
        self.h.record(m)
        return m

    async def discover(self):
        return await self.model.discover(self.trigger, self.h)

    async def test_model_search_read_selects_other_author_picture(self):
        evidence = self.seed()
        result = await self.discover()
        self.assertTrue(result["is_report"])
        self.assertEqual(result["messages"], [evidence])
        self.assertEqual(len(self.context.calls), 3)
        encoded = json.dumps([p for _, p in self.context.calls])
        self.assertNotIn("https://gchat", encoded)
        self.assertNotIn('"reporter"', encoded)
        self.assertNotIn('"picture"', encoded)

    async def test_primary_failure_restarts_search_with_luna(self):
        self.seed()
        self.context.fail_primary = True
        result = await self.discover()
        self.assertTrue(result["is_report"])
        self.assertEqual(
            [x[0] for x in self.context.calls],
            [
                self.model.provider_id,
                FALLBACK_PROVIDER,
                FALLBACK_PROVIDER,
                FALLBACK_PROVIDER,
            ],
        )
        self.assertEqual(self.context.calls[1][1]["turn"], 0)
        self.assertEqual(self.context.calls[1][1]["tool_result"], {})

    async def test_text_or_fallback_model_change_invalidates_cached_lookup(self):
        await self.discover()
        count = len(self.context.calls)
        self.model.text_provider_id = "new-text-fixture"
        await self.discover()
        self.assertGreater(len(self.context.calls), count)
        count = len(self.context.calls)
        self.model.fallback_provider_id = "new-backup-fixture"
        await self.discover()
        self.assertGreater(len(self.context.calls), count)

    async def test_inherited_session_model_change_invalidates_cache(self):
        from unittest.mock import AsyncMock

        self.model.text_provider_id = ""
        self.context.get_current_chat_provider_id = AsyncMock(
            return_value="session-one"
        )
        await self.discover()
        count = len(self.context.calls)
        self.context.get_current_chat_provider_id.return_value = "session-two"
        await self.discover()
        self.assertGreater(len(self.context.calls), count)

    async def test_negative_lookup_is_cached_until_group_changes(self):
        result = await self.discover()
        self.assertFalse(result["is_report"])
        count = len(self.context.calls)
        await self.discover()
        self.assertEqual(len(self.context.calls), count)
        self.seed()
        result = await self.discover()
        self.assertTrue(result["is_report"])
        self.assertGreater(len(self.context.calls), count)

    async def test_other_group_and_account_are_not_visible(self):
        self.h.record(
            Message(
                Identity("other-account", "g", "other"),
                "foreign",
                self.trigger.at - 1,
                "圈外",
                ("https://gchat.qpic.cn/other.png",),
            )
        )
        self.h.record(
            Message(
                Identity(self.who.platform, "other-group", "other"),
                "foreign2",
                self.trigger.at - 1,
                "圈外",
                ("https://gchat.qpic.cn/other2.png",),
            )
        )
        self.assertFalse((await self.discover())["is_report"])
        self.assertNotIn("foreign", json.dumps(self.context.calls))

    async def test_consumed_messages_excluded_from_automatic_search(self):
        m = self.seed()
        self.h.mark(m, "consumed")
        self.assertFalse((await self.discover())["is_report"])

    async def test_failed_discovery_is_not_cached_as_false(self):
        class Broken:
            async def llm_generate(self, **kw):
                raise TimeoutError()

        with self.assertRaises(TimeoutError):
            await ContextModel(Broken(), "test").discover(self.trigger, self.h)
        self.seed()
        self.assertTrue((await self.discover())["is_report"])

    async def test_long_trigger_keeps_keyword_and_input_changes(self):
        self.trigger = replace(
            self.trigger, text="A" * 1800 + "找管理，这个圈外击杀请处理" + "B" * 500
        )
        self.seed()
        await self.discover()
        self.assertIn("找管理", self.context.calls[0][1]["untrusted_message"])
        self.assertLessEqual(len(self.context.calls[0][1]["untrusted_message"]), 1000)

    async def test_search_pagination_and_explicit_reply(self):
        from wardogs.retrieval import SearchSession

        for i in range(12):
            self.seed(str(i), text="圈外线索" + str(i))
        s = SearchSession(self.trigger, self.h)
        first = s.search(
            {
                "action": "search",
                "query": "圈外",
                "window_seconds": 300,
                "author": "all",
                "images_only": False,
                "cursor": "",
            }
        )
        self.assertEqual(len(first["messages"]), 8)
        second = s.search(
            {
                "action": "search",
                "query": "圈外",
                "window_seconds": 300,
                "author": "all",
                "images_only": False,
                "cursor": first["next_cursor"],
            }
        )
        self.assertEqual(len(second["messages"]), 4)
        self.assertFalse(
            set(x["ref"] for x in first["messages"])
            & set(x["ref"] for x in second["messages"])
        )
        self.assertLessEqual(len(json.dumps(first, ensure_ascii=False)), 3500)

    async def test_tool_rejects_scope_injection_bad_cursor_and_unseen_refs(self):
        from wardogs.retrieval import SearchSession

        s = SearchSession(self.trigger, self.h)
        for action in [
            {"action": "search", "group": "other"},
            {"action": "search", "cursor": "invented"},
            {"action": "search", "window_seconds": 86401},
            {"action": "read", "refs": ["made-up"]},
            {"action": "finish", "is_report": True, "selected_refs": ["made-up"]},
        ]:
            with self.subTest(action=action), self.assertRaises(ValueError):
                s.execute(action)

    async def test_reply_ref_can_read_consumed_original(self):
        from wardogs.retrieval import SearchSession

        original = self.seed(
            "original", text="截图位置", images=("https://gchat.qpic.cn/original.png",)
        )
        self.h.mark(original, "consumed")
        self.seed("reply", text="圈外就是这张", images=(), reply_to=original.id)
        s = SearchSession(self.trigger, self.h)
        results = s.execute({"action": "search", "query": "圈外"})
        self.assertEqual(len(results["messages"]), 1)
        linked = results["messages"][0]["reply_ref"]
        self.assertTrue(linked)
        data = s.execute({"action": "read", "refs": [linked]})
        self.assertEqual(data["messages"][0]["image_count"], 1)

    async def test_budget_exhaustion_and_scope_injection_do_not_form_reports(self):
        class Endless:
            def __init__(self):
                self.calls = 0

            async def llm_generate(self, **kw):
                self.calls += 1
                return types.SimpleNamespace(
                    completion_text='{"action":"search","query":"","window_seconds":300}'
                )

        endless = Endless()
        with self.assertRaises(ValueError):
            await ContextModel(endless, "test").discover(self.trigger, self.h)
        self.assertEqual(endless.calls, 8)

        class Inject:
            async def llm_generate(self, **kw):
                return types.SimpleNamespace(
                    completion_text='{"action":"search","group":"foreign"}'
                )

        with self.assertRaises(ValueError):
            await ContextModel(Inject(), "test").discover(self.trigger, self.h)

    async def test_timeout_uses_independent_luna_budget_and_errors_are_redacted(self):
        from unittest.mock import patch

        parent = self

        class Slow:
            async def llm_generate(self, **kw):
                if kw["chat_provider_id"] != FALLBACK_PROVIDER:
                    await asyncio.sleep(1)
                return await parent.context.llm_generate(**kw)

        self.seed()
        with patch("wardogs.retrieval.SESSION_TIMEOUT", 0.05):
            result = await ContextModel(Slow(), "test").discover(self.trigger, self.h)
        self.assertTrue(result["is_report"])
        audit = self.h.cached(
            self.who, "retrieval_audit", self.h.fingerprint(self.trigger.id)
        )
        self.assertEqual(audit["attempts"][0]["error_type"], "TimeoutError")
        self.assertEqual(audit["attempts"][1]["provider"], FALLBACK_PROVIDER)

    async def test_unread_selection_and_query_changed_cursor_rejected(self):
        from wardogs.retrieval import SearchSession

        for i in range(10):
            self.seed(str(i))
        s = SearchSession(self.trigger, self.h)
        found = s.execute({"action": "search", "query": "圈外"})
        with self.assertRaises(ValueError):
            s.execute(
                {
                    "action": "finish",
                    "is_report": True,
                    "selected_refs": [found["messages"][0]["ref"]],
                }
            )
        with self.assertRaises(ValueError):
            s.execute(
                {"action": "search", "query": "Example", "cursor": found["next_cursor"]}
            )

    async def test_first_query_observes_short_followup_window(self):
        from unittest.mock import patch

        async def incoming():
            await asyncio.sleep(0.005)
            self.h.record(
                Message(
                    Identity(self.who.platform, self.who.group, "other"),
                    "late",
                    time.time(),
                    "Example 圈外击杀",
                    ("https://gchat.qpic.cn/late.png",),
                )
            )

        with patch("wardogs.retrieval.FOLLOWUP_WINDOW", 0.025):
            result, _ = await asyncio.gather(self.discover(), incoming())
        self.assertTrue(result["is_report"])
        self.assertEqual(result["messages"][0].id, "late")

    async def test_history_request_cannot_stop_after_intent_classification(self):
        self.seed()
        self.trigger = replace(self.trigger, text="找管理，处理前面的截图")
        parent = self

        class Shortcut:
            async def llm_generate(self, **kw):
                payload = json.loads(kw["prompt"])
                if payload.get("turn") == 0:
                    return types.SimpleNamespace(
                        completion_text='{"action":"finish","is_report":true,"selected_refs":[]}'
                    )
                return await parent.context.llm_generate(**kw)

        result = await ContextModel(Shortcut(), "test").discover(self.trigger, self.h)
        self.assertTrue(result["is_report"])
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(
            self.context.calls[0][1]["tool_result"]["messages"][0]["image_count"], 1
        )

    async def test_unread_picture_candidate_forces_independent_fallback(self):
        self.seed()
        self.trigger = replace(self.trigger, text="找管理，处理前面的截图")
        parent = self

        class SkipRead:
            async def llm_generate(self, **kw):
                if kw["chat_provider_id"] == FALLBACK_PROVIDER:
                    return await parent.context.llm_generate(**kw)
                payload = json.loads(kw["prompt"])
                if payload["turn"] == 0:
                    return await parent.context.llm_generate(**kw)
                return types.SimpleNamespace(
                    completion_text='{"action":"finish","is_report":true,"selected_refs":[]}'
                )

        result = await ContextModel(SkipRead(), "test").discover(self.trigger, self.h)
        self.assertEqual(len(result["messages"]), 1)
        audit = self.h.cached(
            self.who, "retrieval_audit", self.h.fingerprint(self.trigger.id)
        )
        self.assertEqual(audit["attempts"][0]["status"], "failed")
        self.assertEqual(audit["attempts"][1]["provider"], FALLBACK_PROVIDER)
        self.assertEqual(self.context.calls[1][1]["turn"], 0)

    async def test_empty_literal_query_returns_bounded_recent_candidates(self):
        from wardogs.retrieval import SearchSession

        self.seed()
        session = SearchSession(self.trigger, self.h)
        result = session.execute({"action": "search", "query": "圈外开火"})
        self.assertTrue(result["query_relaxed"])
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["messages"][0]["image_count"], 1)
        self.assertFalse(session.read_refs)

    async def test_caption_read_collects_same_author_nearby_pure_images(self):
        from wardogs.retrieval import SearchSession

        caption = self.seed(
            "caption", images=(), text="Example 圈外打我，下两张同次事件"
        )
        from dataclasses import replace

        for mid, delta in [("kill", 12), ("map", 36)]:
            self.h.record(
                replace(
                    caption,
                    id=mid,
                    at=caption.at + delta,
                    text="",
                    images=("https://gchat.qpic.cn/" + mid + ".png",),
                )
            )
        self.h.record(
            replace(
                caption,
                id="other",
                who=replace(caption.who, sender="outsider"),
                text="无关图片",
                images=("https://gchat.qpic.cn/other.png",),
            )
        )
        session = SearchSession(self.trigger, self.h)
        found = session.execute({"action": "search", "query": "圈外"})
        anchor = found["messages"][0]["ref"]
        read = session.execute({"action": "read", "refs": [anchor]})
        self.assertEqual(sum(x["image_count"] for x in read["messages"]), 2)
        final = session.execute(
            {"action": "finish", "is_report": True, "selected_refs": [anchor]}
        )
        self.assertEqual({m.id for m in final["messages"]}, {"caption", "kill", "map"})
        self.assertTrue(
            all(m.who.sender == caption.who.sender for m in final["messages"])
        )

    async def test_nearby_bundle_respects_time_consumption_and_read_budget(self):
        from dataclasses import replace

        from wardogs.retrieval import SearchSession

        caption = self.seed("caption", images=())
        for index in range(7):
            self.h.record(
                replace(
                    caption,
                    id="image" + str(index),
                    at=caption.at + index + 1,
                    text="",
                    images=("https://gchat.qpic.cn/" + str(index) + ".png",),
                )
            )
        old = replace(
            caption,
            id="old",
            at=caption.at - 120,
            text="",
            images=("https://gchat.qpic.cn/old.png",),
        )
        self.h.record(old)
        consumed = replace(
            caption,
            id="consumed",
            text="",
            images=("https://gchat.qpic.cn/consumed.png",),
        )
        self.h.record(consumed)
        self.h.mark(consumed, "consumed")
        session = SearchSession(self.trigger, self.h)
        found = session.execute({"action": "search", "query": "圈外"})
        anchor = found["messages"][0]["ref"]
        read = session.execute({"action": "read", "refs": [anchor]})
        self.assertEqual(len(read["messages"]), 4)
        result = session.execute(
            {"action": "finish", "is_report": True, "selected_refs": [anchor]}
        )
        self.assertLessEqual(len(result["messages"]), 4)
        self.assertFalse({"old", "consumed"} & {m.id for m in result["messages"]})

    async def test_neighbor_with_own_caption_not_implicitly_inherited(self):
        from dataclasses import replace

        from wardogs.retrieval import SearchSession

        caption = self.seed("caption", images=())
        self.h.record(
            replace(
                caption,
                id="different",
                text="另一场完全无关的截图",
                images=("https://gchat.qpic.cn/other.png",),
            )
        )
        session = SearchSession(self.trigger, self.h)
        found = session.execute({"action": "search", "query": "圈外"})
        anchor = found["messages"][0]["ref"]
        read = session.execute({"action": "read", "refs": [anchor]})
        self.assertEqual(len(read["messages"]), 1)

    async def test_natural_incident_requests_are_candidates_and_search_history(self):
        from wardogs.retrieval import TRIGGERS, requires_history

        for text in [
            "管理这个人圈外击杀",
            "服主，Example一直压家",
            "麻烦帮忙处理这个透视玩家",
            "Example圈外杀人，管理管管",
            "管理员帮忙看看这个外挂",
        ]:
            with self.subTest(text=text):
                self.assertTrue(TRIGGERS.search(text))
                self.assertTrue(requires_history(replace(self.trigger, text=text)))

    async def test_ordinary_management_chat_does_not_expand_candidate_gate(self):
        from wardogs.retrieval import TRIGGERS

        for text in [
            "我以为你也是管理",
            "一直都没有管理员啊",
            "游戏内有问题一般会艾特管理",
            "服主之前说有吧",
            "击杀太多触发KD自动踢出",
            "黄圈外不允许交战是服规",
        ]:
            with self.subTest(text=text):
                self.assertFalse(TRIGGERS.search(text))

    async def test_described_incident_without_old_material_can_request_evidence(self):
        parent = self

        class IntentOnly:
            async def llm_generate(self, **kw):
                parent.context.calls.append(
                    (kw["chat_provider_id"], json.loads(kw["prompt"]))
                )
                return types.SimpleNamespace(
                    completion_text='{"action":"finish","is_report":true,"selected_refs":[]}'
                )

        trigger = replace(self.trigger, text="管理这个人圈外击杀")
        result = await ContextModel(IntentOnly(), "test").discover(trigger, self.h)
        self.assertTrue(result["is_report"])
        self.assertEqual(result["messages"], [])
        self.assertEqual(len(self.context.calls), 2)
        self.assertIn("messages", self.context.calls[1][1]["tool_result"])

    async def test_generic_management_without_material_still_cannot_claim_report(self):
        from wardogs.retrieval import SearchSession

        session = SearchSession(self.trigger, self.h)
        session.execute({"action": "search", "query": ""})
        with self.assertRaises(ValueError):
            session.execute(
                {"action": "finish", "is_report": True, "selected_refs": []}
            )

    async def test_bare_admin_cannot_reject_visible_incident_image_without_reading(
        self,
    ):
        from wardogs.retrieval import SearchSession

        self.seed()
        session = SearchSession(self.trigger, self.h)
        result = session.execute({"action": "search", "query": "圈外"})
        with self.assertRaisesRegex(ValueError, "management_candidate_not_read"):
            session.execute(
                {"action": "finish", "is_report": False, "selected_refs": []}
            )
        session.execute({"action": "read", "refs": [result["messages"][0]["ref"]]})
        result = session.execute(
            {"action": "finish", "is_report": False, "selected_refs": []}
        )
        self.assertFalse(result["is_report"])
        self.assertEqual(result["messages"], [])


class MainRetrievalTests(unittest.IsolatedAsyncioTestCase):
    event = pt.PluginTests.event

    async def test_management_trigger_selects_cross_author_without_borrowing_identity(
        self,
    ):
        context = ScriptContext()
        with tempfile.TemporaryDirectory() as folder:
            p = pt.WardogsPlugin(context, {})
            p.config = {
                "platform_id": "default",
                "bot_account_id": pt.BOT_ID,
                "allowed_groups": ["g"],
            }
            p.data_root = Path(folder)
            await p.initialize()
            p.reports.history.record(
                Message(
                    Identity("aiocqhttp:default:" + pt.BOT_ID, "g", "other"),
                    "earlier",
                    time.time() - 60,
                    "无关旧消息",
                )
            )
            from unittest.mock import patch

            wait_patch = patch("wardogs.retrieval.FOLLOWUP_WINDOW", 0)
            wait_patch.start()
            sent = []

            async def reply(result):
                sent.append(result)

            try:
                old = self.event("Example 圈外击杀", images=True)
                old.is_at_or_wake_command = False
                old.message_obj.message_id = "photo"
                old.message_obj.sender.user_id = "other"
                old.send = reply
                await p.on_message(old)
                event = self.event("管理在吗", images=False)
                event.is_at_or_wake_command = False
                event.message_obj.message_id = "request"
                event.send = reply
                await p.on_message(event)
                self.assertGreater(len(context.calls), 0)
                self.assertEqual(len(p.reports.pending), 1)
                b = next(iter(p.reports.pending.values()))
                self.assertEqual(len(b.urls), 1)
                self.assertEqual(b.own_urls, set())
                self.assertEqual(b.retrieved[0].who.sender, "other")
            finally:
                wait_patch.stop()
                await p.terminate()

    async def test_unload_cancels_active_discovery_before_closing_store(self):
        started = asyncio.Event()

        class Slow:
            async def llm_generate(self, **kw):
                started.set()
                await asyncio.sleep(60)

        with tempfile.TemporaryDirectory() as folder:
            p = pt.WardogsPlugin(Slow(), {})
            p.config = {
                "platform_id": "default",
                "bot_account_id": pt.BOT_ID,
                "allowed_groups": ["g"],
            }
            p.data_root = Path(folder)
            await p.initialize()
            event = self.event("找管理处理圈外违规", images=False)
            event.is_at_or_wake_command = False
            task = asyncio.create_task(p.on_message(event))
            await started.wait()
            await p.terminate()
            self.assertTrue(task.cancelled())
            self.assertFalse(p.intake_tasks)
            self.assertIsNone(p.client.session)


class RetrievalFlowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ft.FlowTests.asyncSetUp
    asyncTearDown = ft.FlowTests.asyncTearDown
    m = ft.FlowTests.m
    reply = ft.FlowTests.reply

    async def test_retrieved_evidence_reaches_fixed_service_and_keeps_time_gate(self):
        m = self.m("trigger", text="找管理处理这个违规")
        photo = Message(
            Identity(m.who.platform, m.who.group, "other"),
            "photo",
            time.time() - 60,
            "圈外击杀",
            ("https://gchat.qpic.cn/photo.png",),
        )
        self.r.observe(photo)
        cid = await self.r.ingest(m, True, self.reply, retrieved=[photo])
        await self.r.finish(cid)
        self.assertEqual(len(self.service.kicks), 1)
        self.assertEqual(self.s.get(cid)["reporter_context"]["own_evidence_refs"], [])
        self.r.rates.clear()
        original_model = self.model

        class Expired:
            async def assess(self, *args):
                d = await original_model.assess(*args)
                d["observed_at"] = 0
                return d

        self.r.model = Expired()
        next_case = await self.r.ingest(
            self.m("second", text="另一个举报"), True, self.reply, retrieved=[photo]
        )
        await self.r.finish(next_case)
        self.assertEqual(len(self.service.kicks), 1)
        self.assertEqual(self.s.get(next_case)["status"], "needs_evidence")

    async def test_flow_rejects_foreign_group_before_creating_case(self):
        m = self.m("trigger", text="找管理")
        foreign = Message(
            Identity(m.who.platform, "foreign", "other"),
            "photo",
            time.time(),
            images=("https://gchat.qpic.cn/photo.png",),
        )
        with self.assertRaises(ValueError):
            await self.r.ingest(m, True, self.reply, retrieved=[foreign])
        self.assertEqual(
            self.s.db.execute("SELECT count(*) FROM cases").fetchone()[0], 0
        )

    async def test_retrieved_source_metadata_survives_text_supplement(self):
        captured = []
        parent = self

        class Capture:
            async def assess(self, case, *args):
                captured.append(case)
                d = await parent.model.assess(case, *args)
                d["verdict"] = "insufficient"
                return d

        self.r.model = Capture()
        m = self.m("trigger", text="找管理处理违规")
        photo = Message(
            Identity(m.who.platform, m.who.group, "other"),
            "photo",
            time.time() - 60,
            "圈外",
            ("https://gchat.qpic.cn/a.png",),
        )
        self.r.observe(photo)
        cid = await self.r.ingest(m, True, self.reply, retrieved=[photo])
        await self.r.finish(cid)
        await self.r.supplement(cid, self.m("new", text="补充事件信息"), self.reply)
        await self.r.finish(cid)
        self.assertEqual(len(captured), 2)
        self.assertFalse(captured[1]["retrieved_sources"][0]["from_reporter"])
        self.assertEqual(captured[1]["reporter_context"]["own_evidence_refs"], [])
        self.assertTrue(captured[1]["retrieved_sources"][0]["evidence_refs"])

    async def test_other_author_dual_images_cannot_borrow_reporter_nickname(self):
        from test_domain import combat_fixture, decision

        class DifferentMedia:
            async def load(self, url):
                return "data:image/png;base64," + url

        class NicknameModel:
            async def assess(self, case, images, players, rules):
                refs = list(images)
                combat = combat_fixture(refs[0])
                combat.update(
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
                    combat_evidence=combat,
                    observed_at=time.time(),
                    evidence_refs=refs,
                    identity_evidence=[refs[0]],
                )

        self.r.model = NicknameModel()
        self.r.media = DifferentMedia()
        m = self.m("help", text="找管理处理违规", sender_nickname="Victim")
        photos = [
            Message(
                Identity(m.who.platform, m.who.group, "other"),
                "photo" + str(i),
                time.time() - 60,
                "圈外",
                (f"https://gchat.qpic.cn/{i}.png",),
            )
            for i in range(2)
        ]
        for photo in photos:
            self.r.observe(photo)
        cid = await self.r.ingest(m, True, self.reply, retrieved=photos)
        await self.r.finish(cid)
        self.assertEqual(self.service.kicks, [])
        self.assertEqual(self.s.get(cid)["status"], "needs_evidence")
