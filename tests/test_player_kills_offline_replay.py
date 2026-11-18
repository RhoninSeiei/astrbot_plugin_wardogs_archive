"""An online history subject leaves before confirmation: official event replay."""

import json
import os
import time
import unittest
from pathlib import Path

import test_player_kills_chat_replay as online
from aiohttp import web
from test_admin import A, B


class OfflineHistoryReplayTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = online.GroupHistoryReplayTests.asyncTearDown
    event = online.GroupHistoryReplayTests.event
    start_query = online.GroupHistoryReplayTests.start_query

    async def asyncSetUp(self):
        self.offline = False
        self.roster_error = False
        self.restrict_unknown = False
        self.prior_auto = False
        await online.GroupHistoryReplayTests.asyncSetUp(self)

    async def backend(self, request):
        if request.path == "/v1/players":
            self.assertEqual(request.remote, "127.0.0.1")
            self.assertEqual(
                request.headers.get("Authorization"), "Bearer synthetic-query-token"
            )
            if self.roster_error:
                return web.json_response(
                    {"error": {"code": "player_snapshot_unavailable"}}, status=503
                )
            return web.json_response(
                {
                    "observedAt": time.time(),
                    "players": [
                        p for p in self.players if not self.offline or p["steamId"] == A
                    ],
                }
            )
        if request.path in (
            "/v1/moderation/player-search",
            "/v1/moderation/unban-search",
        ):
            self.assertEqual(
                request.headers.get("Authorization"), "Bearer synthetic-admin-token"
            )
            body = await request.json()
            self.assertEqual(body["actor"]["role"], "admin")
            return web.json_response(
                {
                    "observedAt": time.time(),
                    "complete": True,
                    "truncated": False,
                    "historyComplete": True,
                    "players": [
                        dict(
                            p,
                            online=not self.offline or p["steamId"] == A,
                            aliases=[],
                            source="history"
                            if self.offline and p["steamId"] == B
                            else "online",
                            lastSeen=time.time() - 60,
                        )
                        for p in self.players
                    ],
                }
            )
        if request.path == "/v1/moderation/admin-restrictions":
            self.assertEqual(
                request.headers.get("Authorization"), "Bearer synthetic-admin-token"
            )
            self.assertTrue(
                self.offline, "Must never restrict a player who returned online"
            )
            self.assertEqual(request.method, "POST")
            body = await request.json()
            self.assertEqual(body["steamId"], B)
            self.assertEqual(body["actor"]["role"], "admin")
            self.assertEqual(
                set(body), {"requestId", "steamId", "actor", "requestedAt"}
            )
            self.assertTrue(
                request.headers["Idempotency-Key"].startswith("wd-admin-restrict-")
            )
            self.posted.append((5, body))
            if self.restrict_unknown:
                return web.json_response(
                    {"error": {"code": "synthetic_unknown_submit"}}, status=503
                )
            op = "fixture-offline-restriction"
            self.operations[op] = 5
            return web.json_response(self.receipt(5, op, done=False), status=202)
        return await online.GroupHistoryReplayTests.backend(self, request)

    def receipt(self, action, op, *, done):
        if action == 5:
            result = {
                "operationId": op,
                "steamId": B,
                "source": "admin",
                "action": "restriction",
                "status": "completed" if done else "accepted",
                "restrictionStatus": "restricted" if done else "pending",
                "nativeBanStatus": "not_requested",
                "kickStatus": "not_needed",
                "createdAt": time.time(),
            }
            if self.prior_auto:
                result["priorAutomaticKick"] = {
                    "operationId": "synthetic-previous-auto",
                    "steamId": B,
                    "source": "auto_kd",
                    "kickStatus": "confirmed",
                    "confirmedAt": time.time() - 60,
                }
            return result
        result = online.GroupHistoryReplayTests.receipt(action, op, done=done)
        if self.offline:
            if action == 4:
                result["onlineAtCheck"] = False
            if action == 2:
                result.update(onlineBefore=False, removalStatus="not_needed")
        return result

    def save(self, scenario):
        self.model.generate.assert_not_awaited()
        out = os.environ.get("WARDOGS_OFFLINE_REPLAY_OUTPUT")
        if out:
            folder = Path(out)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / (scenario + ".json")).write_text(
                json.dumps(
                    {
                        "scenario": scenario,
                        "subject": B,
                        "queried_online_then_left": True,
                        "history_records": 50,
                        "actions": [a for a, _ in self.posted],
                        "messages": self.transcript,
                        "real_actions": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )

    async def offline_action_candidate(self, action):
        await self.start_query()
        self.offline = True
        self.transcript.append(
            {
                "speaker": "模拟状态变化",
                "text": "第二名玩家在查看50条记录后离开服务器。",
            }
        )
        await self.event(str(action))
        self.assertFalse(self.posted)
        await self.event("1", mid="first-confirm")
        self.assertFalse(
            self.posted, "Offline subject requires the existing extra confirmation"
        )
        self.assertIn("离线", self.transcript[-1]["text"])
        self.assertIn(B, self.transcript[-1]["text"])

    async def test_offline_kick_then_explicit_supplement_and_receipt(self):
        await self.offline_action_candidate(1)
        self.assertIn("永久重入限制", self.transcript[-1]["text"])
        self.assertIn("不代表当前踢出或原生封禁成功", self.transcript[-1]["text"])
        await self.event("1", mid="offline-confirm")
        self.assertEqual([a for a, _ in self.posted], [5])
        self.assertIn("已受理", self.transcript[-1]["text"])
        await self.plugin.admin_kicks.poll()
        self.assertIn("处理时已离线", self.transcript[-1]["text"])
        self.assertIn("永久重入限制", self.transcript[-1]["text"])
        self.assertIn("本次未执行踢出", self.transcript[-1]["text"])
        self.assertNotIn("已踢出", self.transcript[-1]["text"])
        await self.event("1", mid="offline-confirm")
        self.assertEqual([a for a, _ in self.posted], [5])
        self.save("kick-then-supplement")

    async def test_offline_unban_extra_confirmation_and_final_receipt(self):
        await self.offline_action_candidate(3)
        self.assertIn("解除游戏原生封禁和本地重入限制", self.transcript[-1]["text"])
        await self.event("1", mid="offline-unban-confirm")
        self.assertEqual([a for a, _ in self.posted], [3])
        await self.plugin.admin_kicks.poll()
        self.assertIn("已解封", self.transcript[-1]["text"])
        self.assertIn("原生封禁和本地重入限制均已解除", self.transcript[-1]["text"])
        self.assertNotIn("哈哈名单", self.transcript[-1]["text"])
        await self.event("1", mid="offline-unban-confirm")
        self.assertEqual([a for a, _ in self.posted], [3])
        self.save("offline-unban")

    async def test_offline_haha_extra_confirmation_and_online_policy_notice(self):
        await self.offline_action_candidate(4)
        self.assertIn("上线后生效", self.transcript[-1]["text"])
        await self.event("1", mid="offline-haha-confirm")
        self.assertEqual([a for a, _ in self.posted], [4])
        await self.plugin.admin_kicks.poll()
        self.assertIn("已加入循环惩罚名单", self.transcript[-1]["text"])
        self.assertIn("在线时按服务器既有规则生效", self.transcript[-1]["text"])
        self.assertNotIn("已踢出", self.transcript[-1]["text"])
        await self.event("1", mid="offline-haha-confirm")
        self.assertEqual([a for a, _ in self.posted], [4])
        self.save("offline-haha")

    async def test_offline_kick_alone_never_automatically_supplements(self):
        await self.offline_action_candidate(1)
        await self.plugin.admin_kicks.poll()
        self.assertFalse(self.posted)
        self.save("kick-offline-awaiting-supplement")

    async def test_return_online_before_supplement_rejects_changed_mode(self):
        await self.offline_action_candidate(1)
        self.offline = False
        await self.event("1", mid="returned-confirm")
        self.assertFalse(self.posted)
        self.assertIn("状态或历史身份发生变化", self.transcript[-1]["text"])
        self.assertIn("未补封", self.transcript[-1]["text"])
        self.save("returned-online-stop")

    async def test_player_snapshot_failure_does_not_guess_offline(self):
        await self.start_query()
        self.offline = True
        self.roster_error = True
        await self.event("1")
        await self.event("1")
        self.assertFalse(self.posted)
        self.assertIn("在线名单不可核验", self.transcript[-1]["text"])
        self.save("roster-error-stop")

    async def test_supplement_unknown_blocks_duplicate_submission(self):
        await self.offline_action_candidate(1)
        self.restrict_unknown = True
        await self.event("1", mid="unknown-confirm")
        self.assertEqual([a for a, _ in self.posted], [5])
        self.assertIn("结果待核实", self.transcript[-1]["text"])
        self.assertTrue(self.plugin.admin_kicks.pending_target(B))
        await self.event("1", mid="unknown-confirm")
        await self.plugin.admin_kicks.poll()
        self.assertEqual([a for a, _ in self.posted], [5])
        self.save("supplement-unknown-stop")
