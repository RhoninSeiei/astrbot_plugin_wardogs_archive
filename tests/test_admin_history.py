import tempfile
import time
import unittest
from pathlib import Path

from test_admin import WHO, A, Client

from wardogs.admin import AdminKicks
from wardogs.domain import Identity, Message, Store

H = "76561190000000003"


class HistoryClient(Client):
    def __init__(self):
        super().__init__()
        self.searches = []
        self.restrictions = []
        self.history = [
            {
                "steamId": H,
                "name": "DepartedFixture",
                "aliases": ["FormerFixture"],
                "online": False,
                "lastSeen": time.time() - 60,
                "source": "welcome_history",
            }
        ]
        self.search_error = None
        self.restriction_error = None

    async def admin_player_search(self, payload):
        self.searches.append(payload)
        if self.search_error:
            raise self.search_error
        rows = [
            dict(
                p,
                online=any(o["steamId"] == p["steamId"] for o in self.data["players"]),
            )
            if "online" in p
            else dict(p)
            for p in self.history
        ]
        return {
            "players": rows,
            "observedAt": time.time(),
            "truncated": False,
            "historyComplete": True,
        }

    async def admin_restrict(self, payload, key):
        self.restrictions.append((payload, key))
        if self.restriction_error:
            raise self.restriction_error
        return {"operationId": "fixture-restrict", "restrictionStatus": "pending"}

    async def admin_operation(self, oid):
        return {
            "operationId": oid,
            "steamId": H,
            "restrictionStatus": "restricted",
            "status": "completed",
            "kickStatus": "not_needed",
        }


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db")
        self.client = HistoryClient()
        self.actions = AdminKicks(self.store, self.client)
        self.replies = []

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def handle(self, text, mid="request", role="admin", who=WHO):
        return await self.actions.handle(
            Message(who, mid, time.time(), text), role, self.replies.append
        )

    async def test_offline_name_is_visible_as_explicit_restriction_candidate(self):
        await self.handle("踢出 DepartedFixture")
        self.assertIn(H, self.replies[-1])
        self.assertIn("离线", self.replies[-1])
        self.assertIn("永久", self.replies[-1])
        self.assertFalse(self.client.restrictions)
        self.assertFalse(self.client.calls)

    async def test_historical_alias_lookup(self):
        await self.handle("踢出 FormerFixture")
        self.assertIn(H, self.replies[-1])

    async def test_offline_exact_id_requires_confirmation(self):
        await self.handle("踢出 " + H)
        self.assertFalse(self.client.restrictions)
        self.assertIn(H, self.replies[-1])
        await self.handle("1", mid="confirm")
        self.assertEqual(self.client.restrictions[0][0]["steamId"], H)
        self.assertFalse(self.client.calls)

    async def test_online_direct_id_keeps_original_kick_route(self):
        await self.handle("踢出 " + A)
        self.assertEqual(len(self.client.calls), 1)
        self.assertFalse(self.client.restrictions)

    async def test_history_failure_does_not_break_known_online_kick(self):
        self.client.search_error = RuntimeError("history down")
        await self.handle("踢出 " + A)
        self.assertEqual(len(self.client.calls), 1)

    async def test_history_failure_not_reported_as_no_player(self):
        self.client.search_error = RuntimeError("history down")
        await self.handle("踢出 DepartedFixture")
        self.assertIn("历史", self.replies[-1])
        self.assertNotIn("未找到在线玩家", self.replies[-1])
        self.assertFalse(self.client.restrictions)

    async def test_role_scope_expiry_and_replacement_are_preserved(self):
        await self.handle("踢出 DepartedFixture")
        await self.handle("1", mid="member", role="member")
        await self.handle(
            "1", mid="other", who=Identity(WHO.platform, WHO.group, "other")
        )
        self.store.db.execute("UPDATE admin_kick_drafts SET expires=0")
        self.store.db.commit()
        await self.handle("1", mid="expired")
        self.assertFalse(self.client.restrictions)

    async def test_ordinary_member_cannot_query_history(self):
        await self.handle("踢出 DepartedFixture", role="member")
        self.assertFalse(self.client.searches)

    async def test_unknown_exact_id_never_registers_arbitrary_restriction(self):
        self.client.history = []
        await self.handle("踢出 " + H)
        await self.handle("1", mid="confirm")
        self.assertFalse(self.client.restrictions)

    async def test_offline_now_online_does_not_switch_to_kick(self):
        await self.handle("踢出 DepartedFixture")
        self.client.data["players"].append({"steamId": H, "name": "DepartedFixture"})
        await self.handle("1", mid="confirm")
        self.assertFalse(self.client.calls)
        self.assertFalse(self.client.restrictions)

    async def test_online_now_offline_requires_new_restriction_confirmation(self):
        self.client.history = [
            {
                "steamId": A,
                "name": "Example",
                "aliases": [],
                "online": False,
                "lastSeen": time.time() - 1,
                "source": "welcome_history",
            }
        ]
        await self.handle("踢出 Example")
        self.client.data["players"] = []
        await self.handle("1", mid="left")
        self.assertFalse(self.client.restrictions)
        self.assertFalse(self.client.calls)
        self.assertIn("离线", self.replies[-1])
        await self.handle("1", mid="confirm-offline")
        self.assertEqual(len(self.client.restrictions), 1)

    async def test_repeated_confirmation_submits_once(self):
        await self.handle("踢出 DepartedFixture")
        await self.handle("1", mid="confirm")
        await self.handle("1", mid="confirm")
        self.assertEqual(len(self.client.restrictions), 1)

    async def test_timeout_survives_restart_without_replay(self):
        self.client.restriction_error = TimeoutError()
        await self.handle("踢出 DepartedFixture")
        await self.handle("1", mid="confirm")
        self.actions = AdminKicks(self.store, self.client)
        await self.handle("踢出 DepartedFixture", mid="again")
        await self.handle("1", mid="retry")
        self.assertEqual(len(self.client.restrictions), 1)

    async def test_offline_confirmation_does_not_invent_report_evidence(self):
        await self.handle("踢出 DepartedFixture")
        await self.handle("1", mid="confirm")
        payload, key = self.client.restrictions[0]
        self.assertEqual(set(payload), {"requestId", "steamId", "actor", "requestedAt"})
        self.assertEqual(payload["actor"]["role"], "admin")
        self.assertNotIn("decision", payload)

    async def test_missing_online_flag_is_not_inferred_offline(self):
        del self.client.history[0]["online"]
        await self.handle("踢出 DepartedFixture")
        await self.handle("1", mid="confirm")
        self.assertFalse(self.client.restrictions)

    async def test_old_history_does_not_need_to_be_recent_but_response_must_be_fresh(
        self,
    ):
        self.client.history[0]["lastSeen"] = 1
        await self.handle("踢出 DepartedFixture")
        self.assertIn(H, self.replies[-1])

    async def test_explicit_offline_restriction_alias_and_confirmation(self):
        await self.handle("补封 DepartedFixture")
        self.assertIn(H, self.replies[-1])
        self.assertFalse(self.client.restrictions)
        await self.handle("确认补封1", mid="confirm")
        self.assertEqual(len(self.client.restrictions), 1)

    async def test_restriction_poll_does_not_become_confirmed_kick(self):
        await self.handle("踢出 DepartedFixture")
        await self.handle("1", mid="confirm")
        await self.actions.poll()
        self.assertEqual(
            self.store.db.execute(
                "SELECT status FROM admin_restriction_operations"
            ).fetchone()[0],
            "restricted",
        )
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM admin_kick_operations"
            ).fetchone()[0],
            0,
        )

    async def test_stale_directory_snapshot_does_not_allow_restriction(self):
        async def stale(payload):
            return {
                "players": self.client.history,
                "observedAt": time.time() - 120,
                "truncated": False,
                "historyComplete": True,
            }

        self.client.admin_player_search = stale
        await self.handle("踢出 DepartedFixture")
        await self.handle("1", mid="confirm")
        self.assertFalse(self.client.restrictions)
        self.assertIn("历史", self.replies[0])

    async def test_fresh_online_lookup_conflict_cannot_be_assumed_offline(self):
        async def newer(payload):
            return {
                "players": [dict(self.client.history[0], online=True)],
                "observedAt": time.time(),
                "truncated": False,
                "historyComplete": True,
            }

        self.client.admin_player_search = newer
        await self.handle("踢出 DepartedFixture")
        await self.handle("1", mid="confirm")
        self.assertFalse(self.client.restrictions)

    async def test_invalid_historical_identity_has_no_candidate(self):
        self.client.history[0]["steamId"] = "123"
        await self.handle("踢出 DepartedFixture")
        self.assertNotIn(H, self.replies[-1])
        self.assertFalse(self.client.restrictions)

    async def test_offline_draft_action_persists_across_restart(self):
        await self.handle("踢出 DepartedFixture")
        self.actions = AdminKicks(self.store, self.client)
        await self.handle("1", mid="confirm")
        self.assertEqual(len(self.client.restrictions), 1)
        self.assertFalse(self.client.calls)

    async def test_failed_new_lookup_cannot_leave_previous_candidate_confirmable(self):
        await self.handle("踢出 Example")
        self.client.search_error = RuntimeError("history down")
        await self.handle("踢出 DepartedFixture", mid="new-failed")
        await self.handle("1", mid="stale-confirm")
        self.assertFalse(self.client.calls)
        self.assertFalse(self.client.restrictions)

    async def test_unknown_last_seen_does_not_invent_a_timestamp(self):
        self.client.history[0]["lastSeen"] = None
        await self.handle("踢出 DepartedFixture")
        self.assertIn(H, self.replies[-1])
        self.assertFalse(self.client.restrictions)

    async def test_incomplete_history_empty_result_is_not_claimed_absent(self):
        async def partial(payload):
            return {
                "players": [],
                "observedAt": time.time(),
                "truncated": False,
                "historyComplete": False,
            }

        self.client.admin_player_search = partial
        await self.handle("踢出 DepartedFixture")
        self.assertIn("历史", self.replies[-1])
        self.assertNotIn("均未匹配", self.replies[-1])

    async def test_duplicate_online_snapshot_is_not_deduplicated_into_authorization(
        self,
    ):
        self.client.data["players"].append(dict(self.client.data["players"][0]))
        await self.handle("踢出 " + A)
        self.assertFalse(self.client.calls)
        self.assertFalse(self.client.restrictions)

    async def test_explicit_new_offline_id_is_not_old_draft_confirmation(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from wardogs.model import DEFAULT_PROVIDER

        await self.handle("踢出 Example")
        model = SimpleNamespace(
            provider_id=DEFAULT_PROVIDER,
            generate=AsyncMock(
                return_value={"action": "kick", "target": H, "confidence": 0.99}
            ),
        )
        await self.actions.handle(
            Message(WHO, "new-id", time.time(), "补封 " + H),
            "admin",
            self.replies.append,
            model=model,
        )
        self.assertIn(H, self.replies[-1])
        self.assertIn("离线", self.replies[-1])
        self.assertFalse(self.client.restrictions)

    async def test_missing_action_metadata_does_not_convert_offline_to_online_kick(
        self,
    ):
        await self.handle("踢出 DepartedFixture")
        self.store.db.execute("DELETE FROM admin_draft_actions")
        self.store.db.commit()
        self.client.data["players"].append({"steamId": H, "name": "DepartedFixture"})
        await self.handle("1", mid="confirm-after-incomplete-recovery")
        self.assertFalse(self.client.calls)
        self.assertFalse(self.client.restrictions)
