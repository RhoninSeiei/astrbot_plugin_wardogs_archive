import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import test_player_kills as fixture
from test_admin import WHO, A, B

from wardogs.sanction_summary import describe, query, receipt_notice, validate


def summary(target=A, bans=2, unbans=2):
    return {
        "observedAt": time.time(),
        "steamId": target,
        "countingRule": "verified_sanction_cycles_v1",
        "coverage": "retained_local_sanction_ledger",
        "complete": True,
        "historyComplete": False,
        "countsAreLowerBounds": True,
        "banCount": bans,
        "unbanCount": unbans,
        "unknownItems": 0,
        "alerts": {"repeatedBan": bans > 1, "repeatedUnban": unbans > 1},
    }


class SanctionSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_payload_exact_identity_and_freshness(self):
        client = SimpleNamespace(
            player_sanction_summary=AsyncMock(return_value=summary())
        )
        result = await query(client, WHO, "admin", A)
        self.assertEqual(result["banCount"], 2)
        body = client.player_sanction_summary.call_args.args[0]
        self.assertEqual(set(body), {"steamId", "actor", "requestedAt"})
        self.assertEqual(body["steamId"], A)
        self.assertEqual(body["actor"]["role"], "admin")
        self.assertIn("封禁至少2次", describe(result))
        self.assertIn("解封至少2次", describe(result))
        self.assertIn("管理员提醒", describe(result))

    def test_bad_counts_states_and_identity_are_rejected(self):
        mutations = [
            lambda x: x.update(steamId=B),
            lambda x: x.update(observedAt=time.time() - 16),
            lambda x: x.update(banCount=True),
            lambda x: x.update(unbanCount=-1),
            lambda x: x.update(unknownItems=False),
            lambda x: x.update(complete=False),
            lambda x: x.update(historyComplete=True),
            lambda x: x.update(countsAreLowerBounds=False),
            lambda x: x.update(coverage="all_time"),
            lambda x: x.update(countingRule="count_rows"),
            lambda x: x["alerts"].update(repeatedBan=False),
        ]
        for mutate in mutations:
            data = summary()
            mutate(data)
            with self.assertRaises(ValueError):
                validate(data, A)

    async def test_member_must_not_query_or_receive_a_summary(self):
        client = SimpleNamespace(player_sanction_summary=AsyncMock())
        self.assertIsNone(await query(client, WHO, "member", A))
        client.player_sanction_summary.assert_not_awaited()

    async def test_unavailable_is_not_reported_as_zero(self):
        client = SimpleNamespace(
            player_sanction_summary=AsyncMock(side_effect=TimeoutError())
        )
        text = describe(await query(client, WHO, "admin", A))
        self.assertIn("暂不可核验", text)
        self.assertNotIn("0次", text)

    def test_exact_threshold_excludes_one_and_does_not_claim_whole_history(self):
        text = describe(summary(bans=1, unbans=1))
        self.assertNotIn("管理员提醒", text)
        self.assertIn("本地可核验", text)
        self.assertIn("至少1次", text)
        self.assertNotIn("累计共", text)

    async def test_terminal_receipt_uses_current_count_and_keeps_one_callback(self):
        replies = []
        client = SimpleNamespace(
            player_sanction_summary=AsyncMock(return_value=summary(bans=2, unbans=1))
        )
        callback = receipt_notice(client, WHO, "admin", A, replies.append)
        await callback("已踢出：" + A)
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0].startswith("已踢出：" + A))
        self.assertIn("管理员提醒", replies[0])
        self.assertIn("封禁至少2次", replies[0])


class SanctionHistoryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PlayerKillTests.asyncSetUp
    asyncTearDown = fixture.PlayerKillTests.asyncTearDown
    handle = fixture.PlayerKillTests.handle

    async def test_query_confirmation_and_action_keep_the_same_target(self):
        self.client.player_sanction_summary = AsyncMock(
            side_effect=lambda p: summary(p["steamId"])
        )
        await self.handle("查 " + A)
        self.assertIn("管理员提醒", "\n".join(self.replies))
        await self.handle("1")
        self.assertIn("管理员提醒", self.replies[-1])
        self.assertFalse(self.client.calls)
        await self.handle("1")
        await self.actions.poll()
        self.assertEqual(self.client.calls[0][0]["steamId"], A)
        self.assertTrue(
            all(
                c.args[0]["steamId"] == A
                for c in self.client.player_sanction_summary.call_args_list
            )
        )

    async def test_member_cannot_obtain_counts_through_history(self):
        self.client.player_sanction_summary = AsyncMock()
        await self.handle("查 " + A, role="member")
        self.client.player_sanction_summary.assert_not_awaited()


class SummaryHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_client_readonly_route_uses_admin_and_no_idempotency_key(self):
        import tempfile
        from pathlib import Path

        from aiohttp import web

        from wardogs.io import ServiceClient

        calls = []

        async def handler(request):
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.path, "/v1/moderation/player-sanction-summary")
            self.assertEqual(
                request.headers["Authorization"], "Bearer fixture-admin-key"
            )
            self.assertNotIn("Idempotency-Key", request.headers)
            body = await request.json()
            calls.append(body)
            return web.json_response(summary(body["steamId"]))

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "query").write_text("fixture-query-key")
            (root / "admin").write_text("fixture-admin-key")
            client = ServiceClient(
                "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1]),
                root / "query",
                admin_key_file=root / "admin",
            )
            try:
                self.assertIn(
                    "管理员提醒", describe(await query(client, WHO, "owner", A))
                )
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["actor"]["role"], "owner")
            finally:
                await client.close()
                await runner.cleanup()


class RecordSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_punishment_history_displays_summary_without_punishing(self):
        import test_punishment_records as records

        test = records.RecordTests()
        await test.asyncSetUp()
        try:
            test.client.player_sanction_summary = AsyncMock(return_value=summary())
            await test.handle("封禁原因 " + A)
            text = "\n".join(test.replies)
            self.assertIn("管理员提醒", text)
            self.assertIn("至少2次", text)
            self.assertFalse(test.client.calls)
        finally:
            await test.asyncTearDown()
