import time
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

import test_admin as fixture
from test_admin import WHO, A, B

from wardogs.cycle import cycle_request


class CycleTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixture.AdminTests.asyncTearDown
    message = fixture.AdminTests.message
    handle = fixture.AdminTests.handle

    async def asyncSetUp(self):
        await fixture.AdminTests.asyncSetUp(self)
        self.client.admin_cycle_add = AsyncMock(
            return_value={
                "operationId": "cycle-fixture",
                "steamId": A,
                "source": "admin",
                "action": "cycle_add",
                "cycleStatus": "registered",
                "status": "completed",
                "registeredAt": time.time(),
                "alreadyEnabled": False,
                "onlineAtCheck": True,
            }
        )

    async def test_exact_online_id_registers_without_kicking(self):
        await self.handle("循环 " + A)
        self.client.admin_cycle_add.assert_awaited_once()
        self.assertEqual(self.client.admin_cycle_add.await_args.args[0]["steamId"], A)
        self.assertFalse(self.client.calls)
        self.assertTrue(any("循环" in text for text in self.replies))

    async def test_fuzzy_only_offers_then_ordinal_registers_exact_id(self):
        await self.handle("循环 Example")
        self.client.admin_cycle_add.assert_not_awaited()
        self.assertIn("1.", self.replies[-1])
        self.assertIn("尚未加入", self.replies[-1])
        await self.handle("1", mid="confirm-cycle")
        self.assertEqual(self.client.admin_cycle_add.await_args.args[0]["steamId"], A)
        self.assertFalse(self.client.calls)

    async def test_explicit_cycle_confirmation_and_exact_id(self):
        for confirm in ["确认循环 1", A]:
            await self.handle("循环 Example", mid=confirm + "-request")
            await self.handle(confirm, mid=confirm + "-confirmation")
        self.assertEqual(self.client.admin_cycle_add.await_count, 2)
        self.assertFalse(self.client.calls)

    async def test_member_cannot_offer_or_register(self):
        await self.handle("循环 " + A, role="member")
        self.client.admin_cycle_add.assert_not_awaited()
        self.assertIn("管理员", self.replies[-1])

    async def test_new_cycle_revokes_old_kick_and_replay_choices(self):
        await self.handle("踢出 Example")
        self.client.data["players"][1]["name"] = "DistinctFixture"
        await self.handle("循环 DistinctFixture", mid="new-cycle")
        await self.handle("1", mid="new-choice")
        self.assertEqual(self.client.admin_cycle_add.await_args.args[0]["steamId"], B)
        self.assertFalse(self.client.calls)

    async def test_new_kick_revokes_cycle_and_ordinal_only_kicks(self):
        await self.handle("循环 Examples")
        await self.handle("踢出 Example", mid="new-kick")
        await self.handle("1", mid="new-choice")
        self.client.admin_cycle_add.assert_not_awaited()
        self.assertEqual(len(self.client.calls), 1)

    async def test_lookup_failure_on_new_cycle_still_revokes_old_choices(self):
        await self.handle("踢出 Example")
        self.client.players = AsyncMock(side_effect=OSError("fixture unavailable"))
        self.client.data["players"][1]["name"] = "DistinctFixture"
        await self.handle("循环 DistinctFixture", mid="new-cycle")
        await self.handle("1", mid="after-failure")
        self.assertFalse(self.client.calls)
        self.client.admin_cycle_add.assert_not_awaited()

    async def test_sender_and_expiry_scopes(self):
        await self.handle("循环 Example")
        await self.handle("1", mid="wrong-sender", who=replace(WHO, sender="other"))
        self.client.admin_cycle_add.assert_not_awaited()
        self.store.db.execute(
            "UPDATE admin_cycle_drafts SET expires=?", (time.time() - 1,)
        )
        self.store.db.commit()
        await self.handle("确认循环 1", mid="expired")
        self.client.admin_cycle_add.assert_not_awaited()

    async def test_message_before_candidate_publication_cannot_select(self):
        before = self.message("1", mid="early")
        await self.handle("循环 Example")
        before = replace(before, at=time.time() - 10)
        await self.actions.handle(before, "admin", self.replies.append)
        self.client.admin_cycle_add.assert_not_awaited()

    async def test_duplicate_message_cannot_register_twice(self):
        await self.handle("循环 " + A, mid="duplicate")
        await self.handle("循环 " + A, mid="duplicate")
        self.client.admin_cycle_add.assert_awaited_once()

    async def test_unknown_registration_is_not_replayed_after_restart(self):
        from wardogs.admin import AdminKicks

        self.client.admin_cycle_add.side_effect = TimeoutError(
            "fixture outcome unknown"
        )
        await self.handle("循环 " + A, mid="unknown")
        self.actions.close()
        self.actions = AdminKicks(self.store, self.client)
        await self.handle("循环 " + A, mid="unknown")
        self.client.admin_cycle_add.assert_awaited_once()
        self.assertIn("待核实", "".join(self.replies))

    async def test_wrong_receipt_target_never_claims_enabled(self):
        self.client.admin_cycle_add.return_value = {
            "operationId": "cycle-fixture",
            "steamId": B,
            "source": "admin",
            "action": "cycle_add",
            "cycleStatus": "registered",
            "status": "completed",
            "registeredAt": time.time(),
            "alreadyEnabled": False,
            "onlineAtCheck": True,
        }
        await self.handle("循环 " + A)
        self.assertFalse(any("已加入" in text for text in self.replies))
        self.assertIn("待核实", self.replies[-1])

    async def test_pending_then_confirmed_poll_has_one_receipt(self):
        self.client.admin_cycle_add.return_value = {
            "operationId": "cycle-fixture",
            "steamId": A,
            "source": "admin",
            "action": "cycle_add",
            "cycleStatus": "queued",
            "status": "accepted",
        }
        await self.handle("循环 " + A)
        self.client.admin_operation = AsyncMock(
            return_value={
                "operationId": "cycle-fixture",
                "steamId": A,
                "source": "admin",
                "action": "cycle_add",
                "cycleStatus": "registered",
                "status": "completed",
                "registeredAt": time.time(),
                "alreadyEnabled": False,
                "onlineAtCheck": True,
            }
        )
        await self.actions.poll()
        await self.actions.poll()
        self.assertEqual(sum("已加入" in text for text in self.replies), 1)
        self.client.admin_cycle_add.assert_awaited_once()

    async def test_offline_known_candidate_requires_confirmation_and_never_bans(self):
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
        await self.handle("循环 " + A)
        self.client.admin_cycle_add.assert_not_awaited()
        self.assertIn("上线后", self.replies[-1])
        await self.handle("1", mid="offline-confirm")
        self.client.admin_cycle_add.assert_awaited_once()
        self.assertFalse(self.client.calls)
        self.assertFalse(
            self.store.db.execute(
                "SELECT id FROM admin_restriction_operations"
            ).fetchall()
        )

    def test_negative_discussion_and_multiple_targets_are_not_orders(self):
        for text in [
            "不要循环 Example",
            "取消循环 Example",
            "怎么循环 Example",
            "循环 Example？",
        ]:
            self.assertIsNone(cycle_request(text))
        self.assertTrue(cycle_request("循环 " + A + " " + B)["clarify"])

    async def test_confirmation_rechecks_current_role(self):
        await self.handle("循环 Example")
        await self.handle("1", mid="role-revoked", role="member")
        self.client.admin_cycle_add.assert_not_awaited()

    async def test_group_and_bot_scope_cannot_take_another_candidate(self):
        await self.handle("循环 Example")
        for who in [
            replace(WHO, group="other-group"),
            replace(WHO, platform="other:fixture:app"),
        ]:
            await self.handle("1", mid="other-scope", who=who)
        self.client.admin_cycle_add.assert_not_awaited()

    async def test_replay_replaces_cycle_even_when_query_fails(self):
        await self.handle("循环 Example")
        await self.handle("回溯", mid="new-replay")
        await self.handle("1", mid="after-replay")
        self.client.admin_cycle_add.assert_not_awaited()

    async def test_explicit_kick_confirmation_cannot_register_cycle(self):
        await self.handle("循环 Example")
        await self.handle("确认踢出 1", mid="wrong-action")
        self.client.admin_cycle_add.assert_not_awaited()
        self.assertFalse(self.client.calls)

    async def test_kick_receipt_is_never_treated_as_cycle_confirmation(self):
        self.client.admin_cycle_add.return_value = {
            "operationId": "kick-fixture",
            "steamId": A,
            "source": "admin",
            "action": "kick",
            "cycleStatus": "registered",
            "status": "completed",
            "registeredAt": time.time(),
            "alreadyEnabled": False,
            "onlineAtCheck": True,
        }
        await self.handle("循环 " + A)
        self.assertIn("待核实", self.replies[-1])
        self.assertFalse(any("已加入" in text for text in self.replies))

    async def test_cycle_registration_never_calls_model(self):
        model = AsyncMock()
        await self.actions.handle(
            self.message("循环 " + A), "admin", self.replies.append, model=model
        )
        model.generate.assert_not_awaited()
        self.client.admin_cycle_add.assert_awaited_once()

    async def test_pending_poll_rejects_wrong_operation_and_target(self):
        self.client.admin_cycle_add.return_value = {
            "operationId": "cycle-fixture",
            "steamId": A,
            "source": "admin",
            "action": "cycle_add",
            "cycleStatus": "queued",
            "status": "accepted",
        }
        await self.handle("循环 " + A)
        for operation, target in [("different", A), ("cycle-fixture", B)]:
            self.client.admin_operation = AsyncMock(
                return_value={
                    "operationId": operation,
                    "steamId": target,
                    "source": "admin",
                    "action": "cycle_add",
                    "cycleStatus": "registered",
                    "status": "completed",
                    "registeredAt": time.time(),
                    "alreadyEnabled": False,
                    "onlineAtCheck": True,
                }
            )
            await self.actions.poll()
        self.assertEqual(self.replies, ["已受理"])
        self.client.admin_cycle_add.assert_awaited_once()

    def test_nickname_may_contain_negative_or_test_words(self):
        for name in ["没我大嘴炒不熟的饭", "不要乱跑", "测试玩家"]:
            self.assertEqual(cycle_request("循环 " + name)["query"], name)
