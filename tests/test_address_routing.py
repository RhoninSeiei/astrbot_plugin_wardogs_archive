import unittest

import astrbot.api.message_components as Comp
import test_passive as fixture
import test_plugin as pt


class AddressRoutingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PassiveTests.asyncSetUp
    asyncTearDown = fixture.PassiveTests.asyncTearDown
    make_plugin = fixture.PassiveTests.make_plugin
    event = pt.PluginTests.event

    async def deliver(
        self, chain, text="@管理助手(20000002) 踢", mid="directed", raw=None
    ):
        e = self.event(text, images=False)
        e.message_obj.message_id = mid
        e.message_obj.message = chain
        e.is_at_or_wake_command = False
        if raw is not None:
            e.message_obj.raw_message = raw

        async def capture(value):
            self.sent.append(value)

        e.send = capture
        await self.p.on_message(e)
        return e

    def image(self):
        return Comp.Image.fromURL("https://gchat.qpic.cn/fixture.png")

    async def test_other_bot_screenshot_order_never_starts_passive_report(self):
        self.answer = True
        e = await self.deliver(
            [Comp.At(qq="20000002", name="管理助手"), Comp.Plain("踢"), self.image()]
        )
        self.assertFalse(self.calls)
        self.assertFalse(self.sent)
        self.assertFalse(self.p.reports.pending)
        self.assertFalse(e.is_stopped())
        self.assertEqual(self.p.reports.history.state(self.p.message(e)), "command")
        self.assertFalse(self.p.reports.buffer.messages)

    async def test_directed_report_and_confirmation_are_not_passively_claimed(self):
        self.answer = True
        for mid, text in [("request", "举报 Example 圈外"), ("confirm", "1")]:
            await self.deliver(
                [Comp.At(qq="20000002"), Comp.Plain(text), self.image()], mid=mid
            )
        self.assertFalse(self.calls)
        self.assertFalse(self.p.reports.pending)

    async def test_raw_mention_when_adapter_component_is_missing(self):
        self.answer = True
        await self.deliver(
            [Comp.Plain("踢"), self.image()],
            raw={
                "self_id": int(pt.BOT_ID),
                "message": [
                    {"type": "at", "data": {"qq": "20000002"}},
                    {"type": "text", "data": {"text": "踢"}},
                ],
            },
        )
        self.assertFalse(self.calls)
        self.assertFalse(self.p.reports.pending)

    async def test_explicit_self_mention_preserves_reporting(self):
        self.answer = True
        await self.deliver(
            [
                Comp.At(qq="20000002"),
                Comp.At(qq=pt.BOT_ID),
                Comp.Plain("举报 Example 圈外"),
                self.image(),
            ],
            text="举报 Example 圈外",
        )
        self.assertEqual(len(self.p.reports.pending), 1)
        self.assertTrue(self.sent)

    async def test_inline_accused_mention_is_not_treated_as_addressee(self):
        await self.deliver(
            [
                Comp.Plain("举报 "),
                Comp.At(qq="20000002"),
                Comp.Plain("圈外击杀"),
                self.image(),
            ],
            text="举报 Example 圈外击杀",
        )
        self.assertEqual(len(self.p.reports.pending), 1)

    async def test_quoted_foreign_mention_does_not_override_current_report(self):
        await self.deliver(
            [
                Comp.Reply(id="prior", chain=[Comp.At(qq="20000002")]),
                Comp.Plain("举报 Example 圈外"),
                self.image(),
            ],
            text="举报 Example 圈外",
        )
        self.assertEqual(len(self.p.reports.pending), 1)

    async def test_directed_image_cannot_attach_to_existing_report(self):
        await self.deliver(
            [Comp.Plain("举报 Example 圈外"), self.image()],
            text="举报 Example 圈外",
            mid="report",
        )
        pending = next(iter(self.p.reports.pending.values()))
        before = dict(pending.urls)
        await self.deliver(
            [
                Comp.At(qq="20000002"),
                Comp.Plain("踢"),
                Comp.Image.fromURL("https://gchat.qpic.cn/other.png"),
            ]
        )
        self.assertEqual(pending.urls, before)
        self.assertEqual(len(self.p.reports.pending), 1)

    async def test_excluded_command_is_not_retrieved_or_quoted(self):
        e = await self.deliver([Comp.At(qq="20000002"), Comp.Plain("踢"), self.image()])
        from dataclasses import replace

        from wardogs.retrieval import SearchSession

        m = replace(self.p.message(e), id="later", reply_to="directed")
        self.assertIsNone(self.p.reports.history.quote(m))
        self.assertIsNone(
            self.p.reports.history.lookup(m, "directed", ceiling=m.at + 8)
        )
        result = SearchSession(m, self.p.reports.history).search(
            {
                "action": "search",
                "query": "",
                "window_seconds": 300,
                "author": "all",
                "images_only": False,
                "cursor": "",
            }
        )
        self.assertFalse(result["messages"])
