import time
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

import test_kill_replay as fixture
from test_admin import WHO


class RestrictionBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.ReplayTests.asyncSetUp(self)
        self.targets = [e["killer"]["steamId"] for e in self.result["events"][:2]]
        self.client.data["players"] = [
            p for p in self.client.data["players"] if p["steamId"] not in self.targets
        ]
        self.banned = set()

        async def search(body):
            target = body["steamIds"][0]
            now = time.time()
            return {
                "observedAt": now,
                "complete": True,
                "players": [
                    {
                        "steamId": target,
                        "online": any(
                            p["steamId"] == target for p in self.client.data["players"]
                        ),
                        "onlineObservedAt": now,
                        "localRestricted": target in self.banned,
                        "localRestrictionStatus": "active"
                        if target in self.banned
                        else None,
                        "localObservedAt": now,
                        "nativeBanned": target in self.banned,
                        "nativeObservedAt": now,
                        "nativeSyncState": None,
                        "currentAutomaticRestriction": False,
                        "sourceComplete": True,
                        "localSource": {
                            "category": "manual" if target in self.banned else "none",
                            "code": "admin" if target in self.banned else None,
                        },
                        "nativeSource": {
                            "category": "manual" if target in self.banned else "none",
                            "code": "manual" if target in self.banned else None,
                        },
                    }
                ],
            }

        self.client.current_restrictions = AsyncMock(side_effect=search)
        self.client.admin_restrict = AsyncMock(
            side_effect=lambda body, key: {
                "operationId": "restriction-" + body["steamId"],
                "restrictionStatus": "pending",
            }
        )
        self.client.admin_operation = AsyncMock(
            side_effect=lambda op: {
                "operationId": op,
                "steamId": op.removeprefix("restriction-"),
                "restrictionStatus": "restricted",
            }
        )

    asyncTearDown = fixture.ReplayTests.asyncTearDown
    handle = fixture.ReplayTests.handle

    async def propose(self):
        await self.handle()
        await self.handle("1 2", mid="select")

    async def test_offline_players_get_one_explicit_bulk_confirmation(self):
        await self.propose()
        self.client.admin_restrict.assert_not_awaited()
        self.assertIn("确认补封全部", "".join(self.replies))
        await self.handle("确认补封全部", mid="confirm")
        self.assertEqual(self.client.admin_restrict.await_count, 2)
        self.assertEqual(
            {c.args[0]["steamId"] for c in self.client.admin_restrict.call_args_list},
            set(self.targets),
        )
        self.assertFalse(self.client.calls)
        await self.actions.poll()
        self.assertIn("批量补封结果", "".join(self.replies))
        self.assertIn("永久重入限制", "".join(self.replies))

    async def test_already_banned_players_not_offered_or_restricted(self):
        self.banned.update(self.targets)
        await self.propose()
        self.assertIn("已封禁", "".join(self.replies))
        self.assertNotIn("确认补封全部", "".join(self.replies))
        await self.handle("确认补封全部", mid="confirm")
        self.client.admin_restrict.assert_not_awaited()

    async def test_partial_selection_uses_original_ordinals(self):
        await self.propose()
        await self.handle("补封 2", mid="confirm")
        self.client.admin_restrict.assert_awaited_once()
        self.assertEqual(
            self.client.admin_restrict.call_args.args[0]["steamId"], self.targets[1]
        )

    async def test_whole_selection_validated_before_any_post(self):
        await self.propose()
        await self.handle("补封 1 999", mid="bad")
        self.client.admin_restrict.assert_not_awaited()

    async def test_member_group_bot_and_other_admin_cannot_adopt_proposal(self):
        await self.propose()
        for i, who in enumerate(
            [
                replace(WHO, sender="other"),
                replace(WHO, group="other"),
                replace(WHO, platform="other:bot"),
            ]
        ):
            await self.handle("确认补封全部", mid="other" + str(i), who=who)
        await self.handle("确认补封全部", mid="member", role="member")
        self.client.admin_restrict.assert_not_awaited()

    async def test_expired_or_replaced_list_not_confirmable(self):
        await self.propose()
        self.store.db.execute("UPDATE admin_batch_restriction_drafts SET expires=1")
        self.store.db.commit()
        await self.handle("确认补封全部", mid="expired")
        self.client.admin_restrict.assert_not_awaited()
        await self.handle("回溯", mid="new-replay")
        await self.handle("确认补封全部", mid="old")
        self.client.admin_restrict.assert_not_awaited()

    async def test_newly_online_targets_are_not_kicked_or_restricted(self):
        await self.propose()
        self.client.data["players"] += [
            dict(self.result["events"][i]["killer"]) for i in [0, 1]
        ]
        await self.handle("确认补封全部", mid="confirm")
        self.client.admin_restrict.assert_not_awaited()
        self.assertFalse(self.client.calls)
        self.assertIn("已上线", "".join(self.replies))

    async def test_banned_during_confirmation_is_skipped(self):
        await self.propose()
        self.banned.update(self.targets)
        await self.handle("确认补封全部", mid="confirm")
        self.client.admin_restrict.assert_not_awaited()
        self.assertIn("已封禁", "".join(self.replies))

    async def test_unknown_state_never_claims_banned_or_creates_proposal(self):
        self.client.current_restrictions.side_effect = TimeoutError("private")
        await self.propose()
        self.assertNotIn("确认补封全部", "".join(self.replies))
        self.assertNotIn("已自动封禁", "".join(self.replies))
        self.client.admin_restrict.assert_not_awaited()

    async def test_duplicate_confirmation_and_timeout_are_not_resubmitted(self):
        await self.propose()
        self.client.admin_restrict.side_effect = TimeoutError("private")
        await self.handle("确认补封全部", mid="confirm")
        await self.handle("确认补封全部", mid="confirm")
        await self.handle("确认补封全部", mid="new-duplicate")
        self.assertEqual(self.client.admin_restrict.await_count, 2)

    async def test_current_role_rechecked_for_each_target(self):
        from wardogs.domain import Message

        await self.propose()
        check = AsyncMock(side_effect=["admin", "admin", "member"])
        await self.actions.handle(
            Message(WHO, "confirm", time.time(), "确认补封全部"),
            "admin",
            self.replies.append,
            role_check=check,
        )
        self.assertEqual(self.client.admin_restrict.await_count, 1)
        self.assertEqual(check.await_count, 3)

    async def test_nonce_from_superseded_proposal_is_rejected(self):
        await self.propose()
        old = self.store.db.execute(
            "SELECT id FROM admin_batch_restriction_drafts WHERE status='draft'"
        ).fetchone()[0]
        await self.handle("回溯", mid="new-replay")
        await self.handle("1 2", mid="new-select")
        await self.handle("确认补封全部 " + old[:12], mid="old-button")
        self.client.admin_restrict.assert_not_awaited()

    async def test_current_automatic_ban_is_reported_without_a_new_action(self):
        self.banned.update(self.targets)
        original = self.client.current_restrictions.side_effect

        async def automatic(body):
            data = await original(body)
            p = data["players"][0]
            p["currentAutomaticRestriction"] = True
            p["localSource"] = {"category": "automatic", "code": "auto_weapon"}
            return data

        self.client.current_restrictions.side_effect = automatic
        await self.propose()
        self.assertIn("已被智能检测自动封禁", "".join(self.replies))
        self.assertNotIn("确认补封全部", "".join(self.replies))
        self.client.admin_restrict.assert_not_awaited()

    async def test_cancel_bound_batch_does_not_restrict(self):
        await self.propose()
        row = self.store.db.execute(
            "SELECT id FROM admin_batch_restriction_drafts WHERE status='draft'"
        ).fetchone()
        await self.handle("取消批量补封 " + row[0][:12], mid="cancel")
        await self.handle("确认补封全部", mid="after-cancel")
        self.client.admin_restrict.assert_not_awaited()

    async def test_confirmation_during_offer_send_cannot_adopt_unpublished_list(self):
        import asyncio

        tasks = []

        async def notify(text):
            self.replies.append(text)
            if getattr(text, "restriction_code", None):
                tasks.append(
                    asyncio.create_task(self.handle("确认补封全部", mid="early"))
                )
                await asyncio.sleep(0)

        await self.handle()
        await self.handle("1 2", mid="select", notify=notify)
        await asyncio.gather(*tasks)
        self.client.admin_restrict.assert_not_awaited()
