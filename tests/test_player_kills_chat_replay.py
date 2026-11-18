"""Official group-shaped events through real handlers and loopback HTTP only."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import astrbot.api.message_components as Comp
import test_admin_auth as auth
import test_plugin as pt
from aiohttp import web
from test_admin import A, B
from test_player_kills import response

from wardogs.admin import AdminKicks
from wardogs.domain import Store
from wardogs.history import LocalHistory
from wardogs.io import ServiceClient
from wardogs.queries import PublicQueries

ROUTES = {1: "admin-kicks", 2: "admin-bans", 3: "admin-unbans", 4: "admin-cycle-adds"}
FINALS = {1: "已踢出", 2: "已封禁", 3: "已解封", 4: "已加入循环惩罚名单"}


class GroupHistoryReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.posted, self.history_queries, self.transcript = [], [], []
        self.operations = {}
        self.number = 0
        self.players = [
            {"steamId": A, "name": "打灰大哥哥"},
            {"steamId": B, "name": "打灰大哥哥二号"},
        ]
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.backend)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        (self.root / "query").write_text("synthetic-query-token")
        (self.root / "admin").write_text("synthetic-admin-token")
        self.client = ServiceClient(
            "http://127.0.0.1:" + str(self.site._server.sockets[0].getsockname()[1]),
            self.root / "query",
            admin_key_file=self.root / "admin",
        )
        self.store = Store(self.root / "db")
        _, context, config = auth.AdminAuthTests().setup_official()
        config.update(
            public_queries_enabled=True,
            official_query_scopes=list(config["official_admin_scopes"]),
            official_only=True,
        )
        self.plugin = pt.WardogsPlugin(context, config)
        self.plugin.ready = True
        self.plugin.admin_kicks = AdminKicks(self.store, self.client)
        self.plugin.public_queries = PublicQueries(
            self.client, LocalHistory(self.store)
        )
        self.model = SimpleNamespace(
            generate=AsyncMock(
                side_effect=AssertionError("LLM_FORBIDDEN_IN_NUMERIC_REPLAY")
            )
        )
        self.plugin.assessor = lambda _: self.model

    async def asyncTearDown(self):
        self.plugin.admin_kicks.close()
        self.store.close()
        await self.client.close()
        await self.runner.cleanup()
        self.tmp.cleanup()

    async def backend(self, request):
        # Even inside the network-none candidate, accept loopback substitute only.
        self.assertEqual(request.remote, "127.0.0.1")
        if request.path == "/v1/players":
            self.assertEqual(
                request.headers.get("Authorization"), "Bearer synthetic-query-token"
            )
            return web.json_response(
                {"observedAt": time.time(), "players": self.players}
            )
        self.assertEqual(
            request.headers.get("Authorization"), "Bearer synthetic-admin-token"
        )
        if request.path in (
            "/v1/moderation/player-search",
            "/v1/moderation/unban-search",
        ):
            body = await request.json()
            self.assertEqual(body["actor"]["role"], "admin")
            return web.json_response(
                {
                    "observedAt": time.time(),
                    "truncated": False,
                    "historyComplete": True,
                    "complete": True,
                    "players": [
                        dict(
                            p,
                            aliases=[],
                            online=True,
                            source="online",
                            lastSeen=time.time(),
                        )
                        for p in self.players
                    ],
                }
            )
        if request.path == "/v1/moderation/player-sanction-summary":
            from test_sanction_summary import summary

            body = await request.json()
            return web.json_response(summary(body["steamId"], bans=0, unbans=0))
        if request.path == "/v1/moderation/player-kills":
            body = await request.json()
            self.assertEqual(body["steamId"], B)
            self.assertEqual(body["limit"], 50)
            self.assertNotIn("Idempotency-Key", request.headers)
            self.history_queries.append(body)
            data = response(50, target=B)
            for index, event in enumerate(data["events"]):
                event["killer"]["name"] = self.players[1]["name"]
                event["relationship"] = ("friendly", "enemy", "self", "unknown")[
                    index % 4
                ]
                event["victim"] = {
                    "steamId": B if index % 4 == 2 else A,
                    "name": "模拟受害者" + str(index + 1),
                }
            return web.json_response(data)
        action = next(
            (
                a
                for a, path in ROUTES.items()
                if request.path == "/v1/moderation/" + path
            ),
            None,
        )
        if action is not None:
            self.assertEqual(request.method, "POST")
            body = await request.json()
            self.assertEqual(body["steamId"], B)
            self.assertEqual(body["actor"]["role"], "admin")
            self.assertEqual(
                set(body), {"requestId", "steamId", "actor", "requestedAt"}
            )
            self.assertTrue(request.headers.get("Idempotency-Key"))
            self.posted.append((action, body))
            op = "fixture-replay-" + str(action)
            self.operations[op] = action
            base = self.receipt(action, op, done=False)
            return web.json_response(base, status=202)
        prefix = "/v1/moderation/operations/"
        if request.path.startswith(prefix):
            op = request.path.removeprefix(prefix)
            return web.json_response(self.receipt(self.operations[op], op, done=True))
        self.fail("Unexpected substitute endpoint:" + request.path)

    @staticmethod
    def receipt(action, op, *, done):
        data = {
            "operationId": op,
            "steamId": B,
            "source": "admin",
            "status": "completed" if done else "accepted",
        }
        if action == 1:
            return dict(
                data, action="kick", kickStatus="confirmed" if done else "queued"
            )
        if action == 2:
            return dict(
                data,
                action="ban",
                banStatus="banned" if done else "pending",
                nativeBanStatus="banned" if done else "not_checked",
                localRestrictionStatus="restricted" if done else "not_attempted",
                nativeVerifiedAt=time.time() if done else None,
                localVerifiedAt=time.time() if done else None,
                onlineBefore=True,
                removalStatus="confirmed" if done else "not_checked",
            )
        if action == 3:
            return dict(
                data,
                action="unban",
                unbanStatus="unbanned" if done else "pending",
                nativeUnbanStatus="unbanned" if done else "not_requested",
                localReleaseStatus="released" if done else "not_requested",
            )
        return dict(
            data,
            action="cycle_add",
            cycleStatus="registered" if done else "pending",
            registeredAt=time.time() if done else None,
            alreadyEnabled=False,
            onlineAtCheck=True,
        )

    async def event(self, text, *, role="admin", mid=None):
        self.number += 1
        event, _, _ = auth.AdminAuthTests().setup_official(role)
        event.message_obj.message_id = mid or str(self.number)
        event.message_obj.message = [Comp.At(qq="qq_official"), Comp.Plain(text)]
        self.transcript.append(
            {
                "speaker": "管理员甲" if role == "admin" else "普通群友",
                "text": "@管理员1号 " + text,
            }
        )

        async def capture(result):
            self.transcript.append(
                {
                    "speaker": "管理员1号",
                    "text": "\n".join(getattr(c, "text", "") for c in result.chain),
                }
            )

        event.send = capture
        await self.plugin.on_public_query(event)
        self.assertFalse(
            event.is_stopped(), "Public handler intercepted history navigation"
        )
        await self.plugin.on_admin_order(event)
        return event

    async def start_query(self):
        await self.event(getattr(self, "query_command", "击杀记录 打灰 50"))
        self.assertIn("选择玩家", self.transcript[-1]["text"])
        self.assertIn("【2】打灰大哥哥二号 " + B, self.transcript[-1]["text"])
        self.assertFalse(self.history_queries)
        self.assertFalse(self.posted)
        await self.event("2")
        self.assertEqual(len(self.history_queries), 1)
        text = "\n".join(
            m["text"] for m in self.transcript if m["speaker"] == "管理员1号"
        )
        for value in [
            "记录50",
            "北京时间",
            "友军",
            "敌军",
            "自杀",
            "敌友未知",
            "【1】踢出",
            "【4】加入哈哈名单",
        ]:
            self.assertIn(value, text)
        self.assertFalse(self.posted)

    async def replay(self, action):
        await self.start_query()
        await self.event(str(action))
        self.assertIn("【1】确认", self.transcript[-1]["text"])
        self.assertIn(B, self.transcript[-1]["text"])
        self.assertFalse(self.posted)
        await self.event("1", mid="final-confirmation")
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(self.posted[0][0], action)
        self.assertEqual(self.transcript[-1]["text"], "已受理")
        await self.plugin.admin_kicks.poll()
        self.assertIn(FINALS[action], self.transcript[-1]["text"])
        self.assertIn(B, self.transcript[-1]["text"])
        final = self.transcript[-1]["text"]
        await self.event("1", mid="final-confirmation")
        await self.plugin.admin_kicks.poll()
        self.assertEqual(len(self.posted), 1)
        self.model.generate.assert_not_awaited()
        out = os.environ.get("WARDOGS_REPLAY_OUTPUT")
        if out:
            folder = Path(out)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / (str(action) + ".json")).write_text(
                json.dumps(
                    {
                        "action": action,
                        "target": B,
                        "history_records": 50,
                        "history_queries": len(self.history_queries),
                        "substitute_action_requests": len(self.posted),
                        "route": "/v1/moderation/" + ROUTES[action],
                        "final": final,
                        "messages": self.transcript,
                        "real_punishments": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )

    async def test_group_fuzzy_50_then_kick(self):
        await self.replay(1)

    async def test_group_fuzzy_50_then_permanent_ban(self):
        await self.replay(2)

    async def test_group_fuzzy_50_then_unban(self):
        await self.replay(3)

    async def test_group_fuzzy_50_then_haha_policy(self):
        await self.replay(4)

    async def test_member_cannot_take_over_admin_numeric_menu(self):
        await self.start_query()
        await self.event("2", role="member")
        self.assertIn("管理员", self.transcript[-1]["text"])
        self.assertFalse(self.posted)
        self.model.generate.assert_not_awaited()

    async def test_cancel_final_action_has_no_substitute_punishment(self):
        await self.start_query()
        await self.event("4")
        await self.event("2")
        self.assertIn("取消", self.transcript[-1]["text"])
        self.assertFalse(self.posted)
        self.model.generate.assert_not_awaited()
