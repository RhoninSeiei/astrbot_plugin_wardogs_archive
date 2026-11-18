"""A former-name query retains one SteamID after the subject disconnects."""

import time
import unittest

import test_nickname_action_replay as names
import test_player_kills_offline_replay as offline
from aiohttp import web
from test_admin import A, B


class FormerNameOfflineTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = names.NicknameActionReplayTests.asyncTearDown
    event = names.NicknameActionReplayTests.event
    receipt = offline.OfflineHistoryReplayTests.receipt
    offline_action_candidate = (
        offline.OfflineHistoryReplayTests.offline_action_candidate
    )
    save = offline.OfflineHistoryReplayTests.save

    async def asyncSetUp(self):
        self.offline = False
        self.roster_error = False
        self.restrict_unknown = False
        self.prior_auto = False
        await names.NicknameActionReplayTests.asyncSetUp(self)

    async def backend(self, request):
        if request.path == "/v1/moderation/player-sanction-summary":
            from test_sanction_summary import summary

            self.assertEqual(request.remote, "127.0.0.1")
            self.assertEqual(
                request.headers.get("Authorization"), "Bearer synthetic-admin-token"
            )
            body = await request.json()
            self.assertEqual(body["steamId"], B)
            self.assertNotIn("Idempotency-Key", request.headers)
            return web.json_response(summary(B))
        if request.path in (
            "/v1/moderation/player-search",
            "/v1/moderation/unban-search",
        ):
            self.assertEqual(request.remote, "127.0.0.1")
            self.assertEqual(
                request.headers.get("Authorization"), "Bearer synthetic-admin-token"
            )
            return web.json_response(
                {
                    "observedAt": time.time(),
                    "complete": True,
                    "historyComplete": True,
                    "truncated": False,
                    "players": [
                        dict(
                            p,
                            aliases=[names.FORMER if p["steamId"] == B else "旧狼一号"],
                            online=not self.offline or p["steamId"] == A,
                            source="history"
                            if self.offline and p["steamId"] == B
                            else "online",
                            lastSeen=time.time() - 30,
                        )
                        for p in self.players
                    ],
                }
            )
        if request.path == "/v1/moderation/player-kills":
            return await names.NicknameActionReplayTests.backend(self, request)
        return await offline.OfflineHistoryReplayTests.backend(self, request)

    async def start_query(self):
        await self.event("查 " + names.FORMER + " 50")
        self.assertIn("【1】" + names.CURRENT + " " + B, self.transcript[-1]["text"])
        self.assertFalse(self.history_queries)
        self.assertFalse(self.posted)
        await self.event("1")
        self.assertEqual(self.history_queries[0]["steamId"], B)
        text = "\n".join(
            m["text"] for m in self.transcript if m["speaker"] == "管理员1号"
        )
        self.assertIn("封禁至少2次", text)
        self.assertIn("解封至少2次", text)
        self.assertIn("管理员提醒", text)
        self.assertIn("记录50", text)
        self.assertIn("曾用名：" + names.FORMER, text)
        self.assertEqual(
            sum(
                "同一SteamID的历史昵称" in line
                for line in text.splitlines()
                if line.startswith("│ [")
            ),
            25,
        )
        self.assertIn("操作对象：" + names.CURRENT, self.transcript[-1]["text"])
        self.assertIn(B, self.transcript[-1]["text"])
        self.assertFalse(self.posted)

    test_old_name_offline_kick_then_supplement = offline.OfflineHistoryReplayTests.test_offline_kick_then_explicit_supplement_and_receipt
    test_old_name_offline_kick_alone = offline.OfflineHistoryReplayTests.test_offline_kick_alone_never_automatically_supplements
    test_old_name_offline_unban = offline.OfflineHistoryReplayTests.test_offline_unban_extra_confirmation_and_final_receipt
    test_old_name_offline_haha = offline.OfflineHistoryReplayTests.test_offline_haha_extra_confirmation_and_online_policy_notice
    test_old_name_return_online = offline.OfflineHistoryReplayTests.test_return_online_before_supplement_rejects_changed_mode
    test_old_name_roster_failure = offline.OfflineHistoryReplayTests.test_player_snapshot_failure_does_not_guess_offline
    test_old_name_unknown_supplement = offline.OfflineHistoryReplayTests.test_supplement_unknown_blocks_duplicate_submission
