"""Current and former nickname requests bind all four actions to one SteamID."""

import json
import os
import time
import unittest
from pathlib import Path

import test_player_kills_chat_replay as base
from aiohttp import web
from test_admin import A, B
from test_player_kills import response

CURRENT = "灰狼二号"
FORMER = "旧狼二号"


class NicknameActionReplayTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = base.GroupHistoryReplayTests.asyncTearDown
    event = base.GroupHistoryReplayTests.event
    receipt = staticmethod(base.GroupHistoryReplayTests.receipt)

    async def asyncSetUp(self):
        self.shared_alias = False
        await base.GroupHistoryReplayTests.asyncSetUp(self)
        self.players[0]["name"] = "灰狼一号"
        self.players[1]["name"] = CURRENT

    async def backend(self, request):
        if request.path in (
            "/v1/moderation/player-search",
            "/v1/moderation/unban-search",
        ):
            self.assertEqual(request.remote, "127.0.0.1")
            self.assertEqual(
                request.headers.get("Authorization"), "Bearer synthetic-admin-token"
            )
            body = await request.json()
            self.assertEqual(body["actor"]["role"], "admin")
            return web.json_response(
                {
                    "observedAt": time.time(),
                    "complete": True,
                    "historyComplete": True,
                    "truncated": False,
                    "players": [
                        dict(
                            p,
                            aliases=[
                                FORMER
                                if self.shared_alias or p["steamId"] == B
                                else "旧狼一号"
                            ],
                            online=True,
                            source="online",
                            lastSeen=time.time(),
                        )
                        for p in self.players
                    ],
                }
            )
        if request.path == "/v1/moderation/player-kills":
            self.assertEqual(request.remote, "127.0.0.1")
            self.assertEqual(
                request.headers.get("Authorization"), "Bearer synthetic-admin-token"
            )
            body = await request.json()
            self.assertEqual(body["steamId"], B)
            self.assertEqual(body["limit"], 50)
            self.history_queries.append(body)
            data = response(50, target=B)
            for i, event in enumerate(data["events"]):
                event["killer"]["name"] = CURRENT if i % 2 == 0 else FORMER
                event["relationship"] = ("enemy", "friendly", "self", "unknown")[i % 4]
                event["victim"] = {
                    "steamId": B if i % 4 == 2 else A,
                    "name": CURRENT if i % 4 == 2 else "模拟受害者",
                }
            return web.json_response(data)
        return await base.GroupHistoryReplayTests.backend(self, request)

    async def replay_nickname(self, nickname, action):
        await self.event("查 " + nickname + " 50")
        self.assertIn("【1】" + CURRENT + " " + B, self.transcript[-1]["text"])
        self.assertIn("【2】灰狼一号 " + A, self.transcript[-1]["text"])
        self.assertFalse(self.history_queries)
        self.assertFalse(self.posted)
        await self.event("1")
        self.assertEqual(len(self.history_queries), 1)
        self.assertEqual(self.history_queries[0]["steamId"], B)
        record_text = "\n".join(
            m["text"] for m in self.transcript if m["speaker"] == "管理员1号"
        )
        self.assertIn("记录50", record_text)
        self.assertIn("当前或最近昵称：" + CURRENT, record_text)
        self.assertIn("曾用名：" + FORMER, record_text)
        historical = [
            line
            for line in record_text.splitlines()
            if line.startswith("│ [") and "同一SteamID的历史昵称" in line
        ]
        self.assertEqual(len(historical), 25)
        self.assertTrue(all(FORMER in line for line in historical))
        menu = self.transcript[-1]["text"]
        self.assertIn("操作对象：" + CURRENT, menu)
        self.assertIn(B, menu)
        self.assertNotIn(A, menu)
        await self.event(str(action))
        self.assertIn("【1】确认", self.transcript[-1]["text"])
        self.assertIn(CURRENT, self.transcript[-1]["text"])
        self.assertIn(B, self.transcript[-1]["text"])
        self.assertFalse(self.posted)
        await self.event("1", mid="nickname-final")
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(self.posted[0][0], action)
        self.assertEqual(self.posted[0][1]["steamId"], B)
        self.assertEqual(self.transcript[-1]["text"], "已受理")
        await self.plugin.admin_kicks.poll()
        self.assertIn(base.FINALS[action], self.transcript[-1]["text"])
        self.assertIn(B, self.transcript[-1]["text"])
        final = self.transcript[-1]["text"]
        await self.event("1", mid="nickname-final")
        await self.plugin.admin_kicks.poll()
        self.assertEqual(len(self.posted), 1)
        self.model.generate.assert_not_awaited()
        out = os.environ.get("WARDOGS_ALIAS_REPLAY_OUTPUT")
        if out:
            folder = Path(out)
            folder.mkdir(parents=True, exist_ok=True)
            key = ("current" if nickname == CURRENT else "former") + "-" + str(action)
            (folder / (key + ".json")).write_text(
                json.dumps(
                    {
                        "query_kind": "current" if nickname == CURRENT else "former",
                        "action": action,
                        "target": B,
                        "records": 50,
                        "historical_markers": len(historical),
                        "route": "/v1/moderation/" + base.ROUTES[action],
                        "submitted": len(self.posted),
                        "final": final,
                        "messages": self.transcript,
                        "real_actions": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )

    async def test_current_name_then_kick(self):
        await self.replay_nickname(CURRENT, 1)

    async def test_current_name_then_ban(self):
        await self.replay_nickname(CURRENT, 2)

    async def test_current_name_then_unban(self):
        await self.replay_nickname(CURRENT, 3)

    async def test_current_name_then_haha(self):
        await self.replay_nickname(CURRENT, 4)

    async def test_former_name_then_kick(self):
        await self.replay_nickname(FORMER, 1)

    async def test_former_name_then_ban(self):
        await self.replay_nickname(FORMER, 2)

    async def test_former_name_then_unban(self):
        await self.replay_nickname(FORMER, 3)

    async def test_former_name_then_haha(self):
        await self.replay_nickname(FORMER, 4)

    async def test_shared_former_name_requires_selection_and_never_merges_accounts(
        self,
    ):
        self.shared_alias = True
        await self.event("查 " + FORMER + " 50")
        self.assertIn(A, self.transcript[-1]["text"])
        self.assertIn(B, self.transcript[-1]["text"])
        self.assertFalse(self.history_queries)
        self.assertFalse(self.posted)
        await self.event("2")
        self.assertEqual(self.history_queries[0]["steamId"], B)
        self.assertIn("操作对象：" + CURRENT, self.transcript[-1]["text"])
        self.assertIn(B, self.transcript[-1]["text"])
        self.assertFalse(self.posted)
        self.model.generate.assert_not_awaited()
