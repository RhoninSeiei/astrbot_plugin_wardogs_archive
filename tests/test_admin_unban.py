import time
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

import test_admin as fixture
from test_admin import WHO, A, B

from wardogs.unban import unban_request


class UnbanTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixture.AdminTests.asyncTearDown
    message = fixture.AdminTests.message
    handle = fixture.AdminTests.handle

    async def asyncSetUp(self):
        await fixture.AdminTests.asyncSetUp(self)
        self.client.admin_unban = AsyncMock(
            return_value={
                "operationId": "unban-fixture",
                "steamId": A,
                "source": "admin",
                "action": "unban",
                "status": "accepted",
                "unbanStatus": "pending",
            }
        )
        self.client.admin_cycle_add = AsyncMock()

    async def test_exact_online_id_uses_only_unban_endpoint(self):
        await self.handle("解封 " + A)
        self.client.admin_unban.assert_awaited_once()
        self.assertFalse(self.client.calls)
        self.client.admin_cycle_add.assert_not_awaited()

    async def test_fuzzy_then_ordinal_confirms_unban_not_kick(self):
        await self.handle("取消封禁 Example")
        self.client.admin_unban.assert_not_awaited()
        self.assertIn("尚未解封", self.replies[-1])
        await self.handle("1", mid="confirm")
        self.assertEqual(self.client.admin_unban.await_args.args[0]["steamId"], A)
        self.assertFalse(self.client.calls)

    async def test_confirmation_rechecks_role(self):
        await self.handle("解封 Example")
        await self.handle("1", mid="member-confirm", role="member")
        self.client.admin_unban.assert_not_awaited()

    async def test_scope_and_expiry(self):
        await self.handle("解封 Example")
        for who in [
            replace(WHO, sender="other"),
            replace(WHO, group="other"),
            replace(WHO, platform="other:fixture:app"),
        ]:
            await self.handle("1", mid="different", who=who)
        self.client.admin_unban.assert_not_awaited()
        self.store.db.execute(
            "UPDATE admin_unban_drafts SET expires=?", (time.time() - 1,)
        )
        self.store.db.commit()
        await self.handle("确认解封 1", mid="expired")
        self.client.admin_unban.assert_not_awaited()

    async def test_duplicate_and_unknown_result_never_replayed(self):
        self.client.admin_unban.side_effect = TimeoutError("synthetic lost reply")
        await self.handle("解封 " + A, mid="same")
        await self.handle("解封 " + A, mid="same")
        await self.handle("解封 " + A, mid="different")
        self.client.admin_unban.assert_awaited_once()
        self.assertIn("待核实", "".join(self.replies))

    async def test_cycle_and_kick_candidates_replaced_by_unban(self):
        await self.handle("踢出 Example", mid="kick")
        await self.handle("循环 Example", mid="cycle")
        await self.handle("解封 Example", mid="unban")
        await self.handle("1", mid="confirm")
        self.client.admin_unban.assert_awaited_once()
        self.assertFalse(self.client.calls)
        self.client.admin_cycle_add.assert_not_awaited()

    async def test_new_kick_replaces_unban(self):
        await self.handle("解封 Example")
        await self.handle("踢出 Example", mid="new-kick")
        await self.handle("1", mid="confirm")
        self.client.admin_unban.assert_not_awaited()
        self.assertEqual(len(self.client.calls), 1)

    async def test_new_cycle_replaces_unban(self):
        await self.handle("解封 Example")
        await self.handle("循环 Example", mid="new-cycle")
        await self.handle("1", mid="confirm")
        self.client.admin_unban.assert_not_awaited()
        self.client.admin_cycle_add.assert_awaited_once()

    async def test_offline_known_identity_requires_confirmation(self):
        self.client.data["players"] = []
        self.actions.history.lookup = AsyncMock(
            return_value=(
                [
                    {
                        "steamId": A,
                        "name": "FormerFixture",
                        "online": False,
                        "aliases": [],
                    }
                ],
                False,
            )
        )
        await self.handle("解封 " + A)
        self.client.admin_unban.assert_not_awaited()
        await self.handle("确认解封 1", mid="confirm")
        self.client.admin_unban.assert_awaited_once()
        self.assertFalse(self.client.calls)

    async def test_precandidate_message_cannot_confirm_later_list(self):
        early = replace(self.message("1", mid="early"), at=time.time() - 10)
        await self.handle("解封 Example")
        await self.actions.handle(early, "admin", self.replies.append)
        self.client.admin_unban.assert_not_awaited()

    async def test_explicit_confirmation_cannot_select_another_action(self):
        await self.handle("循环 Example")
        await self.handle("确认解封 1", mid="wrong")
        self.client.admin_unban.assert_not_awaited()
        self.client.admin_cycle_add.assert_not_awaited()
        self.assertFalse(self.client.calls)

    async def test_wrong_target_receipt_never_reports_unbanned(self):
        self.client.admin_unban.return_value = {
            "operationId": "unban-fixture",
            "steamId": B,
            "source": "admin",
            "action": "unban",
            "status": "completed",
            "unbanStatus": "released",
        }
        await self.handle("解封 " + A)
        self.assertFalse(any("已解封" in text for text in self.replies))
        self.assertIn("待核实", self.replies[-1])

    def test_aliases_and_negative_requests(self):
        for prefix in ["解封", "取消封禁", "解除封禁", "取消封号"]:
            self.assertEqual(unban_request(prefix + " Example")["query"], "Example")
        for text in ["不要解封 Example", "怎么解封 Example", "解封 Example？"]:
            self.assertIsNone(unban_request(text))

    async def test_native_ban_only_identity_is_searchable_without_history(self):
        self.client.data["players"] = []
        self.client.admin_unban_search = AsyncMock(
            return_value={
                "observedAt": time.time(),
                "complete": True,
                "truncated": False,
                "players": [
                    {
                        "steamId": A,
                        "name": "NativeOnlyFixture",
                        "aliases": [],
                        "online": None,
                        "nativeBanned": True,
                        "source": "moderation_history",
                    }
                ],
            }
        )
        await self.handle("解封 NativeOnlyFixture")
        self.assertIn(A, self.replies[-1])
        await self.handle("1", mid="native-only-confirm")
        self.client.admin_unban.assert_awaited_once()
        self.assertFalse(self.client.calls)

    async def test_complete_receipt_requires_both_native_and_local_success(self):
        self.client.admin_unban.return_value = {
            "operationId": "unban-fixture",
            "steamId": A,
            "source": "admin",
            "action": "unban",
            "status": "completed",
            "unbanStatus": "unbanned",
            "nativeUnbanStatus": "unbanned",
            "localReleaseStatus": "released",
        }
        await self.handle("解封 " + A)
        self.assertEqual(self.replies[0], "已受理")
        self.assertIn("已解封", self.replies[-1])
        self.client.admin_unban.assert_awaited_once()

    async def test_partial_receipt_explains_which_side_remains_unconfirmed(self):
        self.client.admin_unban.return_value = {
            "operationId": "unban-fixture",
            "steamId": A,
            "source": "admin",
            "action": "unban",
            "status": "partial",
            "unbanStatus": "partial",
            "nativeUnbanStatus": "unknown",
            "localReleaseStatus": "released",
        }
        await self.handle("解封 " + A)
        self.assertIn("本地重入限制已解除", self.replies[-1])
        self.assertIn("原生解封结果待核实", self.replies[-1])
        self.assertNotIn("已解封，", self.replies[-1])
        await self.handle("解封 " + A, mid="retry")
        self.client.admin_unban.assert_awaited_once()

    async def test_misleading_overall_success_does_not_override_failed_native_state(
        self,
    ):
        self.client.admin_unban.return_value = {
            "operationId": "unban-fixture",
            "steamId": A,
            "source": "admin",
            "action": "unban",
            "status": "completed",
            "unbanStatus": "unbanned",
            "nativeUnbanStatus": "failed",
            "localReleaseStatus": "released",
        }
        await self.handle("解封 " + A)
        self.assertIn("待核实", self.replies[-1])
        self.assertFalse(any("已解封，" in text for text in self.replies))

    async def test_pending_poll_notifies_success_once(self):
        await self.handle("解封 " + A)
        self.client.admin_operation = AsyncMock(
            return_value={
                "operationId": "unban-fixture",
                "steamId": A,
                "source": "admin",
                "action": "unban",
                "status": "completed",
                "unbanStatus": "unbanned",
                "nativeUnbanStatus": "already_unbanned",
                "localReleaseStatus": "not_restricted",
            }
        )
        await self.actions.poll()
        await self.actions.poll()
        self.assertEqual(sum("已解封，" in text for text in self.replies), 1)
        self.client.admin_unban.assert_awaited_once()

    async def test_empty_new_unban_request_revokes_old_kick_candidate(self):
        await self.handle("踢出 Example", mid="old")
        await self.handle("解封", mid="missing-target")
        await self.handle("1", mid="confirmation")
        self.assertFalse(self.client.calls)
        self.client.admin_unban.assert_not_awaited()

    async def test_new_kick_with_unban_word_in_name_revokes_unban_candidate(self):
        await self.handle("解封 Example")
        model = AsyncMock()
        model.generate.return_value = {
            "action": "none",
            "target": "",
            "confidence": 0.99,
        }
        await self.actions.handle(
            self.message("踢出解封小明", mid="new-kick"),
            "admin",
            self.replies.append,
            model=model,
        )
        await self.handle("1", mid="late-confirm")
        self.client.admin_unban.assert_not_awaited()
        self.assertFalse(self.client.calls)

    async def test_unban_candidate_and_result_do_not_imply_cycle_membership(self):
        self.client.data["players"] = []
        self.actions.history.lookup = AsyncMock(
            return_value=(
                [
                    {
                        "steamId": A,
                        "name": "FormerFixture",
                        "online": False,
                        "aliases": [],
                    }
                ],
                False,
            )
        )
        self.client.admin_unban.return_value = {
            "operationId": "unban-fixture",
            "steamId": A,
            "source": "admin",
            "action": "unban",
            "status": "completed",
            "unbanStatus": "unbanned",
            "nativeUnbanStatus": "unbanned",
            "localReleaseStatus": "released",
        }
        await self.handle("解封 FormerFixture")
        self.assertIn("尚未解封", self.replies[-1])
        self.assertIn("原生封禁和本地重入限制", self.replies[-1])
        self.assertNotIn("哈哈", self.replies[-1])
        self.client.admin_unban.assert_not_awaited()
        await self.handle("1", mid="unban-confirm")
        self.assertIn("已解封", self.replies[-1])
        self.assertTrue(all("哈哈" not in text for text in self.replies))
        self.client.admin_unban.assert_awaited_once()
        self.client.admin_cycle_add.assert_not_awaited()
        self.assertFalse(self.client.calls)

    async def test_busy_target_receipt_explains_unban_not_executed(self):
        self.client.admin_unban.return_value = {
            "operationId": "unban-busy-fixture",
            "steamId": A,
            "source": "admin",
            "action": "unban",
            "status": "superseded",
            "unbanStatus": "failed",
            "nativeUnbanStatus": "not_checked",
            "localReleaseStatus": "not_attempted",
            "error": {"code": "target_already_coordinated"},
        }
        await self.handle("解封 " + A)
        self.assertIn("本次未解封", self.replies[-1])
        self.assertIn("其他处置", self.replies[-1])
        self.assertNotIn("被其他操作替代", self.replies[-1])
        self.assertNotIn("已解封", self.replies[-1])
        self.client.admin_unban.assert_awaited_once()
        self.assertFalse(self.client.calls)

    async def test_unknown_kick_without_backend_id_still_blocks_unban(self):
        self.client.fail = True
        await self.handle("踢出 " + A, mid="uncertain-kick")
        before = self.store.db.execute(
            "SELECT id,status,operation_id FROM admin_kick_operations WHERE target=?",
            (A,),
        ).fetchone()
        self.assertEqual(before[1], "outcome_unknown")
        self.assertIsNone(before[2])
        await self.handle("解封 " + A, mid="new-unban")
        self.client.admin_unban.assert_not_awaited()
        self.assertIn("待核实", self.replies[-1])
        after = self.store.db.execute(
            "SELECT id,status,operation_id FROM admin_kick_operations WHERE target=?",
            (A,),
        ).fetchone()
        self.assertEqual(after, before)

    async def test_unproven_old_unknown_receipt_explains_manual_review(self):
        self.client.admin_unban.return_value = {
            "operationId": "unban-unproven-fixture",
            "steamId": A,
            "source": "admin",
            "action": "unban",
            "status": "superseded",
            "unbanStatus": "failed",
            "error": {"code": "unknown_punishment_not_reconcilable"},
        }
        await self.handle("解封 " + A)
        self.assertIn("旧处罚", self.replies[-1])
        self.assertIn("无法安全核实", self.replies[-1])
        self.assertIn("本次未解封", self.replies[-1])
        self.assertNotIn("已解封", self.replies[-1])
        self.client.admin_unban.assert_awaited_once()

    async def test_handoff_race_receipt_explains_changed_target(self):
        self.client.admin_unban.return_value = {
            "operationId": "unban-race-fixture",
            "steamId": A,
            "source": "admin",
            "action": "unban",
            "status": "rejected",
            "unbanStatus": "failed",
            "error": {"code": "unknown_handoff_revalidation_failed"},
            "unknownHandoff": {"status": "blocked"},
        }
        await self.handle("解封 " + A)
        self.assertIn("处置状态已变化", self.replies[-1])
        self.assertIn("本次未解封", self.replies[-1])
        self.assertNotIn("已解封", self.replies[-1])
        self.client.admin_unban.assert_awaited_once()

    async def test_handoff_marker_alone_never_means_unbanned(self):
        self.client.admin_unban.return_value = {
            "operationId": "unban-transferred-fixture",
            "steamId": A,
            "source": "admin",
            "action": "unban",
            "status": "accepted",
            "unbanStatus": "pending",
            "unknownHandoff": {"status": "transferred"},
        }
        await self.handle("解封 " + A)
        self.assertEqual(self.replies[-1], "已受理")
        self.assertFalse(any("已解封" in text for text in self.replies))
        self.assertIsNotNone(self.actions.unban.held(A))
        self.client.admin_unban.assert_awaited_once()
