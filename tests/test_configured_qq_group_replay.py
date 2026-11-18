"""Delegated QQ authorization with real official handlers and fake HTTP executor."""

import asyncio
import unittest
from unittest.mock import AsyncMock

import astrbot.api.message_components as Comp
import test_admin_auth as auth
import test_player_kills_chat_replay as base


class QQGrantGroupReplayTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = base.GroupHistoryReplayTests.asyncTearDown
    backend = base.GroupHistoryReplayTests.backend
    receipt = staticmethod(base.GroupHistoryReplayTests.receipt)
    start_query = base.GroupHistoryReplayTests.start_query
    replay = base.GroupHistoryReplayTests.replay

    async def asyncSetUp(self):
        await base.GroupHistoryReplayTests.asyncSetUp(self)
        self.query_command = "查打灰 50"
        self.plugin.config.update(
            authorized_qq=["123456789"],
            official_qq_bindings=[
                {
                    "qq": "123456789",
                    "platform_id": "official-fixture",
                    "app_id": "1234567890",
                    "group_id": "g",
                    "member_id": "u",
                }
            ],
        )

    async def event(self, text, *, role="member", mid=None):
        return await base.GroupHistoryReplayTests.event(self, text, role=role, mid=mid)

    async def test_configured_qq_can_query_and_confirm_kick(self):
        await self.replay(1)
        proof = self.store.db.execute(
            "SELECT qq,platform_role,effective_role,source FROM configured_qq_authorizations"
        ).fetchall()
        self.assertTrue(proof)
        self.assertTrue(
            all(
                tuple(r) == ("123456789", "member", "admin", "configured_qq")
                for r in proof
            )
        )

    async def test_configured_qq_can_confirm_ban(self):
        await self.replay(2)

    async def test_configured_qq_can_confirm_unban(self):
        await self.replay(3)

    async def test_configured_qq_can_confirm_cycle(self):
        await self.replay(4)

    async def test_revoke_after_draft_prevents_final_action(self):
        await self.start_query()
        await self.event("2")
        self.plugin.config["authorized_qq"] = []
        await self.event("1")
        self.assertIn("授权", self.transcript[-1]["text"])
        self.assertFalse(self.posted)

    async def test_audit_failure_blocks_query_and_action(self):
        self.plugin.audit_management_authorization = AsyncMock(
            side_effect=OSError("fixture")
        )
        await self.event("查打灰 50")
        self.assertIn("审计暂不可用", self.transcript[-1]["text"])
        self.assertFalse(self.history_queries)
        self.assertFalse(self.posted)

    async def test_self_identity_is_readonly_and_does_not_grant_authority(self):
        self.plugin.config["authorized_qq"] = []
        event, _, _ = auth.AdminAuthTests().setup_official("member")
        event.message_obj.message = [
            Comp.At(qq="qq_official"),
            Comp.Plain("我的管理身份"),
        ]
        event.send = AsyncMock()
        await self.plugin.on_public_query(event)
        text = event.send.await_args.args[0].chain[0].text
        self.assertIn("不代表获得管理权限", text)
        self.assertIn("member_openid：u", text)
        self.assertFalse(self.history_queries)
        self.assertFalse(self.posted)

    async def test_revocation_while_waiting_for_controller_lock(self):
        await self.plugin.admin_kicks.lock.acquire()
        task = asyncio.create_task(self.event("查打灰 50"))
        try:
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.plugin.config["authorized_qq"] = []
        finally:
            self.plugin.admin_kicks.lock.release()
        await task
        self.assertIn("授权已失效", self.transcript[-1]["text"])
        self.assertFalse(self.history_queries)
        self.assertFalse(self.posted)
