"""Real official-group handlers with synthetic HTTP unban receipts only."""

import unittest

import test_player_kills_chat_replay as group_replay
from test_admin import B


class UnknownUnbanGroupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = group_replay.GroupHistoryReplayTests.asyncSetUp
    asyncTearDown = group_replay.GroupHistoryReplayTests.asyncTearDown
    backend = group_replay.GroupHistoryReplayTests.backend
    event = group_replay.GroupHistoryReplayTests.event

    def receipt(self, action, operation, *, done):
        result = group_replay.GroupHistoryReplayTests.receipt(
            action, operation, done=done
        )
        if action == 3 and done and getattr(self, "blocked", False):
            result.update(
                status=getattr(self, "blocked_status", "superseded"),
                unbanStatus="failed",
                nativeUnbanStatus="not_checked",
                localReleaseStatus="not_attempted",
                error={
                    "code": getattr(self, "blocked_code", "target_already_coordinated")
                },
            )
        return result

    async def choose_target(self):
        await self.event("解封 打灰")
        self.assertIn("解封候选", self.transcript[-1]["text"])
        self.assertIn(B, self.transcript[-1]["text"])
        self.assertFalse(self.posted)
        await self.event("2", mid="unban-choice")
        self.assertEqual(len(self.posted), 1)
        action, body = self.posted[0]
        self.assertEqual(action, 3)
        self.assertEqual(body["steamId"], B)
        self.assertEqual(self.transcript[-1]["text"], "已受理")

    async def test_success_receipt_after_fuzzy_selection_remains_same_id(self):
        await self.choose_target()
        await self.plugin.admin_kicks.poll()
        self.assertIn("已解封", self.transcript[-1]["text"])
        self.assertIn(B, self.transcript[-1]["text"])
        await self.event("2", mid="unban-choice")
        await self.plugin.admin_kicks.poll()
        self.assertEqual(len(self.posted), 1)
        self.model.generate.assert_not_awaited()

    async def test_unsafe_owner_receipt_explains_no_unban_and_no_replay(self):
        self.blocked = True
        await self.choose_target()
        await self.plugin.admin_kicks.poll()
        message = self.transcript[-1]["text"]
        self.assertIn("本次未解封", message)
        self.assertIn("其他处置", message)
        self.assertIn(B, message)
        self.assertNotIn("已解封", message)
        await self.event("2", mid="unban-choice")
        await self.plugin.admin_kicks.poll()
        self.assertEqual(len(self.posted), 1)
        self.model.generate.assert_not_awaited()

    async def test_member_cannot_start_unban_request(self):
        await self.event("解封 打灰", role="member")
        self.assertFalse(self.posted)
        self.assertIn("管理员", self.transcript[-1]["text"])
        self.model.generate.assert_not_awaited()

    async def test_member_cannot_confirm_admin_unban_candidate(self):
        await self.event("解封 打灰")
        await self.event("2", role="member")
        self.assertFalse(self.posted)
        self.assertIn("管理员", self.transcript[-1]["text"])
        self.model.generate.assert_not_awaited()

    async def test_unproven_old_unknown_has_clear_group_reply(self):
        self.blocked = True
        self.blocked_code = "unknown_punishment_not_reconcilable"
        await self.choose_target()
        await self.plugin.admin_kicks.poll()
        message = self.transcript[-1]["text"]
        self.assertIn("旧处罚结果仍无法安全核实", message)
        self.assertIn("本次未解封", message)
        self.assertNotIn("已解封", message)
        self.assertEqual(len(self.posted), 1)

    async def test_changed_target_during_handoff_never_reports_success(self):
        self.blocked = True
        self.blocked_status = "rejected"
        self.blocked_code = "unknown_handoff_revalidation_failed"
        await self.choose_target()
        await self.plugin.admin_kicks.poll()
        message = self.transcript[-1]["text"]
        self.assertIn("处置状态已变化", message)
        self.assertIn("本次未解封", message)
        self.assertNotIn("已解封", message)
        self.assertEqual(len(self.posted), 1)
