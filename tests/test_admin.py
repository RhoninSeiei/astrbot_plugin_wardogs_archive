import asyncio
import tempfile
import time
import unittest
from pathlib import Path

import test_plugin as plugin_fixture

from wardogs.admin import AdminKicks, parse_request
from wardogs.domain import Identity, Message, Store

A = "76561190000000001"
B = "76561190000000002"
WHO = Identity("official:fixture:app", "test-group", "test-admin")


class Client:
    def __init__(self):
        self.calls = []
        self.data = {
            "players": [
                {"steamId": A, "name": "Example"},
                {"steamId": B, "name": "Examples"},
            ],
            "observedAt": time.time(),
        }
        self.fail = False

    async def players(self):
        return self.data

    async def admin_kick(self, payload, key):
        self.calls.append((payload, key))
        if self.fail:
            raise TimeoutError()
        return {"operationId": "fake-operation", "kickStatus": "queued"}


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db")
        self.client = Client()
        self.actions = AdminKicks(self.store, self.client)
        self.replies = []

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def message(self, text, mid="message", who=WHO):
        return Message(who, mid, time.time(), text)

    async def handle(self, text, mid="message", who=WHO, role="admin"):
        return await self.actions.handle(
            self.message(text, mid, who), role, self.replies.append
        )

    async def test_precise_id_executes_once_without_model_report(self):
        await self.handle("请踢出 " + A)
        self.assertEqual(len(self.client.calls), 1)
        payload, key = self.client.calls[0]
        self.assertEqual(payload["steamId"], A)
        self.assertEqual(payload["actor"]["role"], "admin")
        self.assertNotIn("decision", payload)
        self.assertTrue(key.startswith("wd-admin-"))

    async def test_owner_is_authorized(self):
        await self.handle("踢出 " + A, role="owner")
        self.assertEqual(len(self.client.calls), 1)

    async def test_member_and_unknown_cannot_kick(self):
        for role in ["member", "", None, "ADMIN"]:
            await self.handle("踢出 " + A, role=role)
        self.assertFalse(self.client.calls)

    async def test_name_even_exact_requires_confirmation_with_id(self):
        await self.handle("踢出 Example")
        self.assertFalse(self.client.calls)
        self.assertIn(A, self.replies[-1])
        self.assertIn(B, self.replies[-1])
        await self.handle("确认踢出 " + A, mid="confirmation")
        self.assertEqual(len(self.client.calls), 1)

    async def test_confirmation_without_exact_id_never_executes(self):
        await self.handle("踢出 Example")
        await self.handle("确认踢出 Example", mid="confirm")
        self.assertFalse(self.client.calls)

    async def test_confirmation_rechecks_current_role(self):
        await self.handle("踢出 Example")
        await self.handle("确认 " + A, mid="confirm", role="member")
        self.assertFalse(self.client.calls)

    async def test_confirmation_scope_sender_group_and_bot(self):
        await self.handle("踢出 Example")
        for who in [
            Identity(WHO.platform, WHO.group, "other"),
            Identity(WHO.platform, "other", WHO.sender),
            Identity("other", WHO.group, WHO.sender),
        ]:
            await self.handle("确认 " + A, mid="confirm", who=who)
        self.assertFalse(self.client.calls)

    async def test_confirmation_expired(self):
        await self.handle("踢出 Example")
        self.store.db.execute("UPDATE admin_kick_drafts SET expires=0")
        self.store.db.commit()
        await self.handle("确认 " + A, mid="confirm")
        self.assertFalse(self.client.calls)

    async def test_confirmation_id_must_be_from_candidates(self):
        await self.handle("踢出 Example")
        self.client.data["players"].append(
            {"steamId": "76561190000000003", "name": "Unrelated"}
        )
        await self.handle("确认 76561190000000003", mid="confirm")
        self.assertFalse(self.client.calls)

    async def test_confirmation_rechecks_departure(self):
        await self.handle("踢出 Example")
        self.client.data["players"] = []
        await self.handle("确认 " + A, mid="confirm")
        self.assertFalse(self.client.calls)

    async def test_stale_and_future_rosters_block(self):
        for observed in [0, time.time() + 100, True, float("nan")]:
            self.client.data["observedAt"] = observed
            await self.handle("踢出 " + A)
        self.assertFalse(self.client.calls)

    async def test_duplicate_message_and_racing_handlers_only_submit_once(self):
        await asyncio.gather(self.handle("踢出 " + A), self.handle("踢出 " + A))
        self.assertEqual(len(self.client.calls), 1)

    async def test_timeout_survives_restart_and_blocks_blind_retry(self):
        self.client.fail = True
        await self.handle("踢出 " + A)
        self.actions = AdminKicks(self.store, self.client)
        await self.handle("踢出 " + A, mid="retry")
        self.assertEqual(len(self.client.calls), 1)
        self.assertIn("核实", self.replies[-1])

    async def test_duplicate_online_id_blocks(self):
        self.client.data["players"].append({"steamId": A, "name": "Duplicate"})
        await self.handle("踢出 " + A)
        self.assertFalse(self.client.calls)

    async def test_negation_discussion_and_quoted_id_are_not_orders(self):
        for text in [
            "不要踢出 " + A,
            "怎么踢出 " + A,
            "如果踢出 " + A,
            "他说踢出 " + A,
            "踢出 " + A + "？",
            "确认",
        ]:
            self.assertIsNone(parse_request(text))
        self.assertIsNone(parse_request("举报 " + A + " 圈外开火"))

    async def test_multiple_ids_and_fullwidth_id_do_not_execute(self):
        await self.handle("踢出 " + A + " " + B)
        await self.handle("踢出 ７６５６１１９０００００００００１", mid="wide")
        self.assertFalse(self.client.calls)

    async def test_target_before_verb_is_supported(self):
        await self.handle("请把 " + A + " 踢出")
        self.assertEqual(len(self.client.calls), 1)

    async def test_id_embedded_in_ambiguous_description_is_rejected(self):
        await self.handle("踢出 Example 举报人ID " + A)
        self.assertFalse(self.client.calls)

    async def test_confirmation_survives_restart_without_cross_sender_transfer(self):
        await self.handle("踢出 Example")
        self.actions = AdminKicks(self.store, self.client)
        await self.handle("确认踢出 " + A, mid="confirm")
        self.assertEqual(len(self.client.calls), 1)

    async def test_status_poll_only_reads_and_does_not_rekick(self):
        from unittest.mock import AsyncMock

        await self.handle("踢出 " + A)
        self.client.admin_operation = AsyncMock(
            return_value={
                "operationId": "fake-operation",
                "steamId": A,
                "kickStatus": "confirmed",
            }
        )
        await self.actions.poll()
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(
            self.store.db.execute(
                "SELECT status FROM admin_kick_operations"
            ).fetchone()[0],
            "confirmed",
        )

    async def test_exact_id_alone_confirms_only_existing_draft(self):
        await self.handle(A)
        self.assertFalse(self.client.calls)
        await self.handle("踢出 Example", mid="name")
        await self.handle(A, mid="exact-id-confirmation")
        self.assertEqual(len(self.client.calls), 1)


class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = AdminTests.asyncSetUp
    asyncTearDown = AdminTests.asyncTearDown
    message = AdminTests.message
    handle = AdminTests.handle

    async def status(self, kick="confirmed", **extra):
        from unittest.mock import AsyncMock

        self.client.admin_operation = AsyncMock(
            return_value={
                "operationId": "fake-operation",
                "steamId": A,
                "kickStatus": kick,
                **extra,
            }
        )
        await self.actions.poll()

    async def test_ack_then_confirmed_once_even_when_announcement_fails(self):
        await self.handle("踢出 " + A)
        self.assertEqual(self.replies, ["已受理"])
        await self.status(announcementStatus="failed")
        await self.actions.poll()
        self.assertEqual(self.replies, ["已受理", "已踢出：" + A])
        self.assertEqual(len(self.client.calls), 1)

    async def test_queued_and_wrong_identity_never_claim_success(self):
        await self.handle("踢出 " + A)
        await self.status("queued", status="completed")
        await self.status(steamId=B)
        await self.status(operationId="different-operation")
        self.assertEqual(self.replies, ["已受理"])
        await self.status()
        self.assertEqual(self.replies[-1], "已踢出：" + A)

    async def test_terminal_non_success_is_reported_without_kick_claim(self):
        for kick, text in [
            ("target_offline", "已离线"),
            ("failed", "失败"),
            ("unknown", "待核实"),
            ("cancelled", "取消"),
        ]:
            with self.subTest(kick=kick):
                self.store.db.execute("DELETE FROM admin_kick_operations")
                self.store.db.commit()
                self.replies.clear()
                await self.handle("踢出 " + A, mid=kick)
                await self.status(kick)
                self.assertEqual(len(self.replies), 2)
                self.assertIn(text, self.replies[-1])
                self.assertNotIn("已踢出", self.replies[-1])

    async def test_backend_retry_keeps_one_submission_until_terminal_receipt(self):
        outcomes = [
            ("confirmed", {}, "已踢出：" + A),
            ("not_needed", {"status": "offline"}, "玩家已离线，未执行踢出：" + A),
            ("failed", {}, "踢出失败：" + A),
            ("unknown", {}, "踢出结果待核实，请勿重复提交：" + A),
        ]
        for final_kick, extra, expected in outcomes:
            with self.subTest(final_kick=final_kick):
                self.store.db.execute("DELETE FROM admin_kick_operations")
                self.store.db.commit()
                self.replies.clear()
                self.client.calls.clear()
                await self.handle("踢出 " + A, mid="retry-" + final_kick)
                for pending_status, pending_kick in [
                    ("kick_dispatching", "dispatching"),
                    ("resolving", "not_started"),
                    ("kick_dispatching", "dispatching"),
                ]:
                    await self.status(pending_kick, status=pending_status)
                    self.assertEqual(self.replies, ["已受理"])
                    self.assertEqual(
                        self.store.db.execute(
                            "SELECT status FROM admin_kick_operations"
                        ).fetchone()[0],
                        "accepted",
                    )
                await self.status(final_kick, **extra)
                await self.actions.poll()
                self.assertEqual(self.replies, ["已受理", expected])
                self.assertEqual(len(self.client.calls), 1)
                self.assertFalse(self.actions.receipts)

    async def test_concurrent_poll_sends_one_final_reply(self):
        await self.handle("踢出 " + A)
        await self.status("queued")

        async def delayed(_):
            await asyncio.sleep(0)
            return {
                "operationId": "fake-operation",
                "steamId": A,
                "kickStatus": "confirmed",
            }

        self.client.admin_operation = delayed
        await asyncio.gather(self.actions.poll(), self.actions.poll())
        self.assertEqual(self.replies.count("已踢出：" + A), 1)

    async def test_notification_failure_never_replays_punishment(self):
        replies = []

        async def notify(text):
            if text.startswith("已踢出") and not replies:
                replies.append("send_failed")
                raise OSError("synthetic_send_failure")
            self.replies.append(text)

        await self.actions.handle(self.message("踢出 " + A), "admin", notify)
        await self.status()
        await self.actions.poll()
        self.assertEqual(self.replies, ["已受理", "已踢出：" + A])
        self.assertEqual(len(self.client.calls), 1)

    async def test_poll_during_ack_keeps_reply_order(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def notify(text):
            if text == "已受理":
                entered.set()
                await release.wait()
            self.replies.append(text)

        job = asyncio.create_task(
            self.actions.handle(self.message("踢出 " + A), "admin", notify)
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await self.status()
            self.assertEqual(self.replies, [])
        finally:
            release.set()
            await job
        await self.actions.poll()
        self.assertEqual(self.replies, ["已受理", "已踢出：" + A])

    async def test_pending_receipt_expires_without_changing_operation(self):
        await self.handle("踢出 " + A)
        for oid, (notify, _, failures) in list(self.actions.receipts.items()):
            self.actions.receipts[oid] = (notify, 0, failures)
        await self.status("queued")
        self.assertIn("待核实", self.replies[-1])
        self.assertFalse(self.actions.receipts)
        self.assertEqual(
            self.store.db.execute(
                "SELECT status FROM admin_kick_operations"
            ).fetchone()[0],
            "accepted",
        )
        self.assertEqual(len(self.client.calls), 1)

    async def test_close_releases_event_callbacks_and_restart_keeps_audit(self):
        await self.handle("踢出 " + A)
        self.actions.close()
        self.assertFalse(self.actions.receipts)
        self.actions = AdminKicks(self.store, self.client)
        await self.status()
        self.assertEqual(self.replies, ["已受理"])
        self.assertEqual(
            self.store.db.execute(
                "SELECT status FROM admin_kick_operations"
            ).fetchone()[0],
            "confirmed",
        )
        self.assertEqual(len(self.client.calls), 1)

    async def test_official_adapter_sends_two_replies_with_original_message_context(
        self,
    ):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from botpy.message import GroupMessage

        event = plugin_fixture.PluginTests().event(
            "踢出 " + A, images=False, adapter="qq_official"
        )
        raw = GroupMessage.__new__(GroupMessage)
        raw.group_openid = "fixture-group"
        event.message_obj.raw_message = raw
        send = AsyncMock(return_value={"id": "fixture-reply"})
        event.bot = SimpleNamespace(api=SimpleNamespace(post_group_message=send))

        async def notify(text):
            await event.send(event.plain_result(text))

        with patch(
            "astrbot.core.platform.astr_message_event.Metric.upload", new=AsyncMock()
        ):
            await self.actions.handle(self.message("踢出 " + A), "admin", notify)
            await self.status()
            await asyncio.sleep(0)
        self.assertEqual(send.await_count, 2)
        payloads = [call.kwargs for call in send.await_args_list]
        self.assertEqual([p["group_openid"] for p in payloads], ["fixture-group"] * 2)
        self.assertEqual([p["msg_id"] for p in payloads], ["m1"] * 2)
        self.assertEqual(
            [p.get("content") or p["markdown"]["content"] for p in payloads],
            ["已受理", "已踢出：" + A],
        )
        self.assertTrue(all(isinstance(p["msg_seq"], int) for p in payloads))
