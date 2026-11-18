import asyncio
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import astrbot.api.message_components as Comp
import test_admin_auth as auth
import test_plugin as pt

from wardogs.history import LocalHistory
from wardogs.official_menu import keyboard
from wardogs.queries import PublicQueries

JoinError = pt.plugin_main.handle_official_menu.__globals__["JoinError"]
NativeJoinAPI = pt.plugin_main.handle_official_menu.__globals__["JoinAPI"]


class MenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(":memory:")
        self.event, self.context, self.config = auth.AdminAuthTests().setup_official(
            "member"
        )
        self.config.update(
            public_queries_enabled=True,
            official_query_scopes=list(self.config["official_admin_scopes"]),
            official_only=True,
        )
        self.event.message_obj.message = [Comp.Plain("菜单"), Comp.At(qq="qq_official")]
        self.event.send = AsyncMock()
        self.api = SimpleNamespace(
            request=AsyncMock(return_value={"id": "synthetic-message"})
        )
        self.api.close = AsyncMock()
        self.factory = patch.dict(
            pt.plugin_main.handle_official_menu.__globals__,
            JoinAPI=lambda *args: self.api,
        )
        self.factory.start()
        self.plugin = pt.WardogsPlugin(self.context, self.config)
        self.plugin.ready = True
        self.plugin.public_queries = PublicQueries(
            SimpleNamespace(), LocalHistory(SimpleNamespace(db=self.db))
        )
        self.plugin.assessor = lambda *args: (_ for _ in ()).throw(
            AssertionError("LLM_FORBIDDEN")
        )
        self.plugin.admin_kicks = SimpleNamespace(handle=AsyncMock())

    async def asyncTearDown(self):
        self.factory.stop()
        self.db.close()

    def text(self, text):
        self.event.message_obj.message = [Comp.Plain(text), Comp.At(qq="qq_official")]

    async def test_public_menu_uses_native_markdown_keyboard_once(self):
        await self.plugin.on_public_query(self.event)
        await self.plugin.on_public_query(self.event)
        self.api.request.assert_awaited_once()
        kw = self.api.request.call_args.kwargs["json"]
        self.assertEqual(kw["msg_type"], 2)
        self.assertEqual(self.api.request.call_args.args, ("POST", "/messages"))
        self.assertEqual(kw["msg_id"], self.event.message_obj.message_id)
        self.assertEqual(len(kw["keyboard"]["content"]["rows"]), 3)
        self.assertTrue(self.event.is_stopped())
        self.event.send.assert_not_awaited()
        self.plugin.admin_kicks.handle.assert_not_awaited()

    async def test_admin_menu_does_not_execute_and_punishment_buttons_only_fill(self):
        self.event.message_obj.raw_message.raw_data["author"]["member_role"] = "admin"
        self.text("/管理菜单")
        await self.plugin.on_public_query(self.event)
        kw = self.api.request.call_args.kwargs["json"]
        rows = kw["keyboard"]["content"]["rows"]
        self.assertEqual(len(rows), 5)
        for b in [b for r in rows for b in r["buttons"]]:
            action = b["action"]
            self.assertEqual(action["type"], 2)
            if action["data"].strip() in ("踢", "循环", "解封", "封禁原因", "击杀记录"):
                self.assertFalse(action["enter"])
                self.assertEqual(action["permission"]["type"], 1)
        self.assertTrue(
            any(b["action"]["data"] == "击杀记录 " for r in rows for b in r["buttons"])
        )
        self.plugin.admin_kicks.handle.assert_not_awaited()

    async def test_member_cannot_get_admin_menu(self):
        self.text("管理菜单")
        await self.plugin.on_public_query(self.event)
        self.api.request.assert_not_awaited()
        self.assertIn("管理员", self.event.send.call_args.args[0].chain[0].text)

    async def test_role_without_admin_scope_does_not_get_admin_menu(self):
        self.text("管理菜单")
        self.event.message_obj.raw_message.raw_data["author"]["member_role"] = "admin"
        self.config["official_admin_scopes"] = []
        await self.plugin.on_public_query(self.event)
        self.api.request.assert_not_awaited()

    async def test_wrong_query_scope_or_missing_mention_sends_nothing(self):
        self.config["official_query_scopes"] = []
        await self.plugin.on_public_query(self.event)
        self.api.request.assert_not_awaited()
        self.event.send.assert_not_awaited()
        self.config["official_query_scopes"] = list(
            self.config["official_admin_scopes"]
        )
        self.event.message_obj.message = [Comp.Plain("菜单")]
        await self.plugin.on_public_query(self.event)
        self.api.request.assert_not_awaited()

    async def test_explicit_platform_rejection_falls_back_to_text(self):
        self.api.request.side_effect = JoinError(403, "synthetic_denied")
        await self.plugin.on_public_query(self.event)
        self.event.send.assert_awaited_once()
        self.assertIn("服id", self.event.send.call_args.args[0].chain[0].text)

    async def test_timeout_and_none_result_not_replayed_or_fallback_sent(self):
        self.api.request.side_effect = TimeoutError("private")
        await self.plugin.on_public_query(self.event)
        await self.plugin.on_public_query(self.event)
        self.api.request.assert_awaited_once()
        self.event.send.assert_not_awaited()
        self.event.message_obj.message_id = "second"
        self.api.request.side_effect = None
        self.api.request.return_value = None
        await self.plugin.on_public_query(self.event)
        self.assertEqual(self.api.request.await_count, 2)
        self.event.send.assert_not_awaited()

    async def test_disabled_keyboard_uses_text_only(self):
        self.config["official_menu_buttons_enabled"] = False
        await self.plugin.on_public_query(self.event)
        self.api.request.assert_not_awaited()
        self.event.send.assert_awaited_once()

    async def test_cancellation_claim_survives_reentry(self):
        self.api.request.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin.on_public_query(self.event)
        await self.plugin.on_public_query(self.event)
        self.api.request.assert_awaited_once()

    def test_button_limits_and_no_dynamic_player_ids(self):
        for admin in [False, True]:
            rows = keyboard(admin)["content"]["rows"]
            self.assertLessEqual(len(rows), 5)
            self.assertTrue(all(len(r["buttons"]) <= 5 for r in rows))
            ids = [b["id"] for r in rows for b in r["buttons"]]
            self.assertEqual(len(ids), len(set(ids)))


class MenuHttpTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = MenuTests.asyncSetUp
    asyncTearDown = MenuTests.asyncTearDown

    async def test_real_http_transport_fixed_native_route_and_rejection_fallback(self):
        from aiohttp import web

        seen = []
        status = 200

        async def handler(req):
            seen.append(
                (
                    req.method,
                    req.path,
                    req.headers.get("Authorization"),
                    await req.json(),
                )
            )
            return web.json_response(
                {"id": "synthetic-message"}
                if status == 200
                else {"code": 123, "message": "synthetic denied"},
                status=status,
            )

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        http = SimpleNamespace(
            check_session=AsyncMock(), _headers={"Authorization": "synthetic-token"}
        )
        platform = SimpleNamespace(
            get_client=lambda: SimpleNamespace(api=SimpleNamespace(_http=http))
        )

        def make(*args):
            api = NativeJoinAPI(platform, "g")
            api.base = "http://127.0.0.1:" + str(
                site._server.sockets[0].getsockname()[1]
            )
            return api

        try:
            with patch.dict(
                pt.plugin_main.handle_official_menu.__globals__, JoinAPI=make
            ):
                await self.plugin.on_public_query(self.event)
                self.event.message_obj.message_id = "next-menu"
                status = 400
                await self.plugin.on_public_query(self.event)
            self.assertEqual(len(seen), 2)
            self.assertTrue(
                all(
                    row[:3] == ("POST", "/v2/groups/g/messages", "synthetic-token")
                    for row in seen
                )
            )
            self.assertEqual(seen[0][3]["msg_type"], 2)
            self.assertIn("keyboard", seen[0][3])
            self.event.send.assert_awaited_once()
        finally:
            await runner.cleanup()
