import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_admin import WHO, A

from wardogs.batch_restrictions import ban_state, banned_label, confirmation


def state(target=A, automatic=False, local=False, native=False):
    now = time.time()
    return {
        "observedAt": now,
        "complete": True,
        "players": [
            {
                "steamId": target,
                "online": False,
                "onlineObservedAt": now,
                "localRestricted": local,
                "localRestrictionStatus": "active" if local else None,
                "localObservedAt": now,
                "nativeBanned": native,
                "nativeObservedAt": now,
                "nativeSyncState": None,
                "currentAutomaticRestriction": automatic,
                "sourceComplete": automatic is not None,
                "localSource": {
                    "category": (
                        "automatic"
                        if automatic
                        else "unknown"
                        if automatic is None
                        else "manual"
                    )
                    if local
                    else "none",
                    "code": "auto_weapon" if automatic else None,
                },
                "nativeSource": {
                    "category": (
                        "automatic"
                        if automatic
                        else "unknown"
                        if automatic is None
                        else "manual"
                    )
                    if native
                    else "none",
                    "code": "auto_native" if automatic else None,
                },
            }
        ],
    }


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def query(self, data):
        return await ban_state(
            SimpleNamespace(current_restrictions=AsyncMock(return_value=data)),
            WHO,
            "admin",
            A,
        )

    async def test_automatic_current_local_restriction_is_not_native_ban_claim(self):
        result = await self.query(state(automatic=True, local=True))
        self.assertTrue(result["banned"])
        self.assertTrue(result["automatic"])
        self.assertIn("自动封禁", banned_label(result))

    async def test_unknown_current_origin_still_blocks_redundant_ban(self):
        result = await self.query(state(automatic=None, native=True))
        self.assertTrue(result["banned"])
        self.assertFalse(result["automatic"])
        self.assertIn("来源未能确认", banned_label(result))

    async def test_old_auto_history_cannot_label_released_player_as_banned(self):
        data = state()
        data["priorAutomaticKick"] = {"kickStatus": "confirmed"}
        result = await self.query(data)
        self.assertFalse(result["banned"])
        self.assertFalse(result["automatic"])

    async def test_current_manual_ban_is_not_automatic(self):
        result = await self.query(state(local=True))
        self.assertNotIn("自动", banned_label(result))

    async def test_incomplete_stale_mismatched_or_inconsistent_state_rejected(self):
        good = state()
        cases = [None, dict(good, complete=False), dict(good, observedAt=1)]
        for changes in [
            {"steamId": "76561190000000099"},
            {"onlineObservedAt": 1},
            {"nativeObservedAt": True},
            {"nativeBanned": "false"},
            {"currentAutomaticRestriction": True},
            {"nativeSyncState": "unknown"},
        ]:
            bad = copy.deepcopy(good)
            bad["players"][0].update(changes)
            cases.append(bad)
        for data in cases:
            with self.assertRaises((ValueError, AttributeError)):
                await self.query(data)

    def test_confirmation_keeps_precise_id_flow_and_bounds_ordinals(self):
        self.assertIsNone(confirmation("补封 " + A))
        self.assertEqual(confirmation("补封 3 5")["ordinals"], [3, 5])
        self.assertEqual(confirmation("补封 0 5")["ordinals"], [])
        self.assertIsNone(confirmation("不要确认补封全部"))

    async def test_real_service_client_uses_existing_admin_token_and_fixed_path(self):
        from pathlib import Path

        import test_io

        f = test_io.ClientTests()
        await f.asyncSetUp()
        try:
            key = Path(f.tmp.name) / "admin"
            key.write_text("synthetic-admin-key")
            f.c.admin_key_file = key
            await f.c.current_restrictions(
                {"steamIds": [A], "actor": {}, "requestedAt": time.time()}
            )
            self.assertEqual(
                f.calls[0][:3],
                (
                    "POST",
                    "/v1/moderation/current-restrictions",
                    "Bearer synthetic-admin-key",
                ),
            )
            self.assertIsNone(f.calls[0][3])
        finally:
            await f.asyncTearDown()
