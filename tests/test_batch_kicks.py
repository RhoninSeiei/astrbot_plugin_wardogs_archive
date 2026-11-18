import asyncio
import json
import time
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

import test_kill_replay as replay_fixture
from test_admin import WHO, A, B

from wardogs.admin import AdminKicks
from wardogs.batch import batch_selection
from wardogs.replay import replay_command, replay_window


class BatchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = replay_fixture.ReplayTests.asyncSetUp
    asyncTearDown = replay_fixture.ReplayTests.asyncTearDown
    handle = replay_fixture.ReplayTests.handle

    async def test_replay_multiple_ordinals_kick_bound_players(self):
        await self.handle()
        await self.handle("1 3 5", mid="batch")
        self.assertEqual(
            [p[0]["steamId"] for p in self.client.calls],
            [self.result["events"][i]["killer"]["steamId"] for i in [0, 2, 4]],
        )

    async def test_default_kick_candidates_support_batch(self):
        await self.handle("踢出 Example", mid="candidate")
        await self.handle("1 2", mid="batch")
        self.assertEqual({p[0]["steamId"] for p in self.client.calls}, {A, B})

    async def test_all_ordinals_validated_before_any_dispatch(self):
        await self.handle()
        await self.handle("1 999", mid="invalid")
        self.assertFalse(self.client.calls)
        self.assertIn("序号", self.replies[-1])

    async def test_duplicate_rows_and_ordinals_only_kick_each_player_once(self):
        self.result["events"][1]["killer"] = dict(self.result["events"][0]["killer"])
        await self.handle()
        await self.handle("1 2 1 3", mid="batch")
        self.assertEqual(len(self.client.calls), 2)

    async def test_unknown_killer_rejects_whole_batch(self):
        self.result["events"][1]["killer"]["steamId"] = None
        await self.handle()
        await self.handle("1 2", mid="unknown")
        self.assertFalse(self.client.calls)

    async def test_offline_target_is_skipped_without_implicit_restriction(self):
        await self.handle()
        offline = self.result["events"][0]["killer"]["steamId"]
        self.client.data["players"] = [
            p for p in self.client.data["players"] if p["steamId"] != offline
        ]
        await self.handle("1 2", mid="batch")
        self.assertEqual(len(self.client.calls), 1)
        self.assertIn("离线", "".join(self.replies))
        self.assertFalse(
            self.store.db.execute(
                "SELECT id FROM admin_restriction_operations"
            ).fetchall()
        )

    async def test_member_and_other_sender_cannot_use_batch(self):
        await self.handle()
        await self.handle("1 2", mid="member", role="member")
        await self.handle("1 2", mid="other", who=replace(WHO, sender="other"))
        self.assertFalse(self.client.calls)

    async def test_expired_and_newer_list_are_not_adopted(self):
        early = self.result["observedAt"] - 10
        await self.handle()
        from wardogs.domain import Message

        await self.actions.handle(
            Message(WHO, "early", early, "1 2"), "admin", self.replies.append
        )
        self.assertFalse(self.client.calls)
        self.store.db.execute(
            "UPDATE admin_replay_drafts SET expires=?", (time.time() - 1,)
        )
        self.store.db.commit()
        await self.handle("1 2", mid="expired")
        self.assertFalse(self.client.calls)

    async def test_repeat_and_restart_do_not_resubmit_batch(self):
        await self.handle()
        await self.handle("1 2", mid="batch")
        self.assertEqual(len(self.client.calls), 2)
        self.actions.close()
        self.actions = AdminKicks(self.store, self.client)
        await self.handle("1 2", mid="batch")
        self.assertEqual(len(self.client.calls), 2)

    async def test_batch_is_not_applied_to_cycle_or_unban_candidates(self):
        await self.handle("循环 Example", mid="cycle")
        self.client.admin_cycle_add = AsyncMock()
        await self.handle("1 2", mid="batch")
        self.assertFalse(self.client.calls)
        self.client.admin_cycle_add.assert_not_awaited()

    async def test_request_timeout_does_not_replay_first_target_and_continues_second(
        self,
    ):
        await self.handle()
        original = self.client.admin_kick

        async def submit(payload, key):
            result = await original(payload, key)
            if len(self.client.calls) == 1:
                raise TimeoutError("synthetic reply lost")
            return result

        self.client.admin_kick = submit
        await self.handle("1 2", mid="batch")
        await self.handle("1 2", mid="batch")
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM admin_kick_operations WHERE status='outcome_unknown'"
            ).fetchone()[0],
            1,
        )

    async def test_final_receipts_identify_original_ordinal(self):
        await self.handle()
        original = self.client.admin_kick

        async def submit(payload, key):
            result = await original(payload, key)
            result["operationId"] = "op-" + payload["steamId"]
            return result

        self.client.admin_kick = submit
        await self.handle("1 3", mid="batch")

        async def status(op):
            return {"operationId": op, "steamId": op[3:], "kickStatus": "confirmed"}

        self.client.admin_operation = status
        await self.actions.poll()
        self.assertTrue(any("序号1" in t and "已踢出" in t for t in self.replies))
        self.assertTrue(any("序号3" in t and "已踢出" in t for t in self.replies))

    async def test_five_minute_replay_requests_and_renders_full_window(self):
        self.result["windowStart"] = self.result["windowEnd"] - 300
        self.result["events"][0]["receivedAt"] = self.result["windowEnd"] - 250
        await self.handle("5分钟回溯")
        self.assertEqual(
            self.client.recent_kills.await_args.args[0]["windowSeconds"], 300
        )
        self.assertIn("近5分钟", self.replies[0])
        await self.handle("1 2", mid="five-select")
        self.assertEqual(len(self.client.calls), 2)

    async def test_wrong_window_cannot_activate_five_minute_list(self):
        await self.handle("回溯5分钟")
        self.assertIsNone(
            self.actions.replay.snapshot((WHO.platform, WHO.group, WHO.sender))
        )
        self.assertFalse(self.client.calls)

    def test_parser_is_bounded_and_does_not_accept_arbitrary_ids(self):
        self.assertEqual(batch_selection("1 3 5"), [1, 3, 5])
        self.assertEqual(batch_selection("踢出 1 3"), [1, 3])
        self.assertIsNone(batch_selection("1"))
        self.assertIsNone(batch_selection("不要踢1 3"))
        self.assertEqual(batch_selection("0 2"), [])
        self.assertEqual(batch_selection(" ".join(map(str, range(1, 22)))), [])
        self.assertEqual(batch_selection(A + " " + B), [])

    def test_five_minute_aliases_and_default_preserved(self):
        for text in [
            "5分钟回溯",
            "回溯5分钟",
            "五分钟回溯",
            "回溯五分钟",
            "/wd 回溯 5m",
        ]:
            self.assertTrue(replay_command(text))
            self.assertEqual(replay_window(text), 300)
        self.assertEqual(replay_window("回溯"), 60)
        self.assertFalse(replay_command("10分钟回溯"))

    async def test_current_role_rechecked_for_each_target(self):
        await self.handle()
        from wardogs.domain import Message

        role_check = AsyncMock(side_effect=["admin", "admin", "member"])
        await self.actions.handle(
            Message(WHO, "revoke", time.time(), "1 2"),
            "admin",
            self.replies.append,
            role_check=role_check,
        )
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(role_check.await_count, 3)
        self.assertIn(
            "权限",
            str(
                json.loads(
                    self.store.db.execute(
                        "SELECT results FROM admin_kick_batches WHERE status='waiting'"
                    ).fetchone()[0]
                )
            ),
        )

    async def test_cancellation_does_not_resume_undispatched_targets(self):
        await self.handle()
        original = self.client.admin_kick

        async def cancel(payload, key):
            await original(payload, key)
            raise asyncio.CancelledError()

        self.client.admin_kick = cancel
        with self.assertRaises(asyncio.CancelledError):
            await self.handle("1 2", mid="cancelled-batch")
        self.actions.close()
        self.actions = AdminKicks(self.store, self.client)
        await self.handle("1 2", mid="cancelled-batch")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM admin_kick_operations WHERE status='outcome_unknown'"
            ).fetchone()[0],
            1,
        )

    async def test_new_group_and_bot_cannot_adopt_original_snapshot(self):
        await self.handle()
        for who in [
            replace(WHO, group="another-group"),
            replace(WHO, platform="other:fixture:bot"),
        ]:
            await self.handle("1 2", mid="wrong-scope", who=who)
        self.assertFalse(self.client.calls)

    async def test_public_query_yields_multi_ordinals_without_model(self):
        from wardogs.domain import Message
        from wardogs.queries import PublicQueries

        model = AsyncMock()
        self.assertFalse(
            await PublicQueries(self.client, None).handle(
                Message(WHO, "multi", time.time(), "1 2"), model, self.replies.append
            )
        )
        model.generate.assert_not_awaited()

    async def test_batch_sends_one_ack_and_one_summary(self):
        await self.handle()
        original = self.client.admin_kick

        async def submit(payload, key):
            result = await original(payload, key)
            result["operationId"] = "op-" + payload["steamId"]
            return result

        self.client.admin_kick = submit
        before = len(self.replies)
        await self.handle("1 2 3 4 5", mid="batch-five")
        self.assertEqual(len(self.replies) - before, 1)

        async def status(op):
            return {"operationId": op, "steamId": op[3:], "kickStatus": "confirmed"}

        self.client.admin_operation = status
        await self.actions.poll()
        await self.actions.poll()
        self.assertEqual(len(self.replies) - before, 2)
        self.assertEqual(self.replies[-1].count("已踢出"), 5)

    async def test_concurrent_summary_flush_sends_once(self):
        await self.handle()
        await self.handle("1 2", mid="batch-concurrent")
        oid, info = next(iter(self.actions.batch.receipts.items()))
        for item in info["items"]:
            info["results"][item["target"]] = "已踢出：" + item["target"]
        entered = asyncio.Event()
        release = asyncio.Event()
        sent = []

        async def notify(text):
            sent.append(text)
            entered.set()
            await release.wait()

        info["notify"] = notify
        first = asyncio.create_task(self.actions.batch.flush(oid))
        await entered.wait()
        await self.actions.batch.flush(oid)
        release.set()
        await first
        self.assertEqual(len(sent), 1)
        self.assertNotIn(oid, self.actions.batch.receipts)
