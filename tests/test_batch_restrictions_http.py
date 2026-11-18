import tempfile
import time
import unittest
from pathlib import Path

from aiohttp import web
from test_admin import WHO
from test_current_restrictions_client import state
from test_kill_replay import payload

from wardogs.admin import AdminKicks
from wardogs.domain import Message, Store
from wardogs.io import ServiceClient


class BatchHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_client_bulk_skip_confirm_register_poll_and_duplicate(self):
        snapshot = payload(2)
        targets = [e["killer"]["steamId"] for e in snapshot["events"]]
        posted = []
        calls = []
        fail = False

        async def handler(req):
            nonlocal fail
            calls.append((req.method, req.path, req.headers.get("Authorization")))
            if req.path == "/v1/players":
                self.assertEqual(req.method, "GET")
                self.assertEqual(
                    req.headers["Authorization"], "Bearer synthetic-query-token"
                )
                return web.json_response({"observedAt": time.time(), "players": []})
            self.assertEqual(
                req.headers["Authorization"], "Bearer synthetic-admin-token"
            )
            if req.path == "/v1/moderation/player-sanction-summary":
                from test_sanction_summary import summary

                body = await req.json()
                return web.json_response(summary(body["steamId"], bans=0, unbans=0))
            if req.path == "/v1/moderation/recent-kills":
                return web.json_response(snapshot)
            if req.path == "/v1/moderation/current-restrictions":
                body = await req.json()
                if fail:
                    return web.json_response({"code": "lookup_failed"}, status=503)
                sid = body["steamIds"][0]
                return web.json_response(
                    state(sid, automatic=sid == targets[0], local=sid == targets[0])
                )
            if req.path == "/v1/moderation/admin-restrictions":
                body = await req.json()
                self.assertEqual(body["actor"]["role"], "admin")
                self.assertEqual(body["steamId"], targets[1])
                self.assertTrue(
                    req.headers["Idempotency-Key"].startswith("wd-admin-restrict-")
                )
                posted.append((body, req.headers["Idempotency-Key"]))
                return web.json_response(
                    {"operationId": "ar-fixture", "restrictionStatus": "pending"},
                    status=202,
                )
            if req.path == "/v1/moderation/operations/ar-fixture":
                return web.json_response(
                    {
                        "operationId": "ar-fixture",
                        "steamId": targets[1],
                        "restrictionStatus": "restricted",
                    }
                )
            self.fail("unexpected_endpoint:" + req.path)

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query = root / "query"
            query.write_text("synthetic-query-token")
            admin = root / "admin"
            admin.write_text("synthetic-admin-token")
            client = ServiceClient(
                "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1]),
                query,
                admin_key_file=admin,
            )
            store = Store(root / "plugin.db")
            controller = AdminKicks(store, client)
            replies = []

            async def message(text, mid):
                return await controller.handle(
                    Message(WHO, mid, time.time(), text), "admin", replies.append
                )

            try:
                await message("回溯", "replay")
                await message("1 2", "select")
                self.assertIn("自动封禁", "".join(replies))
                self.assertIn("确认补封全部", "".join(replies))
                self.assertFalse(posted)
                fail = True
                await message("确认补封全部", "failed-confirm")
                self.assertFalse(posted)
                fail = False
                await message("回溯", "new-replay")
                await message("1 2", "new-select")
                await message("确认补封全部", "confirmed")
                await controller.poll()
                await message("确认补封全部", "confirmed")
                self.assertEqual(len(posted), 1)
                self.assertIn("批量补封结果", "".join(replies))
                self.assertIn("永久重入限制", "".join(replies))
                self.assertTrue(
                    all(path != "/v1/moderation/admin-kicks" for _, path, _ in calls)
                )
            finally:
                controller.close()
                store.close()
                await client.close()
                await runner.cleanup()
