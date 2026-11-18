import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
from test_admin import WHO, A
from test_admin_bans import result
from test_player_kills import response

from wardogs.admin import AdminKicks
from wardogs.domain import Message, Store
from wardogs.io import ServiceClient


class PlayerKillHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_transport_query_numeric_ban_and_receipt(self):
        calls, submitted = [], []
        unavailable = False

        async def handler(req):
            calls.append((req.method, req.path))
            expected = (
                "fixture-query-token"
                if req.path == "/v1/players"
                else "fixture-admin-token"
            )
            self.assertEqual(req.headers.get("Authorization"), "Bearer " + expected)
            if req.path == "/v1/players":
                return web.json_response(
                    {
                        "observedAt": time.time(),
                        "players": [{"steamId": A, "name": "Fixture"}],
                    }
                )
            if req.path == "/v1/moderation/player-search":
                body = await req.json()
                self.assertEqual(body["actor"]["role"], "admin")
                return web.json_response(
                    {
                        "observedAt": time.time(),
                        "historyComplete": True,
                        "truncated": False,
                        "players": [
                            {
                                "steamId": A,
                                "name": "Fixture",
                                "online": True,
                                "aliases": [],
                                "source": "online",
                                "lastSeen": time.time(),
                            }
                        ],
                    }
                )
            if req.path == "/v1/moderation/player-sanction-summary":
                from test_sanction_summary import summary

                body = await req.json()
                return web.json_response(summary(body["steamId"], bans=0, unbans=0))
            if req.path == "/v1/moderation/player-kills":
                self.assertEqual(req.method, "POST")
                self.assertNotIn("Idempotency-Key", req.headers)
                body = await req.json()
                self.assertEqual(
                    set(body), {"steamId", "limit", "actor", "requestedAt"}
                )
                self.assertEqual(body["steamId"], A)
                self.assertEqual(body["actor"]["memberId"], WHO.sender)
                if unavailable:
                    return web.json_response(
                        {"error": {"code": "player_kills_sync_incomplete"}}, status=503
                    )
                return web.json_response(response(body["limit"]))
            if req.path == "/v1/moderation/admin-bans":
                self.assertEqual(req.method, "POST")
                body = await req.json()
                self.assertEqual(
                    set(body), {"requestId", "steamId", "actor", "requestedAt"}
                )
                self.assertTrue(
                    req.headers["Idempotency-Key"].startswith("wd-admin-ban-")
                )
                self.assertEqual(body["steamId"], A)
                submitted.append(body)
                return web.json_response(result(), status=202)
            if req.path == "/v1/moderation/operations/fixture-ban":
                return web.json_response(result("banned", "banned", "restricted"))
            self.fail("unexpected_route:" + req.path)

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "query").write_text("fixture-query-token")
            (root / "admin").write_text("fixture-admin-token")
            service = ServiceClient(
                "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1]),
                root / "query",
                admin_key_file=root / "admin",
            )
            store = Store(root / "db")
            controller = AdminKicks(store, service)
            replies = []
            model = SimpleNamespace(
                generate=AsyncMock(side_effect=AssertionError("LLM_FORBIDDEN"))
            )

            async def message(text, mid, role="admin"):
                return await controller.handle(
                    Message(WHO, mid, time.time(), text),
                    role,
                    replies.append,
                    model=model,
                )

            try:
                await message("击杀记录 Fixture 50", "member", "member")
                self.assertFalse(calls)
                await message("击杀记录 Fixture 50", "query")
                self.assertIn("选择玩家", replies[-1])
                await message("1", "identity")
                self.assertIn("记录50", "\n".join(replies))
                await message("2", "ban-choice")
                self.assertFalse(submitted)
                await message("1", "confirm")
                self.assertEqual(len(submitted), 1)
                self.assertEqual(replies[-1], "已受理")
                await controller.poll()
                self.assertIn("已封禁", replies[-1])
                await message("1", "confirm")
                self.assertEqual(len(submitted), 1)
                unavailable = True
                await message("击杀记录 " + A, "failed-query")
                self.assertIn("暂不可用", replies[-1])
                # No menu was published after the failed query.
                self.assertIsNone(
                    controller.kill_history.snapshot(
                        (WHO.platform, WHO.group, WHO.sender), time.time()
                    )
                )
                model.generate.assert_not_awaited()
            finally:
                controller.close()
                store.close()
                await service.close()
                await runner.cleanup()
