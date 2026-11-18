import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_admin import WHO, A, B, Client

from wardogs.admin import AdminKicks
from wardogs.domain import Identity, Message, Store


class OrdinalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db")
        self.client = Client()
        self.actions = AdminKicks(self.store, self.client)
        self.replies = []

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def handle(self, text, mid="confirm", who=WHO, role="admin", model=None):
        return await self.actions.handle(
            Message(who, mid, time.time(), text), role, self.replies.append, model=model
        )

    async def draft(self):
        await self.handle("踢出 Example", mid="draft")

    async def test_candidates_numbered(self):
        await self.draft()
        self.assertIn("1. Example " + A, self.replies[-1])
        self.assertIn("2. Examples " + B, self.replies[-1])

    async def test_confirm_second_maps_stored_id_not_live_order(self):
        await self.draft()
        self.client.data["players"].reverse()
        await self.handle("确认踢出 2")
        self.assertEqual(self.client.calls[0][0]["steamId"], B)

    async def test_explicit_natural_selection_variants(self):
        for text in [
            "确认踢出 1",
            "确认1",
            "确认第1个",
            "就踢第1个",
            "确认封禁1号",
            "踢出1号",
            "1",
        ]:
            with self.subTest(text=text):
                await self.draft()
                await self.handle(text, mid=text)
                self.assertEqual(self.client.calls[-1][0]["steamId"], A)
                self.store.db.execute("DELETE FROM admin_kick_operations")
                self.store.db.commit()

    async def test_selection_works_with_llm_path_without_allowing_model_to_remap(self):
        await self.draft()
        model = SimpleNamespace(
            generate=AsyncMock(side_effect=AssertionError("no remapping"))
        )
        await self.handle("确认踢出2", model=model)
        self.assertEqual(self.client.calls[0][0]["steamId"], B)
        model.generate.assert_not_awaited()

    async def test_invalid_missing_and_multiple_ordinals_do_not_kick(self):
        for text in [
            "确认踢出0",
            "确认踢出6",
            "确认踢出-1",
            "确认踢出1和2",
            "确认踢出1.5",
            "确认踢出01",
        ]:
            await self.draft()
            await self.handle(text, mid=text)
        self.assertFalse(self.client.calls)

    async def test_no_draft_never_treats_ordinal_as_player_name(self):
        self.client.data["players"][0]["name"] = "1"
        await self.handle("确认踢出1")
        self.assertFalse(self.client.calls)

    async def test_expired_draft(self):
        await self.draft()
        self.store.db.execute("UPDATE admin_kick_drafts SET expires=0")
        self.store.db.commit()
        await self.handle("确认踢出1")
        self.assertFalse(self.client.calls)

    async def test_actor_bot_group_scope(self):
        await self.draft()
        for who in [
            Identity(WHO.platform, WHO.group, "other"),
            Identity(WHO.platform, "other", WHO.sender),
            Identity("official:other:app", WHO.group, WHO.sender),
        ]:
            await self.handle("确认踢出1", who=who)
        self.assertFalse(self.client.calls)

    async def test_role_checked_again(self):
        await self.draft()
        await self.handle("确认踢出1", role="member")
        self.assertFalse(self.client.calls)

    async def test_departed_player_not_substituted(self):
        await self.draft()
        self.client.data["players"] = [self.client.data["players"][1]]
        await self.handle("确认踢出1")
        self.assertFalse(self.client.calls)

    async def test_duplicate_confirmation_not_resubmitted(self):
        await self.draft()
        await self.handle("确认踢出1")
        await self.handle("确认踢出1")
        self.assertEqual(len(self.client.calls), 1)

    async def test_negative_or_quoted_selection_does_not_dispatch(self):
        for text in ["不要确认踢出1", "如果确认踢出1", "他说确认踢出1", "确认踢出1？"]:
            await self.draft()
            await self.handle(text, mid=text)
        self.assertFalse(self.client.calls)

    async def test_new_draft_replaces_ordinal_mapping(self):
        await self.draft()
        self.client.data["players"].reverse()
        await self.handle("踢出 Examples", mid="newdraft")
        await self.handle("确认踢出1")
        self.assertEqual(self.client.calls[0][0]["steamId"], B)

    async def test_restart_preserves_selection(self):
        await self.draft()
        self.actions = AdminKicks(self.store, self.client)
        await self.handle("确认踢出2")
        self.assertEqual(self.client.calls[0][0]["steamId"], B)

    async def test_bare_number_selects_stored_second_candidate(self):
        await self.draft()
        model = SimpleNamespace(
            generate=AsyncMock(side_effect=AssertionError("no LLM"))
        )
        await self.handle("2", model=model)
        self.assertEqual(self.client.calls[0][0]["steamId"], B)
        model.generate.assert_not_awaited()

    async def test_bare_number_requires_valid_owned_draft(self):
        model = SimpleNamespace(
            generate=AsyncMock(side_effect=AssertionError("no LLM"))
        )
        await self.handle("1", model=model)
        await self.draft()
        await self.handle(
            "1", who=Identity(WHO.platform, WHO.group, "other"), model=model
        )
        await self.handle("1", role="member", model=model)
        self.store.db.execute("UPDATE admin_kick_drafts SET expires=0")
        self.store.db.commit()
        await self.handle("1", model=model)
        self.assertFalse(self.client.calls)

    async def test_bare_number_duplicate_has_one_operation(self):
        await self.draft()
        await self.handle("1")
        await self.handle("1")
        self.assertEqual(len(self.client.calls), 1)

    async def test_public_query_does_not_consume_bare_selection(self):
        from wardogs.queries import PublicQueries

        model = SimpleNamespace(
            generate=AsyncMock(side_effect=AssertionError("no query LLM"))
        )
        queries = PublicQueries(self.client, None)
        result = await queries.handle(
            Message(WHO, "bare-query", time.time(), "1"), model, self.replies.append
        )
        self.assertFalse(result)
        model.generate.assert_not_awaited()

    async def test_queued_selection_cannot_rebind_to_later_draft(self):
        import asyncio

        await self.draft()
        self.client.data["players"][1]["name"] = "SecondTarget"
        started, release = asyncio.Event(), asyncio.Event()

        class Slow:
            async def generate(self, *args, **kwargs):
                started.set()
                await release.wait()
                return {"action": "kick", "target": "SecondTarget", "confidence": 0.99}

        new_request = asyncio.create_task(
            self.handle("踢出 SecondTarget", mid="new", model=Slow())
        )
        await started.wait()
        confirmation = asyncio.create_task(self.handle("1", mid="queued"))
        await asyncio.sleep(0)
        release.set()
        await new_request
        await confirmation
        self.assertFalse(self.client.calls)
        self.assertIn("重新确认", self.replies[-1])
        await self.handle("1", mid="after-new-list")
        self.assertEqual(self.client.calls[0][0]["steamId"], B)

    async def test_confirmation_before_candidate_send_completes_does_not_execute(self):
        import asyncio

        sending, release = asyncio.Event(), asyncio.Event()

        async def notify(text):
            self.replies.append(text)
            sending.set()
            await release.wait()

        request = asyncio.create_task(
            self.actions.handle(
                Message(WHO, "draft", time.time(), "踢出 Example"), "admin", notify
            )
        )
        await sending.wait()
        confirmation = asyncio.create_task(self.handle("1", mid="too-early"))
        await asyncio.sleep(0)
        release.set()
        await request
        await confirmation
        self.assertFalse(self.client.calls)

    async def test_older_received_timestamp_cannot_confirm_newer_candidate(self):
        arrived = time.time() - 2
        await self.draft()
        await self.actions.handle(
            Message(WHO, "delayed", arrived, "1"), "admin", self.replies.append
        )
        self.assertFalse(self.client.calls)

    async def test_failed_notification_never_activates_new_draft(self):
        async def fail(text):
            raise RuntimeError("synthetic_send_failure")

        with self.assertRaises(RuntimeError):
            await self.actions.handle(
                Message(WHO, "unsent", time.time(), "踢出 Example"), "admin", fail
            )
        await self.handle("1")
        self.assertFalse(self.client.calls)
