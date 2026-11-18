import time
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

import test_admin as fixture
from test_admin import WHO, A, B

from wardogs.records import record_request


def page(target=A, more=False):
    now = time.time()
    return {
        "observedAt": now,
        "snapshotAt": now,
        "snapshotMode": "membership_fenced_live_status",
        "player": {"steamId": target, "name": "Fixture"},
        "historyComplete": False,
        "coverage": "local_management_ledger_only",
        "currentState": {"nativeBanLiveVerified": False},
        "records": [
            {
                "recordId": "fixture-id",
                "type": "kick_request",
                "timestamp": now - 10,
                "timeBasis": "operation_created_at",
                "effectiveAt": None,
                "reason": None,
                "reasonKnown": False,
                "reasonTruncated": False,
                "source": {"category": "automatic", "code": "auto_burst"},
                "executionStatus": "pending",
                "kickStatus": "pending",
                "outcomes": None,
                "errorCode": None,
            }
        ],
        "hasMore": more,
        "nextCursor": "opaque-fixture" if more else None,
        "responseLimited": False,
    }


class RecordTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixture.AdminTests.asyncTearDown
    message = fixture.AdminTests.message
    handle = fixture.AdminTests.handle

    async def asyncSetUp(self):
        await fixture.AdminTests.asyncSetUp(self)
        self.client.punishment_search = AsyncMock(
            return_value={
                "observedAt": time.time(),
                "complete": True,
                "truncated": False,
                "historyComplete": False,
                "players": [{"steamId": A, "name": "Fixture", "online": None}],
            }
        )
        self.client.punishment_history = AsyncMock(return_value=page())

    async def test_exact_id_is_readonly_and_unknown_reason_not_inferred(self):
        await self.handle("封禁原因 " + A)
        self.client.punishment_history.assert_awaited_once()
        self.assertIn("原因未知", "".join(self.replies))
        self.assertIn("待处理", "".join(self.replies))
        self.assertNotIn("已踢出", "".join(self.replies))
        self.assertFalse(self.client.calls)

    async def test_nickname_candidate_then_number_views_records_not_kick(self):
        await self.handle("处罚记录 Fixture")
        self.client.punishment_history.assert_not_awaited()
        self.assertIn("查看", "".join(self.replies))
        await self.handle("1", mid="choose-records")
        self.client.punishment_history.assert_awaited_once()
        self.assertFalse(self.client.calls)

    async def test_member_denied_before_any_lookup(self):
        await self.handle("封禁原因 Fixture", role="member")
        self.client.punishment_search.assert_not_awaited()
        self.client.punishment_history.assert_not_awaited()

    async def test_candidate_scope_expiry_and_prepublication(self):
        early = replace(self.message("1", mid="early"), at=time.time() - 10)
        await self.handle("处罚记录 Fixture")
        await self.actions.handle(early, "admin", self.replies.append)
        await self.handle("1", mid="other", who=replace(WHO, sender="other"))
        self.store.db.execute(
            "UPDATE admin_record_drafts SET expires=?", (time.time() - 1,)
        )
        self.store.db.commit()
        await self.handle("1", mid="expired")
        self.client.punishment_history.assert_not_awaited()

    async def test_next_page_uses_bound_cursor_target_and_fresh_actor(self):
        self.client.punishment_history.return_value = page(more=True)
        await self.handle("封禁原因 " + A)
        await self.handle("下一页", mid="page-two")
        body = self.client.punishment_history.await_args.args[0]
        self.assertEqual(body["steamId"], A)
        self.assertEqual(body["cursor"], "opaque-fixture")
        self.assertGreaterEqual(body["requestedAt"], time.time() - 5)
        self.assertFalse(self.client.calls)

    async def test_page_permission_rechecked(self):
        self.client.punishment_history.return_value = page(more=True)
        await self.handle("封禁原因 " + A)
        await self.handle("下一页", mid="member", role="member")
        self.assertEqual(self.client.punishment_history.await_count, 1)

    async def test_query_replaces_kick_and_kick_replaces_query(self):
        await self.handle("踢出 Example", mid="kick")
        await self.handle("处罚记录 Fixture", mid="records")
        await self.handle("1", mid="record-choice")
        self.assertFalse(self.client.calls)
        await self.handle("处罚记录 Fixture", mid="records-two")
        await self.handle("踢出 Example", mid="kick-two")
        await self.handle("1", mid="kick-choice")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.punishment_history.await_count, 1)

    async def test_duplicate_messages_do_not_refetch_or_repeat_reply(self):
        await self.handle("封禁原因 " + A, mid="same")
        count = len(self.replies)
        await self.handle("封禁原因 " + A, mid="same")
        self.assertEqual(self.client.punishment_history.await_count, 1)
        self.assertEqual(len(self.replies), count)

    async def test_wrong_target_stale_and_invalid_page_rejected(self):
        for kind in ["target", "stale", "cursor"]:
            data = page(B if kind == "target" else A)
            if kind == "stale":
                data["observedAt"] = time.time() - 100
            if kind == "cursor":
                data["hasMore"] = True
            self.client.punishment_history.return_value = data
            await self.handle("处罚记录 " + A, mid=kind)
            self.assertIn("不可核验", self.replies[-1])
        self.assertFalse(self.client.calls)

    async def test_truncated_search_has_no_selectable_candidate(self):
        self.client.punishment_search.return_value["truncated"] = True
        await self.handle("处罚记录 Fixture")
        await self.handle("1", mid="truncated-choice")
        self.client.punishment_history.assert_not_awaited()

    async def test_restriction_registration_is_not_confirmed_kick(self):
        data = page()
        data["records"][0].update(
            type="rejoin_restriction",
            executionStatus="registered",
            kickStatus="not_applicable",
            reason="fixture-rule",
            reasonKnown=True,
        )
        self.client.punishment_history.return_value = data
        await self.handle("封禁原因 " + A)
        self.assertIn("重入限制", "".join(self.replies))
        self.assertNotIn("已踢出", "".join(self.replies))

    async def test_confirmed_kick_and_creation_time_labeled(self):
        data = page()
        data["records"][0].update(
            type="kick_confirmation",
            executionStatus="confirmed",
            kickStatus="confirmed",
            timeBasis="kick_confirmed_at",
        )
        self.client.punishment_history.return_value = data
        await self.handle("处罚记录 " + A)
        self.assertIn("已踢出", "".join(self.replies))
        self.assertIn("确认", "".join(self.replies))

    async def test_empty_known_history_and_service_failure_not_fake_empty(self):
        data = page()
        data["records"] = []
        self.client.punishment_history.return_value = data
        await self.handle("处罚记录 " + A)
        self.assertIn("未找到", "".join(self.replies))
        self.client.punishment_history.side_effect = TimeoutError()
        await self.handle("处罚记录 " + A, mid="unavailable")
        self.assertIn("不可核验", self.replies[-1])

    async def test_public_query_yields_record_command_without_model(self):
        from wardogs.queries import PublicQueries

        model = AsyncMock()
        self.assertFalse(
            await PublicQueries(self.client, None).handle(
                self.message("封禁原因 " + A), model, self.replies.append
            )
        )
        model.generate.assert_not_awaited()

    def test_explicit_aliases_negative_and_ambiguous_input(self):
        for text in [
            "封禁原因 Fixture",
            "查一下处罚记录 Fixture",
            "查封禁记录 Fixture",
        ]:
            self.assertEqual(record_request(text)["query"], "Fixture")
        self.assertIsNone(record_request("不要查封禁原因 Fixture"))
        self.assertTrue(record_request("封禁原因 " + A + " " + B)["clarify"])

    async def test_page_cursor_cannot_cross_group_bot_or_sender(self):
        self.client.punishment_history.return_value = page(more=True)
        await self.handle("处罚记录 " + A)
        for who in [
            replace(WHO, sender="other"),
            replace(WHO, group="other"),
            replace(WHO, platform="other:bot"),
        ]:
            await self.handle("下一页", mid="foreign", who=who)
        self.assertEqual(self.client.punishment_history.await_count, 1)

    async def test_inconsistent_confirmation_is_not_reported_as_kick(self):
        data = page()
        data["records"][0].update(
            type="rejoin_restriction",
            executionStatus="registered",
            kickStatus="confirmed",
        )
        self.client.punishment_history.return_value = data
        await self.handle("处罚记录 " + A)
        self.assertIn("不可核验", self.replies[-1])
        self.assertNotIn("已踢出", "".join(self.replies))

    async def test_visible_names_and_reason_cannot_generate_mentions(self):
        data = page()
        data["player"]["name"] = "@all\nFixture"
        data["records"][0].update(reason="@all fake\nreason", reasonKnown=True)
        self.client.punishment_history.return_value = data
        await self.handle("处罚记录 " + A)
        self.assertNotIn("@", "".join(self.replies))

    async def test_client_only_uses_admin_readonly_endpoints_with_bounded_body(self):
        from wardogs.io import ServiceClient

        client = ServiceClient("http://fixture.invalid", "/not-read")
        client.request = AsyncMock(return_value={})
        body = {"actor": {}, "requestedAt": time.time()}
        await client.punishment_search(dict(body, query="Fixture"))
        await client.punishment_history(dict(body, steamId=A, limit=5))
        calls = client.request.await_args_list
        self.assertEqual(
            [c.args[1] for c in calls],
            ["/v1/moderation/punishment-search", "/v1/moderation/punishment-history"],
        )
        self.assertTrue(
            all(
                c.kwargs.get("admin") is True
                and c.kwargs.get("response_limit") == 65536
                for c in calls
            )
        )
        self.assertFalse(self.client.calls)

    async def test_long_candidate_list_is_complete_and_chunked_before_selection(self):
        ids = [str(76561190000000100 + i) for i in range(20)]
        self.client.punishment_search.return_value["players"] = [
            {"steamId": x, "name": "虚构昵称" * 32, "online": None} for x in ids
        ]
        await self.handle("处罚记录 Fixture")
        self.assertGreater(len(self.replies), 1)
        self.assertTrue(all(len(text) <= 1700 for text in self.replies))
        self.assertTrue(all(x in "".join(self.replies) for x in ids))
        self.client.punishment_history.return_value = page(ids[-1])
        await self.handle("20", mid="last-person")
        self.assertEqual(
            self.client.punishment_history.await_args.args[0]["steamId"], ids[-1]
        )
        self.assertFalse(self.client.calls)
