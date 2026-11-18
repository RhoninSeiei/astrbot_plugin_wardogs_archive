import unittest
from unittest.mock import AsyncMock

import test_public_queries as fixture
from test_server_identity import SID, snapshot


class PublicIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixture.PublicQueryTests()
        await self.fixture.asyncSetUp()
        self.fixture.client.server_identity = AsyncMock(side_effect=snapshot)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_all_simple_aliases_are_public_without_llm_or_admin_api(self):
        for i, text in enumerate(
            ["服务器id", "服id", "服地址", "服务器地址", "服务器 ID？", "/服id"]
        ):
            self.assertTrue(await self.fixture.handle(text, mid=str(i)))
            self.assertIn(SID, self.fixture.replies[-1])
            self.assertIn("游戏左下角", self.fixture.replies[-1])
            self.assertIn("ID加入", self.fixture.replies[-1])
        self.fixture.context.llm_generate.assert_not_awaited()
        self.fixture.client.admin_kick.assert_not_awaited()

    async def test_status_includes_identity_and_keeps_population_metadata(self):
        await self.fixture.handle("服况")
        self.assertIn(SID, self.fixture.replies[-1])
        self.assertIn("31", self.fixture.replies[-1])
        self.assertIn("红方 120", self.fixture.replies[-1])
        self.assertEqual(self.fixture.replies[-1].population, 31)
        self.fixture.context.llm_generate.assert_not_awaited()

    async def test_identity_failure_does_not_hide_status_and_uses_safe_message(self):
        self.fixture.client.server_identity.side_effect = TimeoutError("private-token")
        await self.fixture.handle("服况")
        text = self.fixture.replies[-1]
        self.assertIn("31", text)
        self.assertIn("红方 120", text)
        self.assertIn("服务器ID暂不可用", text)
        self.assertNotIn("private-token", text)

    async def test_identity_duplicate_is_not_replied_twice(self):
        await self.fixture.handle("服id")
        await self.fixture.handle("服id")
        self.assertEqual(len(self.fixture.replies), 1)
        self.fixture.client.server_identity.assert_awaited_once()

    async def test_population_and_score_only_do_not_read_identity(self):
        for i, text in enumerate(["人数", "比分"]):
            await self.fixture.handle(text, mid=str(i))
        self.fixture.client.server_identity.assert_not_awaited()

    async def test_stale_or_bad_identity_is_not_shown_as_address(self):
        for i, data in enumerate(
            [
                dict(snapshot(), observedAt=1),
                dict(snapshot(), serverId="192.0.2.15:1234"),
            ]
        ):
            self.fixture.client.server_identity.side_effect = None
            self.fixture.client.server_identity.return_value = data
            await self.fixture.handle("服地址", mid=str(i))
            self.assertIn("服务器ID暂不可用", self.fixture.replies[-1])
            self.assertNotIn("192.0.2.15", self.fixture.replies[-1])

    async def test_slash_status_uses_local_query_and_includes_id(self):
        await self.fixture.handle("/服况")
        self.assertIn(SID, self.fixture.replies[-1])
        self.fixture.context.llm_generate.assert_not_awaited()
