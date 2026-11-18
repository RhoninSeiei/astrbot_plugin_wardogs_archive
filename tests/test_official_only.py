import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import test_admin_auth as auth
import test_lifecycle as lifecycle
import test_plugin as pt


class OfficialOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_onebot_intake_and_public_scope_are_disabled(self):
        config = dict(
            official_only=True,
            public_queries_enabled=True,
            platform_id="default",
            bot_account_id=pt.BOT_ID,
            allowed_groups=["g"],
        )
        p = pt.WardogsPlugin(SimpleNamespace(), config)
        p.ready = True
        p.onebot_ready = True
        e = pt.PluginTests().event(images=False)
        self.assertFalse(p.allowed(e))
        self.assertIsNone(p.query_identity(e))

    async def test_official_admin_orders_remain_enabled(self):
        e, c, cfg = auth.AdminAuthTests().setup_official("admin")
        cfg["official_only"] = True
        p = pt.WardogsPlugin(c, cfg)
        p.ready = True
        p.admin_kicks = SimpleNamespace(handle=AsyncMock(return_value=True))
        await p.on_admin_order(e)
        p.admin_kicks.handle.assert_awaited_once()

    async def test_initialize_does_not_probe_default_or_start_onebot_notice(self):
        f = lifecycle.LifecycleTests()
        await f.asyncSetUp()
        f.config.update(official_only=True, server_identity_auto_notice_enabled=True)
        try:
            await f.plugin.initialize()
            await asyncio.sleep(0.02)
            self.assertTrue(f.plugin.ready)
            self.assertFalse(f.plugin.onebot_ready)
            self.assertIsNone(f.plugin.startup_task)
            self.assertIsNone(f.plugin.identity_task)
            self.assertEqual(f.bot_calls, [])
        finally:
            await f.asyncTearDown()
