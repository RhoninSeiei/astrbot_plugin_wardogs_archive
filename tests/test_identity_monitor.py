import asyncio
import sqlite3
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_server_identity import OTHER, SID, snapshot

from wardogs.identity_monitor import IdentityMonitor, notice_target, prepare_notice

TARGET = ("fixture", "3", "1")


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(":memory:")
        self.client = SimpleNamespace(
            server_identity=AsyncMock(side_effect=lambda: snapshot(self.sid))
        )
        self.sid = SID
        self.send = AsyncMock()
        self.prepare = AsyncMock(return_value=self.send)
        self.monitor = IdentityMonitor(self.db, self.client, TARGET, self.prepare)

    async def asyncTearDown(self):
        self.db.close()

    async def change(self):
        await self.monitor.tick()
        self.sid = OTHER
        await self.monitor.tick()

    async def test_baseline_then_change_once_and_restart(self):
        await self.monitor.tick()
        self.prepare.assert_not_awaited()
        await self.change()
        self.send.assert_awaited_once()
        text = self.send.call_args.args[0]
        self.assertIn(OTHER, text)
        for secret in ["192.0.2.15", "fixture-secret", "http", "RCON"]:
            self.assertNotIn(secret, text)
        restored = IdentityMonitor(self.db, self.client, TARGET, self.prepare)
        await restored.tick()
        self.send.assert_awaited_once()
        self.sid = SID
        await restored.tick()
        self.assertEqual(self.send.await_count, 2)

    async def test_persistent_baseline_detects_change_while_stopped(self):
        await self.monitor.tick()
        self.sid = OTHER
        restored = IdentityMonitor(self.db, self.client, TARGET, self.prepare)
        await restored.tick()
        self.send.assert_awaited_once()

    async def test_concurrent_ticks_claim_once(self):
        await self.monitor.tick()
        self.sid = OTHER
        await asyncio.gather(*(self.monitor.tick() for _ in range(5)))
        self.send.assert_awaited_once()

    async def test_unavailable_data_keeps_previous_baseline(self):
        await self.monitor.tick()
        self.client.server_identity.side_effect = TimeoutError("private")
        await self.monitor.tick()
        self.client.server_identity.side_effect = lambda: snapshot(OTHER)
        await self.monitor.tick()
        self.send.assert_awaited_once()

    async def test_failed_preflight_retries_without_consuming_change(self):
        self.prepare.side_effect = PermissionError("not_admin")
        await self.change()
        self.send.assert_not_awaited()
        self.prepare.side_effect = None
        await self.monitor.tick()
        self.send.assert_awaited_once()

    async def test_stale_and_malformed_do_not_baseline(self):
        for data in [
            None,
            dict(snapshot(), observedAt=time.time() - 100),
            dict(snapshot(), serverId="https://private"),
        ]:
            self.client.server_identity.side_effect = None
            self.client.server_identity.return_value = data
            await self.monitor.tick()
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM wd_server_identity_state").fetchone()[
                0
            ],
            0,
        )
        self.send.assert_not_awaited()

    async def test_change_during_preflight_is_not_published(self):
        await self.monitor.tick()
        self.sid = OTHER

        async def prepare():
            self.sid = SID
            return self.send

        self.prepare.side_effect = prepare
        await self.monitor.tick()
        self.send.assert_not_awaited()

    async def test_timeout_unknown_not_replayed_after_restart_or_new_id(self):
        self.send.side_effect = TimeoutError("private")
        await self.change()
        restored = IdentityMonitor(self.db, self.client, TARGET, self.prepare)
        await restored.tick()
        self.sid = "abcdefab-abcd-abcd-abcd-123456789abc"
        await restored.tick()
        self.send.assert_awaited_once()
        self.assertEqual(restored.status, "unknown")

    async def test_cancel_inflight_persists_unknown(self):
        self.send.side_effect = asyncio.CancelledError()
        await self.monitor.tick()
        self.sid = OTHER
        with self.assertRaises(asyncio.CancelledError):
            await self.monitor.tick()
        restored = IdentityMonitor(self.db, self.client, TARGET, self.prepare)
        await restored.tick()
        self.send.assert_awaited_once()
        self.assertEqual(restored.status, "unknown")

    async def test_crash_dispatching_recovered_as_unknown(self):
        await self.monitor.tick()
        with self.db:
            self.db.execute(
                "UPDATE wd_server_identity_state SET status='dispatching', pending_id=?",
                (OTHER,),
            )
        self.sid = OTHER
        restored = IdentityMonitor(self.db, self.client, TARGET, self.prepare)
        await restored.tick()
        self.send.assert_not_awaited()
        self.assertEqual(restored.status, "unknown")

    async def test_new_explicit_target_has_its_own_baseline(self):
        await self.monitor.tick()
        self.sid = OTHER
        other = IdentityMonitor(
            self.db, self.client, ("fixture", "3", "4"), self.prepare
        )
        await other.tick()
        self.send.assert_not_awaited()

    async def test_uuid_case_is_not_a_change(self):
        self.sid = OTHER
        await self.monitor.tick()
        self.sid = OTHER.upper()
        await self.monitor.tick()
        self.send.assert_not_awaited()


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setup_adapter(self):
        self.responses = {
            "get_login_info": {"user_id": 3},
            "get_group_list": [{"group_id": 1}],
            "get_group_member_info": {"group_id": 1, "user_id": 3, "role": "admin"},
        }

        async def call(action, **kwargs):
            return self.responses.get(action)

        self.client = SimpleNamespace(call_action=AsyncMock(side_effect=call))
        self.platform = SimpleNamespace(
            meta=lambda: SimpleNamespace(name="aiocqhttp"),
            get_client=lambda: self.client,
        )
        self.context = SimpleNamespace(
            get_platform_inst=lambda name: self.platform if name == "fixture" else None
        )

    async def test_exact_native_notice_no_ordinary_message(self):
        self.setup_adapter()
        send = await prepare_notice(self.context, TARGET)
        self.assertEqual(self.client.call_action.await_count, 3)
        await send("fixed-safe-content")
        self.assertEqual(
            self.client.call_action.call_args.kwargs,
            {
                "action": "_send_group_notice",
                "self_id": 3,
                "group_id": 1,
                "content": "fixed-safe-content",
            },
        )

    async def test_wrong_account_group_or_role_denied(self):
        for key, value in [
            ("get_login_info", {"user_id": 9}),
            ("get_group_list", []),
            ("get_group_member_info", {"group_id": 1, "user_id": 3, "role": "member"}),
            ("get_group_member_info", {"group_id": 2, "user_id": 3, "role": "admin"}),
        ]:
            self.setup_adapter()
            self.responses[key] = value
            with self.assertRaises(PermissionError):
                await prepare_notice(self.context, TARGET)
            self.assertNotIn(
                "_send_group_notice",
                [c.kwargs["action"] for c in self.client.call_action.call_args_list],
            )

    async def test_official_adapter_not_guessed_or_faked(self):
        self.setup_adapter()
        self.platform.meta = lambda: SimpleNamespace(name="qq_official")
        with self.assertRaises(PermissionError):
            await prepare_notice(self.context, TARGET)
        self.client.call_action.assert_not_awaited()

    def test_config_target_must_be_explicit_and_in_existing_scope(self):
        config = dict(
            platform_id="fixture",
            bot_account_id="3",
            allowed_groups=["1"],
            server_identity_notice_group="1",
        )
        self.assertEqual(notice_target(config), TARGET)
        for update in [
            dict(server_identity_notice_group=""),
            dict(server_identity_notice_group="2"),
            dict(bot_account_id=""),
            dict(platform_id=""),
            dict(server_identity_notice_group="official-openid"),
        ]:
            with self.assertRaises(ValueError):
                notice_target(dict(config, **update))


class HttpMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_client_http_baseline_change_failure_and_safe_native_payload(
        self,
    ):
        import tempfile
        from pathlib import Path

        from aiohttp import web

        from wardogs.io import ServiceClient

        calls = []
        sid = SID
        status = 200

        async def handle(req):
            calls.append((req.method, req.path, req.headers.get("Authorization")))
            if status != 200:
                return web.Response(status=status, text="private-address-and-token")
            return web.json_response(snapshot(sid))

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "query"
            key.write_text("synthetic-query-token")
            client = ServiceClient(
                "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1]), key
            )
            db = sqlite3.connect(":memory:")
            adapter = AdapterTests()
            adapter.setup_adapter()

            async def prepare():
                return await prepare_notice(adapter.context, TARGET)

            monitor = IdentityMonitor(db, client, TARGET, prepare)
            try:
                await monitor.tick()
                status = 503
                sid = OTHER
                await monitor.tick()
                self.assertEqual(monitor.status, "identity_unavailable")
                status = 200
                await monitor.tick()
                await monitor.tick()
                sent = [
                    c.kwargs
                    for c in adapter.client.call_action.call_args_list
                    if c.kwargs["action"] == "_send_group_notice"
                ]
                self.assertEqual(len(sent), 1)
                self.assertIn(OTHER, sent[0]["content"])
                self.assertNotIn("192.0.2.15", sent[0]["content"])
                self.assertTrue(
                    all(
                        c
                        == (
                            "GET",
                            "/v1/server-identity",
                            "Bearer synthetic-query-token",
                        )
                        for c in calls
                    )
                )
            finally:
                await client.close()
                db.close()
                await runner.cleanup()
