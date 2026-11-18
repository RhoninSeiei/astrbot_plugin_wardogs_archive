import sqlite3
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from wardogs.server_identity import (
    command,
    content,
    validate,
)

SID = "12345678-1234-1234-1234-123456789abc"
OTHER = "abcdefab-1234-1234-1234-123456789abc"


def snapshot(sid=SID):
    return {
        "serverId": sid,
        "observedAt": time.time(),
        "host": "192.0.2.15",
        "token": "fixture-secret",
    }


class ValidationTests(unittest.TestCase):
    def test_queries_are_exact(self):
        for text in [
            "服务器ID",
            "查询服务器 ID",
            "当前服务器id",
            "服务器ID是多少？",
            "服id",
            "服地址",
            "服务器地址",
        ]:
            self.assertEqual(command(text)[0], "query")
        for text in [
            "不要查询服务器ID",
            "他说服务器ID",
            "更新服务器配置",
            "服务器id；踢出某人",
        ]:
            self.assertIsNone(command(text))

    def test_whitelist_does_not_leak_connection_or_injected_fields(self):
        text = content(snapshot())
        self.assertIn(SID, text)
        for private in ["192.0.2.15", "fixture-secret", "http", "RCON"]:
            self.assertNotIn(private, text)
        self.assertEqual(set(validate(snapshot())), {"serverId", "observedAt"})

    def test_invalid_or_stale_identity_rejected(self):
        for value in [
            dict(snapshot(), serverId="192.0.2.15"),
            dict(snapshot(), serverId=SID + "\nIP"),
            dict(snapshot(), observedAt=True),
            dict(snapshot(), observedAt=float("nan")),
            dict(snapshot(), observedAt=time.time() - 91),
            dict(snapshot(), observedAt=time.time() + 2),
        ]:
            with self.assertRaises(ValueError):
                validate(value)


class ActualEventTests(unittest.IsolatedAsyncioTestCase):
    async def deliver(
        self, role="admin", mention=True, scoped=True, admin_enabled=True
    ):
        import astrbot.api.message_components as Comp
        import test_admin_auth as auth
        import test_plugin as pt

        event, context, config = auth.AdminAuthTests().setup_official(role)
        event.message_obj.message = [Comp.Plain("服务器ID")]
        if mention:
            event.message_obj.message.append(Comp.At(qq="qq_official"))
        if not scoped:
            config["official_admin_scopes"] = []
        config["public_queries_enabled"] = True
        config["official_query_scopes"] = list(config.get("official_admin_scopes", []))
        config["admin_kicks_enabled"] = admin_enabled
        config["official_admin_scopes"] = []
        plugin = pt.WardogsPlugin(context, config)
        plugin.ready = True
        plugin.admin_kicks = SimpleNamespace(
            handle=AsyncMock(), clear_drafts=lambda *args: None
        )
        plugin.assessor = lambda *args: (_ for _ in ()).throw(
            AssertionError("LLM_FORBIDDEN")
        )
        client = SimpleNamespace(server_identity=AsyncMock(return_value=snapshot()))
        db = sqlite3.connect(":memory:")
        event.send = AsyncMock()
        from wardogs.history import LocalHistory
        from wardogs.queries import PublicQueries

        plugin.public_queries = PublicQueries(
            client, LocalHistory(SimpleNamespace(db=db))
        )
        try:
            await plugin.on_public_query(event)
            if not event.is_stopped():
                await plugin.on_admin_order(event)
            plugin.admin_kicks.handle.assert_not_awaited()
            return client.server_identity.await_count, event.send, event.is_stopped()
        finally:
            db.close()

    async def test_official_admin_actual_event_queries_without_model(self):
        count, sent, stopped = await self.deliver()
        self.assertEqual(count, 1)
        self.assertTrue(stopped)
        self.assertIn(SID, str(sent.call_args))

    async def test_member_query_scope_works_without_admin_feature(self):
        count, sent, stopped = await self.deliver(role="member", admin_enabled=False)
        self.assertEqual(count, 1)
        self.assertTrue(stopped)
        self.assertIn(SID, str(sent.call_args))
        self.assertIn("游戏左下角", sent.call_args.args[0].chain[0].text)

    async def test_unmentioned_event_does_not_query_or_send(self):
        count, sent, _ = await self.deliver(mention=False)
        self.assertEqual(count, 0)
        sent.assert_not_awaited()

    async def test_wrong_scope_event_does_not_query_or_send(self):
        count, sent, _ = await self.deliver(scoped=False)
        self.assertEqual(count, 0)
        sent.assert_not_awaited()

    async def test_onebot_member_queries_without_role_or_admin_feature(self):
        import astrbot.api.message_components as Comp
        import test_plugin as pt

        from wardogs.history import LocalHistory
        from wardogs.queries import PublicQueries

        event = pt.PluginTests().event("服地址", images=False, group="123")
        event.message_obj.message.append(Comp.At(qq=pt.BOT_ID))
        event.send = AsyncMock()
        context = SimpleNamespace()
        config = dict(
            public_queries_enabled=True,
            platform_id="default",
            bot_account_id=pt.BOT_ID,
            allowed_groups=["123"],
            admin_kicks_enabled=False,
        )
        plugin = pt.WardogsPlugin(context, config)
        plugin.ready = True
        plugin.assessor = lambda *args: (_ for _ in ()).throw(
            AssertionError("LLM_FORBIDDEN")
        )
        db = sqlite3.connect(":memory:")
        client = SimpleNamespace(server_identity=AsyncMock(return_value=snapshot()))
        plugin.public_queries = PublicQueries(
            client, LocalHistory(SimpleNamespace(db=db))
        )
        try:
            await plugin.on_public_query(event)
            self.assertTrue(event.is_stopped())
            self.assertIn(SID, event.send.call_args.args[0].chain[0].text)
            self.assertIn("游戏左下角", event.send.call_args.args[0].chain[0].text)
            client.server_identity.assert_awaited_once()
        finally:
            db.close()


class ClientHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_read_only_path_query_token_and_response_cap(self):
        import test_io as io_fixture

        fixture = io_fixture.ClientTests()
        await fixture.asyncSetUp()
        try:
            await fixture.c.server_identity()
            self.assertEqual(
                fixture.calls[0][:3],
                ("GET", "/v1/server-identity", "Bearer fake-test-token"),
            )
            fixture.mode = "large"
            from wardogs.io import ServiceError

            with self.assertRaises(ServiceError):
                await fixture.c.server_identity()
        finally:
            await fixture.asyncTearDown()
