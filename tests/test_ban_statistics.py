import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import test_public_queries as fixture


class BanStatisticsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PublicQueryTests.asyncSetUp
    asyncTearDown = fixture.PublicQueryTests.asyncTearDown
    handle = fixture.PublicQueryTests.handle
    answer = fixture.PublicQueryTests.answer

    def stats(self, **updates):
        data = {
            "observedAt": time.time(),
            "countingRule": "unique_first_effective_ban_v1",
            "totalPlayers": 100,
            "last24HoursPlayers": 8,
            "last12HoursPlayers": 3,
            "unknownTimePlayers": 2,
            "recordsSince": time.time() - 86400 * 30,
            "historyComplete": False,
        }
        data.update(updates)
        self.client.ban_statistics = AsyncMock(return_value=data)
        return data

    async def test_summary_is_local_and_shows_only_combined_counts(self):
        self.stats()
        for text in [
            "封禁统计",
            "封禁人数统计",
            "封了多少人",
            "服务器累计封禁人数",
            "总共封禁了几个人",
            "查一下封号统计",
            "封进统计",
            "/wd 封禁统计",
            "請問，封禁統計？",
        ]:
            with self.subTest(text=text):
                self.assertTrue(await self.handle(text, text))
                output = self.replies[-1]
                for part in [
                    "累计封禁：100人",
                    "过去24小时：8人",
                    "过去12小时：3人",
                    "截止",
                    "北京时间",
                ]:
                    self.assertIn(part, output)
                self.assertNotIn("原生", output)
                self.assertNotIn("永久", output)
        self.context.llm_generate.assert_not_awaited()
        self.client.match.assert_not_awaited()
        self.client.state.assert_not_awaited()
        self.client.admin_kick.assert_not_awaited()

    async def test_12_and_24_hour_queries_are_local(self):
        self.stats()
        for text in [
            "过去24小时封禁人数",
            "过去24小时内的封禁人数",
            "最近24h封了多少人",
            "封禁统计24小时",
            "查下过去二十四小时封号统计",
            "近12小时封禁人数",
            "最近十二小时封了几个人",
            "封禁人数12h",
        ]:
            with self.subTest(text=text):
                self.assertTrue(await self.handle(text, text))
                if "12" in text or "十二" in text:
                    self.assertIn("过去12小时：3人", self.replies[-1])
                    self.assertNotIn("过去24小时", self.replies[-1])
                else:
                    self.assertIn("过去24小时：8人", self.replies[-1])
                    self.assertNotIn("过去12小时", self.replies[-1])
        self.context.llm_generate.assert_not_awaited()

    async def test_stats_never_capture_administrator_actions_or_quoted_requests(self):
        self.stats()
        for text in [
            "封禁Example",
            "封禁76561190000000001",
            "确认踢出1",
            "封禁统计并踢出Example",
            "别查封禁统计",
            "他说封禁统计",
            "“封禁统计”",
            "查询其他群封禁人数",
            "近48小时封禁人数",
        ]:
            self.assertFalse(await self.handle(text, text), text)
        self.client.ban_statistics.assert_not_awaited()
        self.context.llm_generate.assert_not_awaited()
        self.client.admin_kick.assert_not_awaited()

    async def test_missing_endpoint_and_invalid_payload_never_invent_zero(self):
        self.stats()
        self.client.ban_statistics.side_effect = RuntimeError("not implemented")
        self.assertTrue(await self.handle("封禁统计"))
        self.assertIn("暂不可用", self.replies[-1])
        self.assertNotIn("0人", self.replies[-1])
        self.context.llm_generate.assert_not_awaited()

    async def test_inconsistent_counts_and_unknown_definition_are_refused(self):
        for changes in [
            {"totalPlayers": True},
            {"last24HoursPlayers": -1},
            {"last12HoursPlayers": 9},
            {"totalPlayers": 7},
            {"unknownTimePlayers": 99},
            {"countingRule": "kick_rows"},
            {"historyComplete": "false"},
            {"recordsSince": time.time() + 100},
            {"observedAt": time.time() - 1000},
            {"observedAt": time.time() + 100},
            {"totalPlayers": "100"},
        ]:
            self.stats(**changes)
            await self.handle("封禁统计", str(changes))
            self.assertIn("暂不可用", self.replies[-1])
            self.assertNotIn("累计封禁：", self.replies[-1])

    async def test_real_zero_and_incomplete_coverage_are_explicit(self):
        self.stats(
            totalPlayers=0,
            last24HoursPlayers=0,
            last12HoursPlayers=0,
            unknownTimePlayers=0,
            recordsSince=None,
        )
        await self.handle("封禁统计")
        self.assertIn("累计封禁：0人", self.replies[-1])
        self.assertIn("现存记录", self.replies[-1])
        self.assertNotIn("开服至今完整", self.replies[-1])

    async def test_dedup_avoids_repeated_queries_and_replies(self):
        self.stats()
        await self.handle("封禁统计")
        await self.handle("封禁统计")
        self.client.ban_statistics.assert_awaited_once()
        self.assertEqual(len(self.replies), 1)


class BanStatisticsHttpTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PublicQueryTests.asyncSetUp
    asyncTearDown = fixture.PublicQueryTests.asyncTearDown
    stats = BanStatisticsTests.stats
    answer = fixture.PublicQueryTests.answer

    async def test_member_event_uses_query_token_and_only_get_without_llm(self):
        import astrbot.api.message_components as Comp
        from aiohttp import web

        from wardogs.io import ServiceClient
        from wardogs.queries import PublicQueries

        payload = self.stats()
        calls = []

        async def handler(request):
            calls.append(
                (request.method, request.path, request.headers.get("Authorization"))
            )
            return web.json_response(payload)

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        from pathlib import Path

        key = Path(self.tmp.name) / "query-key"
        key.write_text("fake-query-token")
        admin = Path(self.tmp.name) / "admin-key"
        admin.write_text("fake-admin-token")
        client = ServiceClient(
            "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1]),
            key,
            admin,
            admin,
        )
        event, context, config, plugin = fixture.PublicEventTests().plugin()
        context.llm_generate = AsyncMock(side_effect=AssertionError("LLM forbidden"))
        plugin.admin_kicks = SimpleNamespace(
            handle=AsyncMock(side_effect=AssertionError("punishment forbidden"))
        )
        plugin.public_queries = PublicQueries(client, self.history)
        event.message_obj.message = [Comp.At(qq="qq_official"), Comp.Plain("封禁统计")]
        try:
            await plugin.on_public_query(event)
            self.assertTrue(event.is_stopped())
            self.assertIn(
                "累计封禁：100人", event.send.await_args.args[0].chain[0].text
            )
            self.assertEqual(
                calls,
                [("GET", "/v1/moderation/ban-statistics", "Bearer fake-query-token")],
            )
            context.llm_generate.assert_not_awaited()
            plugin.admin_kicks.handle.assert_not_awaited()
            self.assertFalse(plugin.intake_tasks)
        finally:
            await client.close()
            await runner.cleanup()

    async def test_read_scope_still_requires_at_and_original_group(self):
        import astrbot.api.message_components as Comp

        from wardogs.queries import PublicQueries

        self.stats()
        for mode in ["no_at", "other_group", "disabled"]:
            event, context, config, plugin = fixture.PublicEventTests().plugin()
            event.message_obj.message = [
                Comp.At(qq="qq_official"),
                Comp.Plain("封禁统计"),
            ]
            plugin.public_queries = PublicQueries(self.client, self.history)
            context.llm_generate = AsyncMock()
            if mode == "no_at":
                event.message_obj.message = [Comp.Plain("封禁统计")]
            if mode == "other_group":
                config["official_query_scopes"] = ["official-fixture|1234567890|other"]
            if mode == "disabled":
                config["public_queries_enabled"] = False
            await plugin.on_public_query(event)
            event.send.assert_not_awaited()
            self.client.ban_statistics.assert_not_awaited()
            context.llm_generate.assert_not_awaited()
