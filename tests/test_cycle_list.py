import time
import unittest
from unittest.mock import AsyncMock

import test_admin as fixture
from test_admin import A, B

from wardogs.cycle_list import list_command, render_list


def snapshot(rows=None):
    return {
        "observedAt": time.time(),
        "complete": True,
        "players": rows
        if rows is not None
        else [{"steamId": A, "name": "Fixture", "enabled": True, "online": True}],
    }


class RenderingTests(unittest.TestCase):
    def test_aliases(self):
        for text in [
            "循环名单",
            "哈哈名单",
            "查看哈哈名单",
            "当前循环名单",
            "哈哈名单什么意思",
            "哈哈名单有哪些人",
        ]:
            self.assertTrue(list_command(text), text)
        for text in ["循环 Fixture", "解封 Fixture", "不要查看哈哈名单"]:
            self.assertFalse(list_command(text), text)

    def test_list_shows_current_players_and_explains_actual_policy(self):
        text = "\n".join(render_list(snapshot()))
        for part in [A, "Fixture", "在线", "换队", "击杀", "哈哈"]:
            self.assertIn(part, text)

    def test_empty_list_still_explains_meaning(self):
        text = "\n".join(render_list(snapshot([])))
        self.assertIn("0人", text)
        self.assertIn("换队", text)

    def test_disabled_rows_are_not_current_members(self):
        rows = [
            {"steamId": A, "name": "ActiveFixture", "enabled": True, "online": False},
            {"steamId": B, "name": "DisabledFixture", "enabled": False, "online": None},
        ]
        text = "\n".join(render_list(snapshot(rows)))
        self.assertIn(A, text)
        self.assertNotIn(B, text)
        self.assertIn("离线", text)

    def test_unknown_online_state_does_not_claim_offline(self):
        text = "\n".join(
            render_list(snapshot([{"steamId": A, "name": "Fixture", "online": None}]))
        )
        self.assertIn("在线状态未知", text)

    def test_stale_incomplete_and_wrong_ids_rejected(self):
        for data in [
            dict(snapshot(), observedAt=time.time() - 100),
            dict(snapshot(), complete=False),
            snapshot([{"steamId": "wrong", "name": "Fixture", "online": True}]),
        ]:
            with self.assertRaises(ValueError):
                render_list(data)

    def test_large_list_chunks_and_mentions_are_sanitized(self):
        rows = [
            {
                "steamId": str(76561190000001000 + i),
                "name": "@" + "玩家" * 40,
                "online": None,
            }
            for i in range(100)
        ]
        chunks = render_list(snapshot(rows))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1700 for chunk in chunks))
        self.assertNotIn("@", "".join(chunks))
        for row in rows:
            self.assertIn(row["steamId"], "".join(chunks))


class ListRoutingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.AdminTests.asyncSetUp
    asyncTearDown = fixture.AdminTests.asyncTearDown
    message = fixture.AdminTests.message
    handle = fixture.AdminTests.handle

    async def test_admin_reads_without_model_or_punishment(self):
        self.client.cycle_list = AsyncMock(return_value=snapshot())
        model = AsyncMock()
        await self.actions.handle(
            self.message("哈哈名单"), "admin", self.replies.append, model=model
        )
        self.client.cycle_list.assert_awaited_once()
        model.generate.assert_not_awaited()
        self.assertFalse(self.client.calls)
        self.assertIn(A, "".join(self.replies))

    async def test_member_cannot_query_identities(self):
        self.client.cycle_list = AsyncMock(return_value=snapshot())
        await self.handle("哈哈名单", role="member")
        self.client.cycle_list.assert_not_awaited()
        self.assertIn("管理员", self.replies[-1])

    async def test_list_query_revokes_old_ordinal_drafts(self):
        self.client.cycle_list = AsyncMock(return_value=snapshot())
        await self.handle("踢出 Example", mid="old")
        await self.handle("哈哈名单", mid="list")
        await self.handle("1", mid="ordinal")
        self.assertFalse(self.client.calls)

    async def test_duplicate_old_list_does_not_clear_new_draft(self):
        self.client.cycle_list = AsyncMock(return_value=snapshot())
        await self.handle("哈哈名单", mid="old-list")
        await self.handle("踢出 Example", mid="new")
        await self.handle("哈哈名单", mid="old-list")
        await self.handle("1", mid="ordinal")
        self.assertEqual(len(self.client.calls), 1)
        self.client.cycle_list.assert_awaited_once()

    async def test_unavailable_list_does_not_use_cached_player_data(self):
        self.client.cycle_list = AsyncMock(side_effect=OSError("fixture unavailable"))
        await self.handle("循环名单")
        self.assertIn("暂不可用", self.replies[-1])
        self.assertNotIn(A, self.replies[-1])
        self.assertFalse(self.client.calls)
