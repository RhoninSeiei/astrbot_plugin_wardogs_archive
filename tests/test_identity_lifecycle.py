import asyncio
import unittest
from unittest.mock import patch

import test_lifecycle as fixture
from test_server_identity import snapshot


class IdentityLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixture.LifecycleTests()
        await self.fixture.asyncSetUp()
        self.fixture.config.update(
            server_identity_auto_notice_enabled=True,
            server_identity_notice_group="20000001",
        )
        client_class = fixture.plugin_main.ServiceClient
        original = client_class.request

        async def request(client, method, path, *args, **kwargs):
            if path == "/v1/server-identity":
                return snapshot()
            return await original(client, method, path, *args, **kwargs)

        self.patch = patch.object(client_class, "request", request)
        self.patch.start()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()
        self.patch.stop()

    async def test_initialize_monitor_and_terminate_drain_before_database_close(self):
        plugin = self.fixture.plugin
        await plugin.initialize()
        for _ in range(20):
            if plugin.identity_monitor.status == "baseline":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(plugin.identity_monitor.status, "baseline")
        self.assertFalse(plugin.identity_task.done())
        await plugin.terminate()
        self.assertTrue(plugin.identity_task.done())
        self.assertFalse(plugin.ready)
        self.assertEqual(self.fixture.clients[0].closed, 1)

    async def test_invalid_auto_scope_does_not_leave_background_tasks(self):
        plugin = self.fixture.plugin
        self.fixture.config["server_identity_notice_group"] = "other"
        with self.assertRaises(ValueError):
            await plugin.initialize()
        self.assertFalse(plugin.ready)
        self.assertEqual(self.fixture.clients[0].closed, 1)
        self.assertTrue(plugin.maintenance.done())
        self.assertIsNone(plugin.identity_task)
