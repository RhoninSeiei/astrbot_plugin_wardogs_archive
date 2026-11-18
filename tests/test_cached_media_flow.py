"""Actual cached Image components flow into evidence gates without network actions."""

import base64
import unittest
from pathlib import Path

import astrbot.api.message_components as Comp
import test_flow as ft
import test_plugin as pt
from test_views import picture

from wardogs.io import Media


class CachedMediaFlowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ft.FlowTests.asyncSetUp
    asyncTearDown = ft.FlowTests.asyncTearDown
    reply = ft.FlowTests.reply
    event = pt.PluginTests.event

    async def replay(self, changes):
        cache = Path(self.tmp.name) / "cache"
        cache.mkdir()
        path = cache / "screenshot.png"
        raw = base64.b64decode(picture(1920, 1080).split(",", 1)[1])
        path.write_bytes(raw)
        self.r.media = Media([], cache_root=cache)
        captured = []

        class Review:
            async def assess(self, case, images, players, rules):
                captured.append(images)
                data = await ft.Model().assess(case, images, players, rules)
                data.update(changes)
                return data

        e = self.event("举报 Example 圈外开火", images=False)
        e.message_obj.message.append(Comp.Image(file=str(path)))
        message = pt.WardogsPlugin.message(e)
        cid = await self.r.ingest(message, True, self.reply, Review())
        await self.r.finish(cid)
        self.assertEqual(
            base64.b64decode(next(iter(captured[0].values())).split(",", 1)[1]), raw
        )
        return self.s.get(cid)

    async def test_cached_component_reaches_existing_fixed_action_when_eligible(self):
        case = await self.replay({})
        self.assertEqual(case["status"], "submitted")
        self.assertEqual(len(self.service.kicks), 1)

    async def test_missing_game_event_time_still_blocks(self):
        case = await self.replay({"observed_at": 0})
        self.assertEqual(case["status"], "needs_evidence")
        self.assertFalse(self.service.kicks)

    async def test_uncertain_identity_still_blocks(self):
        case = await self.replay({"identity_confidence": 0.5})
        self.assertEqual(case["status"], "needs_evidence")
        self.assertFalse(self.service.kicks)

    async def test_low_violation_confidence_still_blocks(self):
        case = await self.replay({"violation_confidence": 0.5})
        self.assertEqual(case["status"], "needs_evidence")
        self.assertFalse(self.service.kicks)
