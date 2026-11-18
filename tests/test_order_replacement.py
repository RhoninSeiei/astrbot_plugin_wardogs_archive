import asyncio
import json
import unittest
from types import SimpleNamespace

import test_admin_natural as fixture
from test_admin import A, B

from wardogs.model import DEFAULT_PROVIDER, FALLBACK_PROVIDER

NAME = "没我大嘴炒不熟的饭"


def response(action, target=""):
    return SimpleNamespace(completion_text=json.dumps(fixture.output(action, target)))


class OrderReplacementTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.NaturalAdminTests.asyncSetUp
    asyncTearDown = fixture.NaturalAdminTests.asyncTearDown
    handle = fixture.NaturalAdminTests.handle
    answer = fixture.NaturalAdminTests.answer

    async def old_draft(self):
        self.answer(fixture.output("kick", "Example"))
        await self.handle("踢出 Example", mid="old")
        self.client.data["players"][1]["name"] = NAME

    async def test_primary_none_with_explicit_order_uses_luna(self):
        self.client.data["players"][1]["name"] = NAME
        self.context.llm_generate.side_effect = [
            response("none"),
            response("kick", NAME),
        ]
        await self.handle("踢" + NAME)
        self.assertIn(NAME, self.replies[-1])
        self.assertIn(B, self.replies[-1])
        self.assertFalse(self.client.calls)
        self.assertEqual(
            [
                c.kwargs["chat_provider_id"]
                for c in self.context.llm_generate.await_args_list
            ],
            [DEFAULT_PROVIDER, FALLBACK_PROVIDER],
        )

    async def test_new_none_invalidates_old_draft_and_replies(self):
        await self.old_draft()
        self.answer(fixture.output("none", ""))
        handled = await self.handle("踢" + NAME, mid="new")
        self.assertTrue(handled)
        self.assertIn("旧候选", self.replies[-1])
        await self.handle("1", mid="confirm")
        self.assertFalse(self.client.calls)

    async def test_new_model_failure_cannot_leave_old_target_confirmable(self):
        await self.old_draft()
        self.context.llm_generate.side_effect = TimeoutError()
        await self.handle("踢" + NAME, mid="new")
        await self.handle("1", mid="confirm")
        self.assertFalse(self.client.calls)

    async def test_new_clarification_cannot_leave_old_target_confirmable(self):
        await self.old_draft()
        self.answer(fixture.output("clarify", ""))
        await self.handle("踢" + NAME, mid="new")
        await self.handle("1", mid="confirm")
        self.assertFalse(self.client.calls)

    async def test_recovered_new_request_confirmation_uses_new_candidate(self):
        await self.old_draft()
        self.context.llm_generate.side_effect = [
            response("none"),
            response("kick", NAME),
        ]
        await self.handle("踢" + NAME, mid="new")
        await self.handle("1", mid="confirm")
        self.assertEqual(self.client.calls[0][0]["steamId"], B)
        self.assertNotEqual(self.client.calls[0][0]["steamId"], A)

    async def test_unknown_new_name_cannot_reuse_previous_draft(self):
        await self.old_draft()
        self.answer(fixture.output("kick", "UnmatchedNickname"))
        await self.handle("踢UnmatchedNickname", mid="new")
        await self.handle("1", mid="confirm")
        self.assertFalse(self.client.calls)

    async def test_queued_confirmation_during_rejected_new_order_never_uses_old(self):
        await self.old_draft()
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(**kwargs):
            entered.set()
            await release.wait()
            return response("none")

        self.context.llm_generate.side_effect = slow
        new = asyncio.create_task(self.handle("踢" + NAME, mid="new"))
        await entered.wait()
        confirm = asyncio.create_task(self.handle("1", mid="confirm"))
        release.set()
        await asyncio.gather(new, confirm)
        self.assertFalse(self.client.calls)

    async def test_non_order_stays_silent_and_does_not_revoke_existing_draft(self):
        await self.old_draft()
        self.answer(fixture.output("none", ""))
        self.assertFalse(await self.handle("有人说踢了别的玩家", mid="discussion"))
        await self.handle("1", mid="confirm")
        self.assertEqual(self.client.calls[0][0]["steamId"], A)
