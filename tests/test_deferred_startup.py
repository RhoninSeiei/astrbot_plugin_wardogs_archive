"""Backend readiness and independently verified OneBot startup/recovery."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import astrbot.api.message_components as Comp
import test_plugin as fixture
from aiocqhttp.exceptions import ApiNotAvailable


class DeferredStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        official = SimpleNamespace(
            appid="1234567890", meta=lambda: SimpleNamespace(name="qq_official")
        )
        self.context = SimpleNamespace(
            get_platform_inst=lambda key: (
                official if key == "official-fixture" else None
            )
        )
        self.plugin = fixture.WardogsPlugin(
            self.context,
            {
                "startup_checks": True,
                "platform_id": "default",
                "bot_account_id": "10000001",
                "allowed_groups": ["g"],
                "public_queries_enabled": True,
                "admin_kicks_enabled": True,
                "official_admin_scopes": ["official-fixture|1234567890|g"],
                "official_query_scopes": ["official-fixture|1234567890|g"],
            },
        )
        self.plugin.data_root = Path(self.tmp.name)
        self.backend = AsyncMock(
            return_value={
                "ready": True,
                "checked_at": time.time(),
                "backend_routes": True,
                "independent_tokens": True,
            }
        )
        self.onebot = AsyncMock(side_effect=ApiNotAvailable())
        self.patches = [
            patch.object(
                fixture.plugin_main, "verify_backend_deployment", self.backend
            ),
            patch.object(fixture.plugin_main, "verify_onebot_deployment", self.onebot),
            patch.object(fixture.plugin_main, "verify_admin_deployment", AsyncMock()),
            patch.object(
                fixture.plugin_main,
                "verify_query_deployment",
                AsyncMock(return_value={"ready": True}),
            ),
            patch.object(fixture.plugin_main, "PLATFORM_START_RETRY_SECONDS", 0.01),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    async def test_platform_late_or_disconnected_does_not_block_official_readiness(
        self,
    ):
        await self.plugin.initialize()
        await asyncio.sleep(0)
        self.assertTrue(self.plugin.ready)
        self.assertFalse(self.plugin.onebot_ready)
        self.backend.assert_awaited_once()
        self.onebot.assert_awaited()

    async def test_official_five_minute_replay_reaches_admin_route_with_onebot_down(
        self,
    ):
        await self.plugin.initialize()
        event = fixture.PluginTests().event(
            "回溯5分钟",
            images=False,
            group="g",
            platform="official-fixture",
            bot="official-self",
            adapter="qq_official",
        )
        event.message_obj.sender.user_id = "fixture-admin"
        event.message_obj.message = [
            Comp.At(qq="official-self"),
            Comp.Plain("回溯5分钟"),
        ]
        event.message_obj.raw_message = SimpleNamespace(
            raw_data={
                "group_openid": "g",
                "author": {"member_openid": "fixture-admin", "member_role": "admin"},
            }
        )
        handler = AsyncMock(return_value=True)
        self.plugin.admin_kicks.handle = handler
        await self.plugin.on_admin_order(event)
        handler.assert_awaited_once()
        self.assertEqual(handler.await_args.args[0].text, "回溯5分钟")
        self.assertEqual(handler.await_args.args[1], "admin")
        self.assertFalse(self.plugin.onebot_ready)

    async def test_onebot_channel_cannot_use_unverified_membership(self):
        await self.plugin.initialize()
        event = fixture.PluginTests().event("回溯5分钟", images=False, group="g")
        self.assertFalse(self.plugin.allowed(event))
        self.assertIsNone(self.plugin.query_identity(event))

    async def test_reconnected_onebot_requires_full_verifier_then_enables(self):
        verified = {
            "ready": True,
            "checked_at": time.time(),
            "groups_checked": 1,
            "onebot_membership": True,
        }
        self.onebot.side_effect = [ApiNotAvailable(), verified]
        await self.plugin.initialize()
        await asyncio.wait_for(self.plugin.startup_task, 1)
        self.assertTrue(self.plugin.ready)
        self.assertTrue(self.plugin.onebot_ready)
        self.assertEqual(self.onebot.await_count, 2)

    async def test_backend_failure_remains_closed_and_does_not_start_channel_worker(
        self,
    ):
        self.backend.side_effect = ValueError("synthetic_backend_failure")
        with self.assertRaises(ValueError):
            await self.plugin.initialize()
        self.assertFalse(self.plugin.ready)
        self.assertFalse(self.plugin.onebot_ready)
        self.onebot.assert_not_awaited()

    async def test_terminate_cancels_pending_channel_check_and_closes_service(self):
        await self.plugin.initialize()
        await asyncio.sleep(0)
        pending = self.plugin.startup_task
        await self.plugin.terminate()
        self.assertTrue(pending.done())
        self.assertFalse(self.plugin.ready)
        self.assertFalse(self.plugin.onebot_ready)
        before = self.onebot.await_count
        await asyncio.sleep(0.02)
        self.assertEqual(before, self.onebot.await_count)
        self.assertIsNone(self.plugin.client.session)

    async def test_concurrent_initialize_does_not_duplicate_resources(self):
        await asyncio.gather(self.plugin.initialize(), self.plugin.initialize())
        self.backend.assert_awaited_once()
        self.assertTrue(self.plugin.ready)

    async def test_account_mismatch_never_enables_onebot_or_disables_official(self):
        self.onebot.side_effect = ValueError("onebot_account_mismatch")
        await self.plugin.initialize()
        await asyncio.sleep(0.02)
        self.assertTrue(self.plugin.ready)
        self.assertFalse(self.plugin.onebot_ready)

    async def test_channel_diagnostics_are_separate_atomic_and_private(self):
        await self.plugin.initialize()
        await asyncio.sleep(0.02)
        path = self.plugin.data_root
        backend = json.loads((path / "deployment-readiness.json").read_text())
        onebot = json.loads((path / "onebot-readiness.json").read_text())
        self.assertTrue(backend["ready"])
        self.assertFalse(onebot["ready"])
        self.assertEqual((path / "onebot-readiness.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(path.glob("*.tmp")))
