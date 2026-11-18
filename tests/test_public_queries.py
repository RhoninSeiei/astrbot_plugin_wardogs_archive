import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import astrbot.api.message_components as Comp
import test_admin_auth as auth
import test_plugin as pt

from wardogs.domain import Identity, Message, Store
from wardogs.history import LocalHistory
from wardogs.model import DEFAULT_PROVIDER, FALLBACK_PROVIDER, ContextModel
from wardogs.queries import PublicQueries


def snapshot():
    return {
        "observedAt": time.time(),
        "playerCount": 31,
        "maxPlayers": 64,
        "map": "TestMap",
        "mode": "Control",
        "teams": [
            {"id": "red", "name": "红方", "score": 120, "playerCount": 10},
            {"id": "blue", "name": "蓝方", "score": 95, "playerCount": 11},
            {"id": "green", "name": "绿方", "score": 87, "playerCount": 10},
        ],
        "scoreAvailable": True,
        "scoreKind": "tickets",
    }


class PublicQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db")
        self.history = LocalHistory(self.store)
        self.client = SimpleNamespace(
            match=AsyncMock(side_effect=lambda: snapshot()),
            state=AsyncMock(
                side_effect=lambda: {
                    "playerCount": 31,
                    "lastSuccessfulPoll": time.time(),
                    "lastError": None,
                }
            ),
            admin_kick=AsyncMock(side_effect=AssertionError("forbidden")),
        )
        self.context = SimpleNamespace(llm_generate=AsyncMock())
        self.model = ContextModel(self.context, "synthetic")
        self.answer("both")
        self.queries = PublicQueries(self.client, self.history)
        self.replies = []

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def answer(self, query):
        self.context.llm_generate.side_effect = None
        self.context.llm_generate.return_value = SimpleNamespace(
            completion_text=json.dumps({"query": query, "confidence": 0.99})
        )

    async def handle(self, text="现在比分和在线人数多少", mid="msg"):
        return await self.queries.handle(
            Message(
                Identity("official:fixture:app", "g", "member"), mid, time.time(), text
            ),
            self.model,
            self.replies.append,
        )

    async def test_population_from_fresh_state_for_any_member(self):
        self.answer("population")
        self.assertTrue(await self.handle("服里现在多少人"))
        self.assertIn("31", self.replies[-1])
        self.client.match.assert_awaited_once()
        self.client.state.assert_not_awaited()
        self.client.admin_kick.assert_not_awaited()

    async def test_score_and_population_are_actual_snapshot_values(self):
        await self.handle()
        text = self.replies[-1]
        for value in ["31", "64", "120", "95", "87", "红方", "TestMap"]:
            self.assertIn(value, text)
        self.client.admin_kick.assert_not_awaited()
        self.context.llm_generate.assert_not_awaited()

    async def test_real_zero_score_is_preserved(self):
        data = snapshot()
        data["teams"][1]["score"] = 0
        self.client.match.side_effect = None
        self.client.match.return_value = data
        await self.handle()
        self.assertIn("蓝方 0", self.replies[-1])

    async def test_score_missing_is_not_invented(self):
        data = snapshot()
        data.update(scoreAvailable=False, teams=[])
        self.client.match.side_effect = None
        self.client.match.return_value = data
        await self.handle()
        self.assertIn("比分暂不可用", self.replies[-1])
        self.assertIn("31", self.replies[-1])
        self.assertNotIn("0:0", self.replies[-1])

    async def test_stale_score_not_shown_and_fresh_population_fallback(self):
        data = snapshot()
        data["observedAt"] = time.time() - 1000
        self.client.match.side_effect = None
        self.client.match.return_value = data
        await self.handle()
        self.assertIn("31", self.replies[-1])
        self.assertIn("比分暂不可用", self.replies[-1])
        self.assertNotIn("120", self.replies[-1])

    async def test_both_endpoints_unavailable_is_explicit(self):
        self.client.match.side_effect = RuntimeError()
        self.client.state.side_effect = RuntimeError()
        await self.handle()
        self.assertIn("暂不可用", self.replies[-1])
        self.assertNotIn("31", self.replies[-1])

    async def test_population_fallback_uses_fresh_roster_count(self):
        self.answer("population")
        self.client.match.side_effect = RuntimeError()
        await self.handle("服务器多少人")
        self.assertIn("31", self.replies[-1])
        self.client.state.assert_awaited_once()

    async def test_invalid_population_not_rendered(self):
        self.answer("population")
        self.client.match.side_effect = RuntimeError()
        self.client.state.side_effect = None
        for value in [True, -1, "31"]:
            self.client.state.return_value = {
                "lastSuccessfulPoll": time.time(),
                "playerCount": value,
            }
            await self.handle("多少人", str(value))
            self.assertIn("暂不可用", self.replies[-1])

    async def test_bad_score_boolean_or_missing_team_not_rendered(self):
        self.answer("score")
        self.client.match.side_effect = None
        for teams in [[{"id": "r", "name": "red", "score": True}], []]:
            data = snapshot()
            data["teams"] = teams
            self.client.match.return_value = data
            await self.handle("比分", str(len(teams)))
            self.assertNotIn("120", self.replies[-1])
            self.assertIn("暂不可用", self.replies[-1])

    async def test_no_query_leaves_other_handlers_untouched(self):
        self.answer("none")
        self.assertFalse(await self.handle("晚上一起玩"))
        self.assertFalse(self.replies)
        self.client.match.assert_not_awaited()
        self.client.state.assert_not_awaited()

    async def test_mutation_word_is_not_laundered_through_query(self):
        for text in ["查人数并封禁Example", "看看比分再踢人", "举报Example圈外击杀"]:
            self.assertFalse(await self.handle(text, text))
        self.client.admin_kick.assert_not_awaited()
        self.context.llm_generate.assert_not_awaited()

    async def test_malformed_primary_uses_luna(self):
        self.context.llm_generate.side_effect = [
            SimpleNamespace(completion_text='{"query":"execute"}'),
            SimpleNamespace(completion_text='{"query":"population","confidence":0.99}'),
        ]
        await self.handle("请告知此刻战场的兵力规模以及各阵营交锋结果")
        self.assertIn("31", self.replies[-1])
        self.assertIsNone(self.context.llm_generate.await_args.kwargs["tools"])
        self.assertEqual(
            [
                c.kwargs["chat_provider_id"]
                for c in self.context.llm_generate.await_args_list
            ],
            [DEFAULT_PROVIDER, FALLBACK_PROVIDER],
        )

    async def test_model_failure_has_no_service_calls(self):
        self.context.llm_generate.side_effect = RuntimeError()
        await self.handle("请告知此刻战场的兵力规模以及各阵营交锋结果")
        self.assertIn("查询", self.replies[-1])
        self.client.match.assert_not_awaited()
        self.client.state.assert_not_awaited()

    async def test_duplicate_receipt_survives_restart_without_llm_or_repeat_send(self):
        await self.handle()
        self.queries = PublicQueries(self.client, self.history)
        await self.handle()
        self.context.llm_generate.assert_not_awaited()
        self.assertEqual(len(self.replies), 1)


class PublicEventTests(unittest.IsolatedAsyncioTestCase):
    event = pt.PluginTests.event
    setup_official = auth.AdminAuthTests.setup_official

    def plugin(self):
        e, c, cfg = self.setup_official("member")
        cfg.update(
            public_queries_enabled=True,
            official_query_scopes=cfg["official_admin_scopes"],
        )
        p = pt.WardogsPlugin(c, cfg)
        p.ready = True
        p.public_queries = SimpleNamespace(handle=AsyncMock(return_value=True))
        e.send = AsyncMock()
        e.message_obj.message = [
            Comp.At(qq="qq_official"),
            Comp.Plain("现在多少人，比分呢"),
        ]
        return e, c, cfg, p

    async def test_member_event_uses_readonly_scope_without_role_lookup(self):
        e, c, cfg, p = self.plugin()
        await p.on_public_query(e)
        p.public_queries.handle.assert_awaited_once()
        self.assertEqual(
            p.public_queries.handle.await_args.args[0].who.platform,
            "qq_official:official-fixture:1234567890",
        )

    async def test_no_at_wrong_group_wrong_app_empty_or_disabled_scope_block(self):
        for mode in ["no_at", "group", "app", "empty", "disabled", "raw_mismatch"]:
            e, c, cfg, p = self.plugin()
            if mode == "no_at":
                e.message_obj.message = [Comp.Plain("多少人")]
            if mode == "group":
                e.message_obj.group.group_id = "other"
            if mode == "app":
                c.get_platform_inst = lambda _: SimpleNamespace(
                    appid="1234567899", meta=lambda: SimpleNamespace(name="qq_official")
                )
            if mode == "empty":
                cfg["official_query_scopes"] = []
            if mode == "disabled":
                cfg["public_queries_enabled"] = False
            if mode == "raw_mismatch":
                e.message_obj.raw_message.raw_data["group_openid"] = "other"
            await p.on_public_query(e)
            p.public_queries.handle.assert_not_awaited()

    async def test_query_tools_do_not_grant_player_detail_report_or_announcement(self):
        e, c, cfg, p = self.plugin()
        p.public_queries.render = AsyncMock(return_value="服务器在线人数：31人。")
        self.assertIn("31", await p.match_tool(e))
        self.assertIn("31", await p.status_tool(e))
        self.assertIn("未启用", await p.players_tool(e))
        self.assertIn("未启用", await p.report_tool(e, "report"))
        self.assertIn("权限", await p.announcement_tool(e, "中文", "English", "日本語"))

    async def test_read_scope_does_not_enable_admin_scope(self):
        e, c, cfg, p = self.plugin()
        cfg["official_admin_scopes"] = []
        p.admin_kicks = SimpleNamespace(handle=AsyncMock())
        e.message_obj.message = [Comp.At(qq="qq_official"), Comp.Plain("封禁Example")]
        await p.on_admin_order(e)
        p.admin_kicks.handle.assert_not_awaited()


class PublicReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_and_fresh_snapshot_pass_only_get(self):
        from wardogs.readiness import verify_query_deployment

        client = SimpleNamespace(
            request=AsyncMock(return_value={"paths": {"/v1/match": {}}}),
            match=AsyncMock(return_value=snapshot()),
        )
        result = await verify_query_deployment(client)
        self.assertTrue(result["ready"])
        client.request.assert_awaited_once_with("GET", "/openapi.json")

    async def test_missing_route_and_stale_snapshot_fail(self):
        from wardogs.readiness import verify_query_deployment

        client = SimpleNamespace(
            request=AsyncMock(return_value={"paths": {}}),
            match=AsyncMock(return_value=snapshot()),
        )
        with self.assertRaises(ValueError):
            await verify_query_deployment(client)
        client.request.return_value = {"paths": {"/v1/match": {}}}
        data = snapshot()
        data["observedAt"] = 0
        client.match.return_value = data
        with self.assertRaises(ValueError):
            await verify_query_deployment(client)
