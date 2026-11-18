"""Compact history queries through official group events and loopback substitutes."""

import unittest

import test_player_kills_chat_replay as base
from aiohttp import web
from test_admin import B
from test_player_kills import response


class CompactHistoryReplayTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.GroupHistoryReplayTests.asyncSetUp
    asyncTearDown = base.GroupHistoryReplayTests.asyncTearDown
    event = base.GroupHistoryReplayTests.event

    async def backend(self, request):
        if request.path == "/v1/moderation/player-kills" and getattr(
            self, "default_limit", False
        ):
            self.assertEqual(request.remote, "127.0.0.1")
            self.assertEqual(
                request.headers.get("Authorization"), "Bearer synthetic-admin-token"
            )
            body = await request.json()
            self.assertEqual(body["steamId"], B)
            self.assertEqual(body["limit"], 20)
            self.assertEqual(body["actor"]["role"], "admin")
            self.history_queries.append(body)
            return web.json_response(response(20, target=B))
        return await base.GroupHistoryReplayTests.backend(self, request)

    async def test_compact_query_defaults_to_twenty(self):
        self.default_limit = True
        await self.event("查打灰")
        self.assertIn("选择玩家", self.transcript[-1]["text"])
        await self.event("2")
        self.assertEqual(self.history_queries[0]["limit"], 20)
        text = "\n".join(
            m["text"] for m in self.transcript if m["speaker"] == "管理员1号"
        )
        self.assertIn("记录20", text)
        self.assertNotIn("记录21", text)
        self.assertFalse(self.posted)
        self.model.generate.assert_not_awaited()

    receipt = staticmethod(base.GroupHistoryReplayTests.receipt)
    replay = base.GroupHistoryReplayTests.replay

    async def start_query(self):
        self.players[0]["name"] = "KAIKAI_A"
        self.players[1]["name"] = "KAIKAI"
        await self.event("查KAIKAI 50")
        self.assertIn("【1】KAIKAI " + B, self.transcript[-1]["text"])
        self.assertFalse(self.history_queries)
        self.assertFalse(self.posted)
        await self.event("1")
        self.assertEqual(len(self.history_queries), 1)
        self.assertEqual(self.history_queries[0]["steamId"], B)
        self.assertEqual(self.history_queries[0]["limit"], 50)
        text = "\n".join(
            m["text"] for m in self.transcript if m["speaker"] == "管理员1号"
        )
        self.assertIn("记录50", text)
        self.assertIn("【1】踢出", self.transcript[-1]["text"])
        self.assertIn(B, self.transcript[-1]["text"])
        self.assertFalse(self.posted)

    async def test_compact_query_then_kick(self):
        await self.replay(1)

    async def test_compact_query_then_ban(self):
        await self.replay(2)

    async def test_compact_query_then_unban(self):
        await self.replay(3)

    async def test_compact_query_then_cycle(self):
        await self.replay(4)

    async def test_member_compact_query_is_denied_without_history_or_action(self):
        await self.event("查KAIKAI", role="member")
        self.assertIn("管理员", self.transcript[-1]["text"])
        self.assertFalse(self.history_queries)
        self.assertFalse(self.posted)
        self.model.generate.assert_not_awaited()

    async def test_chinese_compact_query_and_cancel(self):
        await self.event("查打灰 50")
        self.assertIn("选择玩家", self.transcript[-1]["text"])
        await self.event("2")
        self.assertEqual(self.history_queries[0]["steamId"], B)
        await self.event("4")
        await self.event("2")
        self.assertIn("取消", self.transcript[-1]["text"])
        self.assertFalse(self.posted)
        self.model.generate.assert_not_awaited()
