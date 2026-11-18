import time
import unittest
from unittest.mock import AsyncMock

import test_player_kills as kill_fixture
from test_admin import A

from wardogs.bans import receipt


def result(state="pending", native="not_checked", local="not_attempted"):
    return {
        "operationId": "fixture-ban",
        "steamId": A,
        "source": "admin",
        "action": "ban",
        "status": "accepted"
        if state == "pending"
        else "completed"
        if state == "banned"
        else state,
        "banStatus": state,
        "nativeBanStatus": native,
        "localRestrictionStatus": local,
        "nativeVerifiedAt": time.time()
        if native in ("banned", "already_banned")
        else None,
        "localVerifiedAt": time.time()
        if local in ("restricted", "already_restricted")
        else None,
        "onlineBefore": False,
        "removalStatus": "not_needed",
    }


class BanTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = kill_fixture.PlayerKillTests.asyncTearDown
    handle = kill_fixture.PlayerKillTests.handle

    async def choose(self):
        await self.handle("击杀记录 " + A)
        await self.handle("2")
        self.client.admin_ban.assert_not_awaited()
        await self.handle("1")

    async def asyncSetUp(self):
        await kill_fixture.PlayerKillTests.asyncSetUp(self)
        self.client.admin_ban = AsyncMock(return_value=result())
        self.client.admin_operation = AsyncMock(
            return_value=result("banned", "banned", "restricted")
        )

    async def test_ban_is_explicit_and_distinct_from_kick(self):
        await self.choose()
        self.client.admin_ban.assert_awaited_once()
        self.assertEqual(self.client.admin_ban.call_args.args[0]["steamId"], A)
        self.assertFalse(self.client.calls)
        self.assertNotIn("已封禁", self.replies[-1])
        await self.actions.poll()
        self.assertIn("已封禁", self.replies[-1])
        self.assertIn("处理时离线", self.replies[-1])

    async def test_unknown_dispatch_blocks_later_kick_unban_cycle(self):
        self.client.admin_ban.side_effect = TimeoutError()
        await self.choose()
        self.assertIn("待核实", self.replies[-1])
        self.client.admin_unban = AsyncMock()
        self.client.admin_cycle_add = AsyncMock()
        for command in ["踢出 " + A, "循环 " + A, "解封 " + A]:
            await self.handle(command)
            self.assertIn("待核实", self.replies[-1])
        self.client.admin_unban.assert_not_awaited()
        self.client.admin_cycle_add.assert_not_awaited()
        self.assertFalse(self.client.calls)

    async def test_partial_receipt_is_not_full_ban_success_and_not_replayed(self):
        self.client.admin_ban.return_value = result("partial", "failed", "restricted")
        await self.choose()
        self.assertIn("未全部完成", self.replies[-1])
        self.assertIn("原生封禁失败", self.replies[-1])
        self.assertNotIn("已封禁，", self.replies[-1])
        await self.handle("击杀记录 " + A)
        await self.handle("2")
        await self.handle("1")
        self.client.admin_ban.assert_awaited_once()

    async def test_mismatched_operation_receipt_never_claims_success(self):
        await self.choose()
        data = result("banned", "banned", "restricted")
        data["operationId"] = "wrong-operation"
        self.client.admin_operation.return_value = data
        await self.actions.poll()
        self.assertNotIn("已封禁", "\n".join(self.replies))

    def test_receipt_needs_native_and_local_verification(self):
        data = result("banned", "banned", "restricted")
        self.assertEqual(receipt(data, A), "banned")
        for key in ["nativeVerifiedAt", "localVerifiedAt"]:
            broken = dict(data)
            broken[key] = None
            with self.assertRaises(ValueError):
                receipt(broken, A)
        data["nativeBanStatus"] = "failed"
        with self.assertRaises(ValueError):
            receipt(data, A)
