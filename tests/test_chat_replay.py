"""Synthetic history-shaped replay; only in-memory service and media substitutes."""

import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import astrbot.api.message_components as Comp
import test_flow as ft
import test_plugin as pt

from test_domain import combat_fixture, decision
from wardogs.model import ContextModel


class ReplayContext:
    def __init__(self):
        self.report = True
        self.calls = 0

    async def llm_generate(self, **kw):
        self.calls += 1
        p = json.loads(kw["prompt"])
        if not self.report:
            action = {"action": "finish", "is_report": False, "selected_refs": []}
        elif p["turn"] == 0:
            action = {"action": "search", "query": "", "window_seconds": 300}
        elif not any(x["action"] == "read" for x in p["previous_tools"]):
            action = {
                "action": "read",
                "refs": [
                    x["ref"] for x in p["tool_result"]["messages"] if x["image_count"]
                ][:4],
            }
        else:
            action = {
                "action": "finish",
                "is_report": True,
                "selected_refs": [x["ref"] for x in p["tool_result"]["messages"]][:4],
            }
        return types.SimpleNamespace(completion_text=json.dumps(action))


class ChatReplayTests(unittest.IsolatedAsyncioTestCase):
    event = pt.PluginTests.event

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.context = ReplayContext()
        self.sent = []
        self.mode = "valid"
        self.seen = []
        self.p = pt.WardogsPlugin(self.context, {})
        self.p.config = {
            "platform_id": "default",
            "bot_account_id": pt.BOT_ID,
            "allowed_groups": ["g"],
            "auto_execute": True,
            "outside_combat_rule": "No combat outside boundary",
        }
        self.p.data_root = Path(self.tmp.name)
        await self.p.initialize()
        self.service = ft.Service()
        self.p.reports.service = self.service

        class Media:
            async def load(self, url):
                return "data:image/png;base64," + url

        self.p.reports.media = Media()
        parent = self

        class Assessor:
            provider_id = "synthetic"

            async def discover(self, message, history):
                return await ContextModel(
                    parent.context, "synthetic", "synthetic"
                ).discover(message, history)

            async def assess(self, case, images, players, rules):
                parent.seen.append(case)
                return decision(
                    combat_evidence=combat_fixture(next(iter(images))),
                    observed_at=0 if parent.mode == "missing_time" else time.time(),
                    evidence_refs=list(images),
                    identity_evidence=list(images),
                    verdict="not_substantiated"
                    if parent.mode == "negative"
                    else "substantiated",
                )

        self.p.assessor = lambda event: Assessor()
        self.wait = patch("wardogs.retrieval.FOLLOWUP_WINDOW", 0)
        self.wait.start()

    async def asyncTearDown(self):
        self.wait.stop()
        await self.p.terminate()
        self.tmp.cleanup()

    async def send(self, mid, text="", sender="u", images=(), reply=None, at=False):
        e = self.event(text, images=False)
        e.is_at_or_wake_command = False
        e.message_obj.message_id = mid
        e.message_obj.sender.user_id = sender
        chain = [
            Comp.Image.fromURL("https://gchat.qpic.cn/" + name + ".png")
            for name in images
        ]
        if reply:
            chain.insert(
                0,
                Comp.Reply(
                    id=reply[0],
                    chain=[
                        Comp.Image.fromURL("https://gchat.qpic.cn/" + reply[1] + ".png")
                    ],
                ),
            )
        if at:
            chain.append(Comp.At(qq=pt.BOT_ID))
        if text:
            chain.append(Comp.Plain(text))
        e.message_obj.message = chain

        async def capture(value):
            self.sent.append(value)

        e.send = capture
        await self.p.on_message(e)

    def pending(self):
        return next(iter(self.p.reports.pending.values()))

    async def test_noise_then_admin_cross_author_split_images(self):
        await self.send("chat1", "今晚开黑吗", "b")
        await self.send("kill", images=("kill",), sender="a")
        await self.send("chat2", "我还在更新", "b")
        await self.send("map", images=("map",), sender="a")
        await self.send("request", "找管理，前面两张是Example圈外打人的证据")
        b = self.pending()
        self.assertEqual(len(b.urls), 2)
        self.assertFalse(b.own_urls)
        cid = next(iter(self.p.reports.pending))
        await self.p.reports.finish(cid)
        self.assertEqual(len(self.service.kicks), 1)
        self.assertEqual(len(self.seen[0]["retrieved_sources"]), 2)
        self.assertFalse(self.p.client.session)

    async def test_reply_at_text_reaches_collector(self):
        await self.send("source", images=("kill",), sender="a")
        await self.send(
            "request", "举报 Example 圈外开火", reply=("source", "kill"), at=True
        )
        self.assertEqual(len(self.pending().urls), 1)
        self.assertEqual(self.context.calls, 0)

    async def test_followup_picture_after_ack_is_collected(self):
        await self.send("request", "举报 Example 圈外开火", images=("kill",))
        b = self.pending()
        await self.send("followup", images=("map",))
        self.assertEqual(len(b.urls), 2)
        self.assertEqual(len(self.p.reports.pending), 1)

    async def test_consultation_after_previous_image_is_silent(self):
        await self.send("source", images=("kill",), sender="a")
        self.context.report = False
        await self.send("question", "找管理员是怎么联系的？我想问赞助，没有举报")
        self.assertFalse(self.sent)
        self.assertFalse(self.p.reports.pending)
        self.assertFalse(self.service.kicks)

    async def test_duplicate_delivery_does_not_repeat_simulated_penalty(self):
        await self.send("report", "举报 Example 圈外开火", images=("kill",))
        cid = next(iter(self.p.reports.pending))
        await self.p.reports.finish(cid)
        await self.send("report", "举报 Example 圈外开火", images=("kill",))
        self.assertEqual(len(self.service.kicks), 1)
        self.assertEqual(
            self.p.store.db.execute("SELECT count(*) FROM cases").fetchone()[0], 1
        )

    async def test_missing_event_time_blocks_simulated_penalty(self):
        self.mode = "missing_time"
        await self.send("report", "举报 Example 圈外开火", images=("kill",))
        cid = next(iter(self.p.reports.pending))
        await self.p.reports.finish(cid)
        self.assertEqual(self.p.store.get(cid)["status"], "needs_evidence")
        self.assertFalse(self.service.kicks)

    async def test_negative_review_blocks_simulated_penalty(self):
        self.mode = "negative"
        await self.send("report", "举报 Example 圈外开火", images=("kill",))
        cid = next(iter(self.p.reports.pending))
        await self.p.reports.finish(cid)
        self.assertEqual(self.p.store.get(cid)["status"], "not_substantiated")
        self.assertFalse(self.service.kicks)

    async def test_retrieval_that_only_selects_caption_cannot_execute(self):
        from dataclasses import replace

        trigger = replace(
            self.p.message(self.event("找管理，处理前面的两张图", images=False)),
            id="request",
        )
        source = replace(
            trigger,
            who=replace(trigger.who, sender="a"),
            id="caption",
            at=time.time() - 30,
            text="Example 圈外击杀，一张击杀条一张地图",
        )
        self.p.reports.observe(source)
        self.p.reports.observe(trigger)

        async def notify(text):
            self.sent.append(text)

        cid = await self.p.reports.ingest(
            trigger,
            True,
            notify,
            self.p.assessor(None),
            observed=True,
            retrieved=[source],
        )
        await self.p.reports.finish(cid)
        self.assertEqual(self.p.store.get(cid)["status"], "needs_evidence")
        self.assertFalse(self.service.kicks)
        self.assertFalse(self.seen)

    async def test_bare_administrator_cue_searches_history_without_at(self):
        await self.send("source", "Example 圈外打人", sender="a", images=("kill",))
        await self.send("request", "管理员")
        self.assertEqual(len(self.pending().urls), 1)
        self.assertGreater(self.context.calls, 0)

    async def test_any_admin_question_searches_history_without_at(self):
        await self.send("source", "Example 圈外打人", sender="a", images=("kill",))
        await self.send("request", "有管理吗")
        self.assertEqual(len(self.pending().urls), 1)
        self.assertGreater(self.context.calls, 0)

    async def test_narrow_caption_selection_keeps_both_split_images(self):
        class Narrow:
            async def llm_generate(self, **kw):
                payload = json.loads(kw["prompt"])
                if payload["turn"] == 0:
                    action = {"action": "search", "query": "圈外"}
                else:
                    anchor = next(
                        m["ref"]
                        for m in payload["tool_result"]["messages"]
                        if m["text"]
                    )
                    action = (
                        {"action": "read", "refs": [anchor]}
                        if payload["turn"] == 1
                        else {
                            "action": "finish",
                            "is_report": True,
                            "selected_refs": [anchor],
                        }
                    )
                return types.SimpleNamespace(completion_text=json.dumps(action))

        self.p.context = Narrow()
        self.context = self.p.context
        await self.send(
            "caption", "Example 圈外打我了，下面是击杀条和死亡位置", sender="a"
        )
        await self.send("kill", images=("kill",), sender="a")
        await self.send("noise", "今晚有人玩吗", sender="b")
        await self.send("map", images=("map",), sender="a")
        await self.send("request", "找管理，处理前面的两张图")
        self.assertEqual(len(self.pending().urls), 2)
        self.assertFalse(self.pending().own_urls)
        self.mode = "missing_time"
        cid = next(iter(self.p.reports.pending))
        await self.p.reports.finish(cid)
        self.assertEqual(self.p.store.get(cid)["status"], "needs_evidence")
        self.assertFalse(self.service.kicks)

    async def test_natural_management_incident_uses_earlier_image(self):
        await self.send("photo", sender="a", images=("kill",))
        await self.send("request", "管理这个人圈外击杀")
        self.assertEqual(len(self.pending().urls), 1)
        self.assertGreater(self.context.calls, 0)
        self.assertFalse(self.service.kicks)

    async def test_natural_management_incident_without_image_requests_evidence(self):
        class NoImage:
            async def llm_generate(self, **kw):
                return types.SimpleNamespace(
                    completion_text='{"action":"finish","is_report":true,"selected_refs":[]}'
                )

        self.context = NoImage()
        await self.send("request", "管理这个人圈外击杀")
        cid = next(iter(self.p.reports.pending))
        await self.p.reports.finish(cid)
        self.assertEqual(self.p.store.get(cid)["status"], "needs_evidence")
        self.assertTrue(self.sent)
        self.assertFalse(self.service.kicks)

    async def test_management_rule_discussion_keeps_silent_despite_old_image(self):
        await self.send("photo", sender="a", images=("kill",))
        self.context.report = False
        await self.send("discussion", "管理员说过黄圈外不准交战，这条规则我知道了")
        self.assertFalse(self.p.reports.pending)
        self.assertFalse(self.sent)
        self.assertFalse(self.service.kicks)
