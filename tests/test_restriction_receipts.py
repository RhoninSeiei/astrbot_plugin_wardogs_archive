import time
import unittest
from unittest.mock import AsyncMock

import test_admin_history as fixture

H = fixture.H


class RestrictionReceiptTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.HistoryTests.asyncSetUp
    asyncTearDown = fixture.HistoryTests.asyncTearDown
    handle = fixture.HistoryTests.handle

    async def submit(self):
        await self.handle("踢出 DepartedFixture")
        await self.handle("1", mid="confirm")
        now = time.time()
        self.result = {
            "operationId": "fixture-restrict",
            "steamId": H,
            "restrictionStatus": "restricted",
            "status": "completed",
            "kickStatus": "not_needed",
            "createdAt": now - 1,
            "priorAutomaticKick": {
                "operationId": "prior-auto",
                "steamId": H,
                "source": "auto_kd",
                "kickStatus": "confirmed",
                "confirmedAt": now - 10,
            },
        }
        self.client.admin_operation = AsyncMock(side_effect=lambda _: dict(self.result))

    async def test_prior_confirmed_automatic_kick_and_appended_record_final_reply(self):
        await self.submit()
        await self.actions.poll()
        self.assertIn("此前已被智能检测自动踢出", self.replies[-1])
        self.assertIn("已追加封禁记录", self.replies[-1])
        self.assertIn("永久重入限制", self.replies[-1])
        self.assertNotIn("原生封禁成功", self.replies[-1])
        self.assertFalse(self.client.calls)
        self.assertEqual(len(self.client.restrictions), 1)
        await self.actions.poll()
        self.assertEqual(sum("已追加封禁记录" in s for s in self.replies), 1)

    async def test_absent_or_untrusted_automatic_history_uses_offline_reply(self):
        await self.submit()
        prior = dict(self.result["priorAutomaticKick"])
        for index, change in enumerate(
            [
                None,
                {"source": "admin"},
                {"source": "report"},
                {"kickStatus": "unknown"},
                {"steamId": "76561190000000009"},
                {"confirmedAt": time.time() + 100},
                {"confirmedAt": True},
                {"confirmedAt": self.result["createdAt"] + 0.1},
                {"operationId": "invalid/id"},
            ]
        ):
            with self.subTest(change=change):
                if index:
                    await self.handle("踢出 DepartedFixture", mid=f"new-{index}")
                    await self.handle("1", mid=f"confirm-{index}")
                self.result["priorAutomaticKick"] = (
                    None if change is None else dict(prior, **change)
                )
                await self.actions.poll()
                self.assertIn("处理时已离线", self.replies[-1])
                self.assertIn("已追加封禁记录", self.replies[-1])
                self.assertNotIn("智能检测", self.replies[-1])

    async def test_pending_and_failed_registration_never_claim_appended(self):
        await self.submit()
        before = len(self.replies)
        self.result.update(status="accepted", restrictionStatus="pending")
        await self.actions.poll()
        self.assertEqual(len(self.replies), before)
        self.result.update(status="failed", restrictionStatus="failed")
        await self.actions.poll()
        self.assertIn("登记失败", self.replies[-1])
        self.assertNotIn("已追加", self.replies[-1])
        self.assertNotIn("智能检测", self.replies[-1])

    async def test_announcement_failure_does_not_hide_registered_restriction(self):
        await self.submit()
        self.result["status"] = "announcement_failed"
        await self.actions.poll()
        self.assertIn("已追加封禁记录", self.replies[-1])

    async def test_operation_or_target_mismatch_is_not_success(self):
        await self.submit()
        before = len(self.replies)
        self.result["operationId"] = "wrong"
        await self.actions.poll()
        self.assertEqual(len(self.replies), before)
        self.result["operationId"] = "fixture-restrict"
        self.result["steamId"] = "76561190000000009"
        await self.actions.poll()
        self.assertEqual(len(self.replies), before)

    async def test_result_reply_failure_retries_without_resubmitting(self):
        await self.submit()
        oid, (notify, expires, failures) = next(
            iter(self.actions.history.receipts.items())
        )
        send = AsyncMock(side_effect=[TimeoutError(), None])
        self.actions.history.receipts[oid] = (send, expires, failures)
        await self.actions.poll()
        await self.actions.poll()
        self.assertEqual(send.await_count, 2)
        self.assertEqual(len(self.client.restrictions), 1)
        self.assertFalse(self.actions.history.receipts)

    async def test_pending_expiration_reports_unknown_without_replay(self):
        await self.submit()
        self.result.update(status="accepted", restrictionStatus="pending")
        for oid, (notify, _, failures) in list(self.actions.history.receipts.items()):
            self.actions.history.receipts[oid] = (notify, 0, failures)
        await self.actions.poll()
        self.assertIn("仍待核实", self.replies[-1])
        self.assertNotIn("已追加", self.replies[-1])
        self.assertEqual(len(self.client.restrictions), 1)

    async def test_termination_clears_callbacks_without_changing_audit(self):
        await self.submit()
        self.actions.close()
        self.assertFalse(self.actions.history.receipts)
        await self.actions.poll()
        self.assertNotIn("已追加封禁记录", self.replies[-1])
        self.assertEqual(
            self.store.db.execute(
                "SELECT status FROM admin_restriction_operations"
            ).fetchone()[0],
            "restricted",
        )

    async def test_concurrent_poll_sends_one_final_reply(self):
        import asyncio

        await self.submit()
        await asyncio.gather(self.actions.poll(), self.actions.poll())
        self.assertEqual(sum("已追加封禁记录" in s for s in self.replies), 1)
        self.assertEqual(len(self.client.restrictions), 1)

    async def test_ack_precedes_result_when_poll_runs_during_send(self):
        import asyncio

        await self.handle("踢出 DepartedFixture")
        entered, release = asyncio.Event(), asyncio.Event()

        async def notify(text):
            if "正在追加" in text:
                entered.set()
                await release.wait()
            self.replies.append(text)

        from test_admin import WHO

        from wardogs.domain import Message

        job = asyncio.create_task(
            self.actions.handle(
                Message(WHO, "confirm", time.time(), "1"), "admin", notify
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await self.actions.poll()
            self.assertFalse(any("已追加" in s for s in self.replies))
        finally:
            release.set()
            await job
        await self.actions.poll()
        self.assertIn("正在追加", self.replies[-2])
        self.assertIn("已追加封禁记录", self.replies[-1])

    async def test_official_adapter_preserves_passive_context_for_final_reply(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import patch

        import test_plugin as plugin_fixture
        from botpy.message import GroupMessage

        await self.submit()
        event = plugin_fixture.PluginTests().event(
            "1", images=False, adapter="qq_official"
        )
        raw = GroupMessage.__new__(GroupMessage)
        raw.group_openid = "fixture-group"
        event.message_obj.raw_message = raw
        send = AsyncMock(return_value={"id": "fixture-reply"})
        event.bot = SimpleNamespace(api=SimpleNamespace(post_group_message=send))

        async def notify(text):
            await event.send(event.plain_result(text))

        oid, (_, expires, failures) = next(iter(self.actions.history.receipts.items()))
        self.actions.history.receipts[oid] = (notify, expires, failures)
        with patch(
            "astrbot.core.platform.astr_message_event.Metric.upload", new=AsyncMock()
        ):
            await self.actions.poll()
            await asyncio.sleep(0)
        self.assertEqual(send.await_count, 1)
        payload = send.await_args.kwargs
        self.assertEqual(payload["group_openid"], "fixture-group")
        self.assertEqual(payload["msg_id"], "m1")
        self.assertIn(
            "已追加封禁记录", payload.get("content") or payload["markdown"]["content"]
        )
