"""Nickname annotations require one exact, fresh directory identity."""

import time
import unittest
from unittest.mock import AsyncMock

import test_player_kills as fixture
from test_admin import A


def directory():
    return {
        "observedAt": time.time(),
        "players": [
            {
                "steamId": A,
                "name": "CurrentFixture",
                "aliases": ["FormerFixture", "Example"],
                "online": True,
                "source": "online",
                "lastSeen": time.time(),
            }
        ],
        "historyComplete": True,
        "truncated": False,
    }


class HistoryAliasDisplayTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PlayerKillTests.asyncSetUp
    asyncTearDown = fixture.PlayerKillTests.asyncTearDown
    handle = fixture.PlayerKillTests.handle

    async def test_precise_id_shows_verified_latest_name_and_historical_marker(self):
        self.client.admin_player_search = AsyncMock(side_effect=lambda _: directory())
        await self.handle("查 " + A)
        text = "\n".join(self.replies)
        self.assertIn("当前或最近昵称：CurrentFixture", text)
        self.assertIn("曾用名：", text)
        self.assertIn("FormerFixture", text)
        self.assertIn("同一SteamID的历史昵称", text)
        self.assertIn("操作对象：CurrentFixture", self.replies[-1])
        self.assertFalse(self.client.calls)

    async def test_old_alias_query_keeps_precise_target_through_numeric_kick(self):
        self.client.admin_player_search = AsyncMock(side_effect=lambda _: directory())
        await self.handle("查 FormerFixture")
        self.assertIn("CurrentFixture", self.replies[-1])
        await self.handle("1")
        self.assertIn("曾用名：", "\n".join(self.replies))
        await self.handle("1")
        self.assertFalse(self.client.calls)
        await self.handle("1")
        self.assertEqual(self.client.calls[0][0]["steamId"], A)

    async def test_directory_failure_does_not_guess_a_current_nickname(self):
        self.client.admin_player_search = AsyncMock(side_effect=TimeoutError())
        await self.handle("查 " + A)
        text = "\n".join(self.replies)
        self.assertIn("当前或最近昵称：暂不可核验", text)
        self.assertIn("Example", text)
        self.assertNotIn("同一SteamID的历史昵称", text)
        self.assertFalse(self.client.calls)

    async def test_wrong_or_stale_directory_does_not_claim_current_name(self):
        for mutate in [
            lambda d: d.update(observedAt=time.time() - 30),
            lambda d: d["players"][0].update(steamId="76561190000000099"),
        ]:
            data = directory()
            mutate(data)
            self.client.admin_player_search = AsyncMock(return_value=data)
            await self.handle("查 " + A)
            self.assertIn("当前或最近昵称：暂不可核验", "\n".join(self.replies[-6:]))
            self.assertNotIn("操作对象：CurrentFixture", self.replies[-1])

    async def test_current_record_name_is_not_marked_as_history(self):
        data = directory()
        data["players"][0]["name"] = "Example"
        data["players"][0]["aliases"] = []
        self.client.admin_player_search = AsyncMock(return_value=data)
        await self.handle("查 " + A)
        text = "\n".join(self.replies)
        self.assertIn("当前或最近昵称：Example", text)
        self.assertNotIn("同一SteamID的历史昵称", text)
        self.assertIn("未发现其他昵称", text)
