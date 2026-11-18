"""Short query uses the same official event and real loopback HTTP flow."""

import unittest

import test_player_kills_chat_replay as replay


class ShortHistoryReplayTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = replay.GroupHistoryReplayTests.asyncSetUp
    asyncTearDown = replay.GroupHistoryReplayTests.asyncTearDown
    backend = replay.GroupHistoryReplayTests.backend
    receipt = staticmethod(replay.GroupHistoryReplayTests.receipt)
    event = replay.GroupHistoryReplayTests.event
    start_query = replay.GroupHistoryReplayTests.start_query
    replay = replay.GroupHistoryReplayTests.replay
    query_command = "查 打灰 50"

    async def test_short_fuzzy_fifty_query_then_confirmed_kick(self):
        await self.replay(1)
