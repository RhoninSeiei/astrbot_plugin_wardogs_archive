"""Exercise real readiness checks across cold start and plugin reload ordering."""

import asyncio
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_plugin as fixture
from aiocqhttp.exceptions import ActionFailed, ApiNotAvailable, NetworkError

plugin_main = fixture.plugin_main
RealServiceClient = plugin_main.ServiceClient
ServiceError = sys.modules[RealServiceClient.__module__].ServiceError


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.retry_patch = patch.object(
            plugin_main, "PLATFORM_START_RETRY_SECONDS", 0.005
        )
        self.retry_patch.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clients = []
        self.platform = None
        self.account_matches = True
        self.bot_calls = []
        self.bot_errors = []
        self.persistent_bot_error = None
        for name, letter in (("query", "q"), ("execution", "e"), ("admin", "a")):
            (self.root / name).write_text(letter * 40)
        self.config = {
            "platform_id": "test-platform",
            "bot_account_id": "10000001",
            "allowed_groups": ["20000001"],
            "startup_checks": True,
            "admin_kicks_enabled": True,
            "public_queries_enabled": True,
            "service_token_file": str(self.root / "query"),
            "execution_token_file": str(self.root / "execution"),
            "admin_execution_token_file": str(self.root / "admin"),
        }
        test = self

        class FakeService(RealServiceClient):
            def __init__(self, *args):
                super().__init__(*args)
                self.calls = []
                self.closed = 0
                test.clients.append(self)

            async def request(self, method, path, *args, **kwargs):
                self.calls.append((method, path))
                if method != "GET":
                    raise AssertionError("Readiness must not submit actions")
                if path == "/openapi.json":
                    return {
                        "paths": {
                            value: {}
                            for value in (
                                "/v1/moderation/kicks",
                                "/v1/moderation/operations/{operationId}",
                                "/v1/moderation/admin-kicks",
                                "/v1/moderation/player-search",
                                "/v1/moderation/admin-restrictions",
                                "/v1/moderation/recent-kills",
                                "/v1/moderation/admin-cycle-adds",
                                "/v1/moderation/admin-unbans",
                                "/v1/moderation/unban-search",
                                "/v1/moderation/cycle-list",
                                "/v1/moderation/current-restrictions",
                                "/v1/moderation/player-kills",
                                "/v1/moderation/player-sanction-summary",
                                "/v1/moderation/admin-bans",
                                "/v1/match",
                            )
                        }
                    }
                if path == "/v1/players":
                    return {"observedAt": time.time(), "players": []}
                if path == "/v1/match":
                    return {
                        "observedAt": time.time(),
                        "scoreAvailable": False,
                        "playerCount": 0,
                    }
                if path.startswith("/v1/moderation/operations/"):
                    raise ServiceError(404)
                raise AssertionError(path)

            async def close(self):
                self.closed += 1
                await super().close()

        self.service_patch = patch.object(plugin_main, "ServiceClient", FakeService)
        self.service_patch.start()
        self.context = SimpleNamespace(get_platform_inst=lambda _key: self.platform)
        self.plugin = fixture.WardogsPlugin(self.context, self.config)
        self.plugin.data_root = self.root / "plugin"

        class Bot:
            async def call_action(self, action, **kwargs):
                test.bot_calls.append((action, kwargs))
                if test.persistent_bot_error is not None:
                    raise test.persistent_bot_error
                if test.bot_errors:
                    raise test.bot_errors.pop(0)
                account = 10000001 if test.account_matches else 10000002
                if action == "get_group_list":
                    return [{"group_id": 20000001}]
                return {"user_id": account, "group_id": 20000001}

        self.available_platform = SimpleNamespace(
            meta=lambda: SimpleNamespace(name="aiocqhttp"),
            get_client=lambda: Bot(),
        )

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.service_patch.stop()
        self.retry_patch.stop()
        self.temp.cleanup()

    def startup_readiness(self):
        return json.loads(
            (self.plugin.data_root / "deployment-readiness.json").read_text()
        )

    def onebot_readiness(self):
        return json.loads((self.plugin.data_root / "onebot-readiness.json").read_text())

    async def finish_startup(self):
        if self.plugin.startup_task is not None:
            await asyncio.wait_for(self.plugin.startup_task, 1)

    async def test_cold_start_preserves_backend_and_verifies_onebot_after_platform(
        self,
    ):
        await self.plugin.initialize()
        self.assertTrue(self.plugin.ready)
        self.assertFalse(self.plugin.onebot_ready)
        self.assertFalse(self.plugin.allowed(SimpleNamespace()))
        self.platform = self.available_platform
        await self.finish_startup()
        self.assertTrue(self.plugin.onebot_ready)
        self.assertTrue(self.onebot_readiness()["onebot_membership"])
        self.assertTrue(self.startup_readiness()["independent_tokens"])
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(len(self.bot_calls), 3)
        self.assertTrue(all(method == "GET" for method, _ in self.clients[0].calls))

    async def test_hot_reload_checks_existing_platform(self):
        self.platform = self.available_platform
        await self.plugin.initialize()
        await self.finish_startup()
        self.assertTrue(self.plugin.ready)
        self.assertTrue(self.plugin.onebot_ready)
        self.assertEqual(len(self.bot_calls), 3)
        self.assertEqual(len(self.clients), 1)

    async def test_concurrent_initialize_starts_one_instance(self):
        await self.plugin.initialize()
        self.platform = self.available_platform
        await asyncio.gather(
            self.plugin.initialize(), self.finish_startup(), self.finish_startup()
        )
        self.assertTrue(self.plugin.ready)
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(len(self.bot_calls), 3)

    async def test_background_tasks_wait_for_backend_and_admin_checks(self):
        self.platform = self.available_platform
        self.config["auto_approve_joins_enabled"] = True
        entered, release = asyncio.Event(), asyncio.Event()
        verify = plugin_main.verify_admin_deployment

        async def paused(client):
            entered.set()
            await release.wait()
            await verify(client)

        with patch.object(plugin_main, "verify_admin_deployment", paused):
            task = asyncio.create_task(self.plugin.initialize())
            await entered.wait()
            self.assertFalse(self.plugin.ready)
            self.assertFalse(hasattr(self.plugin, "maintenance"))
            self.assertIsNone(self.plugin.join_task)
            release.set()
            await task
        maintenance, joins = self.plugin.maintenance, self.plugin.join_task
        await self.finish_startup()
        await self.plugin.initialize()
        self.assertIs(self.plugin.maintenance, maintenance)
        await self.plugin.terminate()
        self.assertTrue(maintenance.done())
        self.assertTrue(joins.done())

    async def test_missing_platform_keeps_only_onebot_blocked(self):
        await self.plugin.initialize()
        await asyncio.sleep(0.02)
        self.assertTrue(self.plugin.ready)
        self.assertFalse(self.plugin.onebot_ready)
        self.assertTrue(self.startup_readiness()["ready"])
        self.assertFalse(self.onebot_readiness()["ready"])
        self.assertEqual(self.bot_calls, [])
        self.assertEqual(len(self.clients), 1)

    async def test_delayed_connection_retries_identity_without_recreating_resources(
        self,
    ):
        await self.plugin.initialize()
        self.platform = self.available_platform
        self.bot_errors = [
            ApiNotAvailable(),
            NetworkError("WebSocket API call timeout"),
            TimeoutError(),
        ]
        await self.finish_startup()
        self.assertTrue(self.plugin.ready)
        self.assertTrue(self.plugin.onebot_ready)
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(
            [action for action, _ in self.bot_calls[-3:]],
            ["get_login_info", "get_group_list", "get_group_info"],
        )

    async def test_connection_timeout_remains_blocked_without_disabling_official(self):
        self.platform = self.available_platform
        self.persistent_bot_error = ApiNotAvailable()
        await self.plugin.initialize()
        await asyncio.sleep(0.03)
        self.assertTrue(self.plugin.ready)
        self.assertFalse(self.plugin.onebot_ready)
        self.assertFalse(self.onebot_readiness()["ready"])
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.clients[0].closed, 0)

    async def test_permanent_onebot_error_never_enables_channel(self):
        self.platform = self.available_platform
        self.persistent_bot_error = ActionFailed(403)
        await self.plugin.initialize()
        await asyncio.sleep(0.02)
        self.assertTrue(self.plugin.ready)
        self.assertFalse(self.plugin.onebot_ready)
        self.assertFalse(self.onebot_readiness()["ready"])

    async def test_termination_during_initialization_drains_resources(self):
        self.platform = self.available_platform
        entered, release = asyncio.Event(), asyncio.Event()
        verify = plugin_main.verify_admin_deployment

        async def paused(client):
            entered.set()
            await release.wait()
            await verify(client)

        with patch.object(plugin_main, "verify_admin_deployment", paused):
            initialization = asyncio.create_task(self.plugin.initialize())
            await entered.wait()
            termination = asyncio.create_task(self.plugin.terminate())
            await asyncio.sleep(0)
            self.assertFalse(termination.done())
            release.set()
            results = await asyncio.gather(
                initialization, termination, return_exceptions=True
            )
        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertIsNone(results[1])
        self.assertFalse(self.plugin.ready)
        self.assertEqual(self.clients[0].closed, 1)
        self.assertFalse(hasattr(self.plugin, "maintenance"))

    async def test_wrong_account_never_enables_onebot(self):
        self.platform = self.available_platform
        self.account_matches = False
        await self.plugin.initialize()
        await asyncio.sleep(0.02)
        self.assertTrue(self.plugin.ready)
        self.assertFalse(self.plugin.onebot_ready)
        await self.plugin.terminate()
        self.assertEqual(self.clients[0].closed, 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            self.plugin.store.db.execute("SELECT 1")

    async def test_admin_failure_cannot_start_maintenance_or_onebot(self):
        self.platform = self.available_platform
        (self.root / "admin").write_text("q" * 40)
        with self.assertRaisesRegex(ValueError, "independent_admin_token_required"):
            await self.plugin.initialize()
        self.assertFalse(self.plugin.ready)
        self.assertFalse(self.startup_readiness()["ready"])
        self.assertEqual(self.bot_calls, [])
        self.assertFalse(hasattr(self.plugin, "maintenance"))

    async def test_termination_before_platform_prevents_late_channel_enable(self):
        await self.plugin.initialize()
        await self.plugin.terminate()
        self.platform = self.available_platform
        self.assertTrue(self.plugin.startup_task.cancelled())
        await self.plugin.initialize()
        self.assertFalse(self.plugin.ready)
        self.assertFalse(self.plugin.onebot_ready)
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.clients[0].closed, 1)

    async def test_repeated_termination_closes_client_once(self):
        self.platform = self.available_platform
        await self.plugin.initialize()
        maintenance = self.plugin.maintenance
        await asyncio.gather(self.plugin.terminate(), self.plugin.terminate())
        self.assertFalse(self.plugin.ready)
        self.assertTrue(maintenance.done())
        self.assertEqual(self.clients[0].closed, 1)

    async def test_concurrent_termination_does_not_cancel_cleanup_twice(self):
        entered, cleanup_entered, release = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def pending(_context, _config):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(plugin_main, "verify_onebot_deployment", pending):
            await self.plugin.initialize()
            await entered.wait()
            close = self.plugin.media.close

            async def paused_close():
                cleanup_entered.set()
                await release.wait()
                await close()

            with patch.object(self.plugin.media, "close", paused_close):
                first = asyncio.create_task(self.plugin.terminate())
                await cleanup_entered.wait()
                second = asyncio.create_task(self.plugin.terminate())
                await asyncio.sleep(0)
                try:
                    self.assertEqual(self.plugin.startup_task.cancelling(), 1)
                    self.assertFalse(first.done())
                    self.assertFalse(second.done())
                finally:
                    release.set()
                await asyncio.gather(first, second)
        self.assertFalse(self.plugin.ready)
        self.assertEqual(self.clients[0].closed, 1)
        self.assertTrue(self.plugin.startup_task.cancelled())
        with self.assertRaises(sqlite3.ProgrammingError):
            self.plugin.store.db.execute("SELECT 1")
