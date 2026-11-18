"""Natural chat orders use the LLM; permission and target gates stay deterministic."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import astrbot.api.message_components as Comp
import test_admin_auth as auth
import test_plugin as pt
from test_admin import WHO, A, B, Client

from wardogs.admin import AdminKicks
from wardogs.domain import Message, Store
from wardogs.model import DEFAULT_PROVIDER, FALLBACK_PROVIDER, ContextModel


def output(action="kick", target=A, confidence=0.99):
    return {"action": action, "target": target, "confidence": confidence}


class NaturalAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db")
        self.client = Client()
        self.actions = AdminKicks(self.store, self.client)
        self.replies = []
        self.context = SimpleNamespace(llm_generate=AsyncMock())
        self.model = ContextModel(self.context, "fixture")
        self.answer(output())

    def answer(self, value):
        self.context.llm_generate.side_effect = None
        self.context.llm_generate.return_value = SimpleNamespace(
            completion_text=json.dumps(value)
        )

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def handle(self, text, mid="message", role="admin", who=WHO):
        return await self.actions.handle(
            Message(who, mid, time.time(), text),
            role,
            self.replies.append,
            model=self.model,
        )

    async def test_natural_precise_order_uses_llm_and_fixed_payload(self):
        await self.handle("这个ID " + A + " 一直捣乱，麻烦让他离开服务器吧")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.calls[0][0]["steamId"], A)
        self.context.llm_generate.assert_awaited_once()
        self.assertIsNone(self.context.llm_generate.await_args.kwargs["tools"])
        self.assertEqual(
            set(self.client.calls[0][0]),
            {"requestId", "steamId", "actor", "requestedAt"},
        )

    async def test_rejected_exact_order_replies_without_regex_execution(self):
        self.answer(output("none", "", 0.99))
        self.assertTrue(await self.handle("踢出 " + A))
        self.assertFalse(self.client.calls)
        self.assertEqual(self.context.llm_generate.await_count, 2)
        self.assertIn("未执行", self.replies[-1])

    async def test_natural_name_and_followup_precise_confirmation(self):
        self.answer(output("kick", "Example"))
        await self.handle("Example这个人一直捣乱，把他请出去吧")
        self.assertFalse(self.client.calls)
        self.assertIn(A, self.replies[-1])
        self.answer(output("kick", A))
        await self.handle("就是 " + A + "，按刚才说的处理吧", "confirm")
        self.assertEqual(len(self.client.calls), 1)
        prompt = json.loads(self.context.llm_generate.await_args.kwargs["prompt"])
        self.assertEqual(prompt["pending_candidate_ids"], [A, B])

    async def test_ban_alias_is_game_kick_and_name_needs_exact_confirmation(self):
        self.answer(output("kick", "Example"))
        await self.handle("封禁Example")
        self.assertFalse(self.client.calls)
        self.assertIn(A, self.replies[-1])
        self.answer(output("confirm", A))
        await self.handle("确认封禁 " + A, "confirm-ban")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(
            set(self.client.calls[0][0]),
            {"requestId", "steamId", "actor", "requestedAt"},
        )

    async def test_member_ban_alias_denied_before_model(self):
        for text in ["封禁Example", "永封Example", "ban Example"]:
            self.assertTrue(await self.handle(text, role="member"))
        self.assertEqual(len(self.replies), 3)
        self.context.llm_generate.assert_not_awaited()
        self.assertFalse(self.client.calls)

    async def test_unban_is_handled_separately_and_never_becomes_kick(self):
        for text in ["解封 " + A, "解除封禁 " + A]:
            self.assertTrue(await self.handle(text))
        self.context.llm_generate.assert_not_awaited()
        self.assertFalse(self.client.calls)

    async def test_ban_negation_and_unsupported_english_unban_cannot_become_kick(self):
        for text in ["别封禁 " + A, "unban " + A, "UNBAN " + A]:
            self.assertFalse(await self.handle(text))
        self.assertFalse(self.client.calls)

    async def test_member_denied_before_model_and_roster(self):
        self.client.players = AsyncMock(side_effect=AssertionError("must_not_query"))
        await self.handle("把 " + A + " 请出去吧", role="member")
        self.context.llm_generate.assert_not_awaited()
        self.client.players.assert_not_awaited()
        self.assertFalse(self.client.calls)

    async def test_model_cannot_invent_or_replace_id(self):
        self.answer(output("kick", B))
        await self.handle("把 " + A + " 请出服务器")
        self.assertFalse(self.client.calls)

    async def test_model_cannot_invent_name(self):
        self.answer(output("kick", "Example"))
        await self.handle("把那个人请出去")
        self.assertFalse(self.client.calls)

    async def test_multiple_ids_stay_ambiguous_even_if_model_chooses_one(self):
        await self.handle("举报人 " + B + " 说把 " + A + " 踢了")
        self.assertFalse(self.client.calls)

    async def test_low_confidence_never_dispatches(self):
        self.answer(output(confidence=0.5))
        await self.handle("大概是 " + A + "，处理一下")
        self.assertFalse(self.client.calls)

    async def test_invalid_step_falls_back_to_luna_and_validates_output(self):
        self.context.llm_generate.side_effect = [
            SimpleNamespace(completion_text='{"action":"execute_script"}'),
            SimpleNamespace(completion_text=json.dumps(output())),
        ]
        await self.handle("麻烦让 " + A + " 离开服务器")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(
            [
                c.kwargs["chat_provider_id"]
                for c in self.context.llm_generate.await_args_list
            ],
            [DEFAULT_PROVIDER, FALLBACK_PROVIDER],
        )

    async def test_uncertain_step_uses_luna_decision(self):
        self.context.llm_generate.side_effect = [
            SimpleNamespace(completion_text=json.dumps(output("clarify", "", 0.5))),
            SimpleNamespace(completion_text=json.dumps(output())),
        ]
        await self.handle("麻烦让 " + A + " 离开服务器")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(
            self.context.llm_generate.await_args.kwargs["chat_provider_id"],
            FALLBACK_PROVIDER,
        )

    async def test_both_providers_fail_without_regex_execution(self):
        self.context.llm_generate.side_effect = RuntimeError("synthetic_failure")
        await self.handle("踢出 " + A)
        self.assertFalse(self.client.calls)
        self.assertIn("未执行", self.replies[-1])

    async def test_discussion_negation_and_question_model_none(self):
        self.answer(output("none", "", 0.99))
        for text in [
            "别踢 " + A,
            "如果踢了 " + A + " 会怎么样",
            "能不能踢 " + A + "？",
            "有人说把 " + A + " 踢了",
            "举报这个人圈外击杀",
        ]:
            self.assertFalse(await self.handle(text))
        self.assertFalse(self.client.calls)

    async def test_draft_does_not_allow_new_id_even_when_model_calls_it_new_order(self):
        self.answer(output("kick", "Example"))
        await self.handle("Example请出去")
        other = "76561190000000003"
        self.client.data["players"].append({"steamId": other, "name": "Else"})
        self.answer(output("kick", other))
        await self.handle("就是 " + other + "，处理吧", "confirm")
        self.assertFalse(self.client.calls)

    async def test_model_misclassification_cannot_override_explicit_non_orders(self):
        for text in [
            "先别踢 " + A,
            "模拟踢出 " + A,
            "如果踢出 " + A,
            "有人说踢出 " + A,
        ]:
            self.assertFalse(await self.handle(text))
        self.assertFalse(self.client.calls)

    async def test_duplicate_does_not_call_model_twice(self):
        await self.handle("把 " + A + " 请出服务器")
        await self.handle("把 " + A + " 请出服务器")
        self.assertEqual(len(self.client.calls), 1)
        self.context.llm_generate.assert_awaited_once()


class NaturalEventTests(unittest.IsolatedAsyncioTestCase):
    event = pt.PluginTests.event
    setup_official = auth.AdminAuthTests.setup_official

    async def exercise(self, text, role="admin", mentioned=True, quote=""):
        e, c, cfg = self.setup_official(role)
        e.message_obj.message = [Comp.Plain(text)]
        if mentioned:
            e.message_obj.message.append(Comp.At(qq="qq_official"))
        if quote:
            e.message_obj.message.append(Comp.Reply(id="quoted", message_str=quote))
        e.send = AsyncMock()
        c.llm_generate = AsyncMock(
            return_value=SimpleNamespace(completion_text=json.dumps(output()))
        )
        p = pt.WardogsPlugin(c, cfg)
        p.ready = True
        p.admin_kicks = SimpleNamespace(handle=AsyncMock(return_value=True))
        await p.on_admin_order(e)
        return e, c, p

    async def test_natural_event_reaches_controller_with_model(self):
        e, c, p = await self.exercise("麻烦把 " + A + " 请出服务器，影响游戏了")
        p.admin_kicks.handle.assert_awaited_once()
        self.assertIsNotNone(p.admin_kicks.handle.await_args.kwargs["model"])
        self.assertIn("请出服务器", p.admin_kicks.handle.await_args.args[0].text)

    async def test_official_own_opaque_mention_uses_application_scope(self):
        e, c, cfg = self.setup_official()
        e.message_obj.self_id = "synthetic-official-bot-openid"
        e.message_obj.message = [
            Comp.Plain("封禁LilyFixture"),
            Comp.At(qq=e.message_obj.self_id, name="TestModerator"),
        ]
        e.send = AsyncMock()
        p = pt.WardogsPlugin(c, cfg)
        p.ready = True
        p.admin_kicks = SimpleNamespace(handle=AsyncMock(return_value=True))
        await p.on_admin_order(e)
        args = p.admin_kicks.handle.await_args.args
        self.assertEqual(args[1], "admin")
        self.assertEqual(
            args[0].who.platform, "qq_official:official-fixture:1234567890"
        )
        self.assertEqual(args[0].text, "封禁LilyFixture")

    async def test_no_at_remains_outside_manual_path(self):
        e, c, p = await self.exercise("把 " + A + " 请出去", mentioned=False)
        p.admin_kicks.handle.assert_not_awaited()
        c.llm_generate.assert_not_awaited()

    async def test_quote_not_included_in_model_message(self):
        e, c, p = await self.exercise("看看这句话", quote="踢出 " + A)
        self.assertEqual(p.admin_kicks.handle.await_args.args[0].text, "看看这句话")

    async def test_overlong_message_not_silently_truncated_into_order(self):
        e, c, p = await self.exercise("踢出 " + A + "a" * 1100)
        p.admin_kicks.handle.assert_not_awaited()
