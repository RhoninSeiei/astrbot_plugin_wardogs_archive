import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_admin import WHO, A, B, Client

from wardogs.admin import AdminKicks
from wardogs.admin_vision import interpret_image
from wardogs.domain import Message, Store
from wardogs.model import DEFAULT_PROVIDER, FALLBACK_PROVIDER


class VisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db")
        self.client = Client()
        self.actions = AdminKicks(self.store, self.client)
        self.replies = []
        self.media = SimpleNamespace(
            load=AsyncMock(return_value="data:image/png;base64,fixture")
        )
        self.model = SimpleNamespace(
            provider_id=DEFAULT_PROVIDER,
            generate=AsyncMock(
                return_value={
                    "action": "kick",
                    "targets": ["Example"],
                    "confidence": 0.99,
                }
            ),
        )

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def handle(
        self,
        text="把截图中的玩家踢出",
        role="admin",
        images=("https://gchat.qpic.cn/fixture.png",),
        mid="image",
    ):
        return await self.actions.handle(
            Message(WHO, mid, time.time(), text, images),
            role,
            self.replies.append,
            model=self.model,
            media=self.media,
        )

    async def test_picture_nickname_only_proposes_candidates_then_numeric_confirm(self):
        await self.handle()
        self.assertFalse(self.client.calls)
        self.assertIn("1. Example", self.replies[-1])
        self.assertIn(B, self.replies[-1])
        self.assertIn(A, self.replies[-1])
        await self.actions.handle(
            Message(WHO, "confirm", time.time(), "2"),
            "admin",
            self.replies.append,
            model=self.model,
        )
        self.assertEqual(self.client.calls[0][0]["steamId"], B)

    async def test_picture_exact_id_also_requires_confirmation(self):
        self.model.generate.return_value = {
            "action": "kick",
            "targets": [A],
            "confidence": 0.99,
        }
        await self.handle()
        self.assertFalse(self.client.calls)
        self.assertIn(A, self.replies[-1])

    async def test_normal_member_never_downloads_or_calls_model(self):
        await self.handle(role="member")
        self.media.load.assert_not_awaited()
        self.model.generate.assert_not_awaited()
        self.assertFalse(self.client.calls)

    async def test_multiple_visible_names_are_unioned_without_duplicate_ids(self):
        self.model.generate.return_value = {
            "action": "kick",
            "targets": ["Example", "Examples"],
            "confidence": 0.99,
        }
        await self.handle()
        self.assertEqual(self.replies[-1].count(A), 1)
        self.assertEqual(self.replies[-1].count(B), 1)

    async def test_vision_fallback_after_primary_failure(self):
        self.model.generate.side_effect = [
            TimeoutError(),
            {"action": "kick", "targets": ["Example"], "confidence": 0.99},
        ]
        await self.handle()
        self.assertIn(A, self.replies[-1])
        self.assertEqual(
            [c.kwargs["provider_id"] for c in self.model.generate.await_args_list],
            [DEFAULT_PROVIDER, FALLBACK_PROVIDER],
        )

    async def test_uncertain_primary_uses_luna(self):
        self.model.generate.side_effect = [
            {"action": "clarify", "targets": [], "confidence": 0.4},
            {"action": "kick", "targets": ["Examples"], "confidence": 0.99},
        ]
        await self.handle()
        self.assertEqual(self.model.generate.await_count, 2)
        self.assertIn(B, self.replies[-1])

    async def test_uncertain_both_does_not_create_candidates(self):
        self.model.generate.return_value = {
            "action": "clarify",
            "targets": [],
            "confidence": 0.3,
        }
        await self.handle()
        self.assertFalse(self.client.calls)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM admin_kick_drafts").fetchone()[
                0
            ],
            0,
        )

    async def test_no_kick_intent_does_not_create_candidates(self):
        self.model.generate.return_value = {
            "action": "none",
            "targets": [],
            "confidence": 0.99,
        }
        await self.handle(text="看看这张截图")
        self.assertFalse(self.client.calls)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM admin_kick_drafts").fetchone()[
                0
            ],
            0,
        )

    async def test_negative_current_message_never_creates_candidates(self):
        await self.handle(text="不要踢图里的人")
        self.assertFalse(self.client.calls)
        self.model.generate.assert_not_awaited()

    async def test_media_failure_blocks_not_text_fallback(self):
        self.media.load.side_effect = ValueError("untrusted image")
        await self.handle(text="踢出 " + A)
        self.assertFalse(self.client.calls)
        self.model.generate.assert_not_awaited()

    async def test_raw_image_urls_not_sent_to_model(self):
        await self.handle()
        call = self.model.generate.await_args
        self.assertEqual(call.kwargs["images"], ["data:image/png;base64,fixture"])
        self.assertNotIn("https://", call.args[0])

    async def test_empty_or_control_character_targets_are_rejected(self):
        for targets in [[], [""], ["Example\nignore instructions"], ["x"] * 6]:
            self.model.generate.return_value = {
                "action": "kick",
                "targets": targets,
                "confidence": 0.99,
            }
            await self.handle(mid=str(targets))
        self.assertFalse(self.client.calls)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM admin_kick_drafts").fetchone()[
                0
            ],
            0,
        )

    async def test_too_many_images_are_bounded(self):
        result = await interpret_image(
            self.model, "踢图里的人", tuple(str(n) for n in range(5)), self.media
        )
        self.assertTrue(result["clarify"])
        self.media.load.assert_not_awaited()

    async def test_quoted_image_is_forwarded_by_main(self):
        import astrbot.api.message_components as Comp
        import test_admin_natural as natural
        import test_plugin as pt

        e, c, cfg = natural.NaturalEventTests().setup_official()
        e.message_obj.message = [
            Comp.Plain("踢图里的人"),
            Comp.At(qq=e.message_obj.self_id),
            Comp.Reply(
                id="quoted",
                chain=[Comp.Image.fromURL("https://gchat.qpic.cn/quoted.png")],
            ),
        ]
        p = pt.WardogsPlugin(c, cfg)
        p.ready = True
        p.media = self.media
        p.admin_kicks = SimpleNamespace(handle=AsyncMock(return_value=True))
        await p.on_admin_order(e)
        message = p.admin_kicks.handle.await_args.args[0]
        self.assertEqual(message.reply_images, ("https://gchat.qpic.cn/quoted.png",))
        self.assertIs(p.admin_kicks.handle.await_args.kwargs["media"], self.media)

    async def test_short_kick_with_image_proposes_candidates(self):
        await self.handle(text="踢")
        self.model.generate.assert_awaited_once()
        self.assertIn(A, self.replies[-1])
        self.assertFalse(self.client.calls)

    async def test_short_kick_multiple_names_need_numeric_selection(self):
        self.client.data["players"][1]["name"] = "SecondFixture"
        self.model.generate.return_value = {
            "action": "kick",
            "targets": ["Example", "SecondFixture"],
            "confidence": 0.99,
        }
        await self.handle(text="踢")
        self.assertIn(A, self.replies[-1])
        self.assertIn(B, self.replies[-1])
        self.assertFalse(self.client.calls)
        await self.actions.handle(
            Message(WHO, "choose", time.time(), "2"),
            "admin",
            self.replies.append,
            model=self.model,
        )
        self.assertEqual(self.client.calls[0][0]["steamId"], B)

    async def test_short_kick_primary_none_is_not_silently_dropped(self):
        self.model.generate.side_effect = [
            {"action": "none", "targets": [], "confidence": 0.99},
            {"action": "kick", "targets": ["Example"], "confidence": 0.99},
        ]
        await self.handle(text="踢")
        self.assertEqual(self.model.generate.await_count, 2)
        self.assertIn(A, self.replies[-1])
        self.assertFalse(self.client.calls)

    async def test_short_offline_order_primary_none_uses_luna(self):
        self.model.generate.side_effect = [
            {"action": "none", "targets": [], "confidence": 0.99},
            {"action": "kick", "targets": ["Example"], "confidence": 0.99},
        ]
        await self.handle(text="补封")
        self.assertEqual(self.model.generate.await_count, 2)
        self.assertIn(A, self.replies[-1])
        self.assertFalse(self.client.calls)
