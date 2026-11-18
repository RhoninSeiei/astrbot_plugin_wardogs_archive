import asyncio
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_public_queries as fixture

from wardogs.domain import Identity, Message
from wardogs.queries import PublicQueries
from wardogs.rally import DailyRally, PopulationReply, send_all


class RallyTests(unittest.IsolatedAsyncioTestCase):
    answer = fixture.PublicQueryTests.answer
    asyncSetUp = fixture.PublicQueryTests.asyncSetUp
    asyncTearDown = fixture.PublicQueryTests.asyncTearDown

    def set_population(self, n):
        self.client.match.side_effect = None
        self.client.match.return_value = dict(fixture.snapshot(), playerCount=n)

    async def query(self, text="人数", mid="query", sender="member", callback=None):
        return await self.queries.handle(
            Message(
                Identity("official:fixture:app", "g", sender), mid, time.time(), text
            ),
            self.model,
            self.replies.append,
            notify_all=callback,
        )

    async def test_population_boundaries_and_exact_encouragement(self):
        for n in [0, 14, 15, 19, 20, 64]:
            self.set_population(n)
            text = await self.queries.render("population")
            self.assertEqual("低于20" in text, n < 20)
            self.assertEqual("加油啊打灰的大哥哥" in text, n < 20)

    async def test_combined_and_fallback_population_include_low_notice(self):
        self.set_population(18)
        text = await self.queries.render("both")
        self.assertIn("低于20", text)
        self.client.match.side_effect = TimeoutError()
        self.client.state.side_effect = None
        self.client.state.return_value = {
            "playerCount": 17,
            "lastSuccessfulPoll": time.time(),
            "lastError": None,
        }
        text = await self.queries.render("population")
        self.assertIn("低于20", text)
        self.assertEqual(text.population, 17)

    async def test_first_15_to_19_query_calls_once_then_other_members_do_not_repeat(
        self,
    ):
        self.set_population(18)
        all_send = AsyncMock()
        await self.query(callback=all_send)
        await self.query(mid="next", sender="different", callback=all_send)
        all_send.assert_awaited_once()
        self.assertIn("还差2人", all_send.await_args.args[0])
        self.assertEqual(len(self.replies), 1)
        self.assertIn("低于20", self.replies[-1])

    async def test_no_rally_below_15_at_20_or_for_score_only(self):
        all_send = AsyncMock()
        for n in [0, 14, 20, 30]:
            self.set_population(n)
            await self.query(mid=str(n), callback=all_send)
        self.set_population(18)
        await self.query(text="比分", mid="score", callback=all_send)
        all_send.assert_not_awaited()

    async def test_failed_or_stale_count_never_notifies_everyone(self):
        self.client.match.side_effect = None
        self.client.match.return_value = dict(
            fixture.snapshot(), observedAt=time.time() - 1000, playerCount=18
        )
        self.client.state.side_effect = TimeoutError()
        all_send = AsyncMock()
        await self.query(callback=all_send)
        all_send.assert_not_awaited()
        self.assertIn("不可用", self.replies[-1])

    async def test_duplicate_query_message_never_repeats_reply_or_rally(self):
        self.set_population(16)
        all_send = AsyncMock()
        await self.query(callback=all_send)
        await self.query(callback=all_send)
        all_send.assert_awaited_once()
        self.assertFalse(self.replies)

    async def test_restart_and_second_controller_share_daily_limit(self):
        self.set_population(19)
        all_send = AsyncMock()
        await self.query(callback=all_send)
        self.queries = PublicQueries(self.client, self.history)
        await self.query(mid="restart", callback=all_send)
        all_send.assert_awaited_once()

    async def test_concurrent_and_cross_bot_groups_still_only_one_global_daily_attempt(
        self,
    ):
        gate = DailyRally(self.history.db)
        other = DailyRally(self.history.db)
        all_send = AsyncMock()
        who = Identity("official:fixture:app", "g", "member")
        reply = PopulationReply("fixture", 18)
        await asyncio.gather(
            gate.send_once(who, reply, all_send),
            other.send_once(
                replace(who, platform="onebot:other:bot", group="another"),
                reply,
                all_send,
            ),
        )
        all_send.assert_awaited_once()

    async def test_calendar_day_boundary_uses_beijing_not_rolling_24h(self):
        now = [
            datetime(
                2026, 10, 26, 23, 59, 59, tzinfo=timezone(timedelta(hours=8))
            ).timestamp()
        ]
        gate = DailyRally(self.history.db, clock=lambda: now[0])
        who = Identity("x:y:z", "g", "m")
        reply = PopulationReply("fixture", 15)
        all_send = AsyncMock()
        self.assertTrue(await gate.send_once(who, reply, all_send))
        now[0] += 2
        self.assertTrue(await gate.send_once(who, reply, all_send))
        all_send.assert_awaited()
        self.assertEqual(all_send.await_count, 2)

    async def test_timeout_consumes_daily_attempt_and_falls_back_to_normal_reply(self):
        self.set_population(17)
        all_send = AsyncMock(side_effect=TimeoutError("unknown result"))
        await self.query(callback=all_send)
        await self.query(mid="second", callback=all_send)
        all_send.assert_awaited_once()
        self.assertEqual(len(self.replies), 2)
        self.assertTrue(all("加油啊打灰的大哥哥" in x for x in self.replies))

    async def test_without_sender_callback_keeps_plain_notice_and_daily_quota_unused(
        self,
    ):
        self.set_population(18)
        await self.query()
        all_send = AsyncMock()
        await self.query(mid="with-channel", callback=all_send)
        all_send.assert_awaited_once()

    async def test_combined_query_rallies_without_losing_score(self):
        self.set_population(15)
        all_send = AsyncMock()
        await self.query(text="服务器状况", callback=all_send)
        all_send.assert_awaited_once()
        self.assertIn("当前对局比分", all_send.await_args.args[0])
        self.assertIn("还差5人", all_send.await_args.args[0])

    async def test_cancelled_send_is_never_replayed_after_recreation(self):
        reply = PopulationReply("fixture", 18)
        who = Identity("x:y:z", "g", "member")
        send = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await DailyRally(self.history.db).send_once(who, reply, send)
        self.assertFalse(await DailyRally(self.history.db).send_once(who, reply, send))
        send.assert_awaited_once()


class RallySenderTests(unittest.IsolatedAsyncioTestCase):
    def event(self, adapter):
        return SimpleNamespace(
            get_platform_name=lambda: adapter,
            get_platform_id=lambda: "fixture",
            get_group_id=lambda: "123",
            message_obj=SimpleNamespace(self_id="10000001"),
            send=AsyncMock(),
            plain_result=lambda text: SimpleNamespace(
                chain=[SimpleNamespace(text=text)]
            ),
            chain_result=lambda chain: SimpleNamespace(chain=chain),
        )

    async def test_official_native_everyone_markup_is_sent_after_bot_admin_proof(self):
        event = self.event("qq_official")
        api = SimpleNamespace(
            state=AsyncMock(return_value={"member_role": "admin"}), close=AsyncMock()
        )
        inst = SimpleNamespace(meta=lambda: SimpleNamespace(name="qq_official"))
        with patch("wardogs.rally.JoinAPI", return_value=api):
            await send_all(
                event, SimpleNamespace(get_platform_inst=lambda _: inst), "一起打灰"
            )
        self.assertEqual(
            event.send.await_args.args[0].chain[0].text, "<@everyone> 一起打灰"
        )
        api.close.assert_awaited_once()

    async def test_official_non_admin_is_not_sent(self):
        event = self.event("qq_official")
        api = SimpleNamespace(
            state=AsyncMock(return_value={"member_role": "member"}), close=AsyncMock()
        )
        inst = SimpleNamespace(meta=lambda: SimpleNamespace(name="qq_official"))
        with patch("wardogs.rally.JoinAPI", return_value=api):
            with self.assertRaises(PermissionError):
                await send_all(
                    event, SimpleNamespace(get_platform_inst=lambda _: inst), "fixture"
                )
        event.send.assert_not_awaited()
        api.close.assert_awaited_once()

    async def test_onebot_emits_real_at_all_segment_only_for_bot_admin(self):
        event = self.event("aiocqhttp")
        call = AsyncMock(
            return_value={"group_id": 123, "user_id": 10000001, "role": "admin"}
        )
        inst = SimpleNamespace(
            meta=lambda: SimpleNamespace(name="aiocqhttp"),
            get_client=lambda: SimpleNamespace(call_action=call),
        )
        await send_all(
            event, SimpleNamespace(get_platform_inst=lambda _: inst), "fixture"
        )
        self.assertEqual(event.send.await_args.args[0].chain[0].qq, "all")

    async def test_onebot_non_admin_cannot_emit_everyone(self):
        event = self.event("aiocqhttp")
        call = AsyncMock(
            return_value={"group_id": 123, "user_id": 10000001, "role": "member"}
        )
        inst = SimpleNamespace(
            meta=lambda: SimpleNamespace(name="aiocqhttp"),
            get_client=lambda: SimpleNamespace(call_action=call),
        )
        with self.assertRaises(PermissionError):
            await send_all(
                event, SimpleNamespace(get_platform_inst=lambda _: inst), "fixture"
            )
        event.send.assert_not_awaited()

    async def test_official_real_adapter_preserves_native_everyone_text(self):
        import astrbot.api.message_components as Comp
        from astrbot.core.message.message_event_result import MessageChain
        from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
            QQOfficialMessageEvent,
        )

        parsed = await QQOfficialMessageEvent._parse_to_qqofficial(
            MessageChain([Comp.Plain("<@everyone> 一起打灰")])
        )
        self.assertEqual(parsed[0], "<@everyone> 一起打灰")
