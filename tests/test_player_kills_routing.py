import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import astrbot.api.message_components as Comp
import test_admin_auth as auth
import test_plugin as pt
from test_admin import Client
from test_player_kills import response

from wardogs.admin import AdminKicks
from wardogs.domain import Store
from wardogs.history import LocalHistory
from wardogs.queries import PublicQueries

A = "76561190000000001"


class KillRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db")
        _, context, config = auth.AdminAuthTests().setup_official()
        config.update(
            public_queries_enabled=True,
            official_query_scopes=list(config["official_admin_scopes"]),
            official_only=True,
        )
        self.client = Client()
        self.client.player_kills = AsyncMock(side_effect=lambda p: response(p["limit"]))
        self.plugin = pt.WardogsPlugin(context, config)
        self.plugin.ready = True
        self.plugin.admin_kicks = AdminKicks(self.store, self.client)
        self.plugin.public_queries = PublicQueries(
            self.client, LocalHistory(self.store)
        )
        self.model = SimpleNamespace(
            generate=AsyncMock(side_effect=AssertionError("UNEXPECTED_LLM"))
        )
        self.plugin.assessor = lambda _: self.model
        self.index = 0

    async def asyncTearDown(self):
        self.plugin.admin_kicks.close()
        self.store.close()
        self.tmp.cleanup()

    async def event(self, text, role="admin", mention=True):
        self.index += 1
        event, _, _ = auth.AdminAuthTests().setup_official(role)
        event.message_obj.message_id = str(self.index)
        event.message_obj.message = [Comp.Plain(text)] + (
            [Comp.At(qq="qq_official")] if mention else []
        )
        event.send = AsyncMock()
        await self.plugin.on_public_query(event)
        self.assertFalse(event.is_stopped())
        await self.plugin.on_admin_order(event)
        return event

    async def test_actual_official_handler_chain_query_and_numbers_skip_public_model(
        self,
    ):
        event = await self.event("击杀记录 " + A + " 50")
        self.assertTrue(event.is_stopped())
        self.client.player_kills.assert_awaited_once()
        self.assertIn(
            "记录50",
            "\n".join(x.args[0].chain[0].text for x in event.send.call_args_list),
        )
        await self.event("1")
        self.assertFalse(self.client.calls)
        await self.event("1")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.calls[0][0]["steamId"], A)
        self.model.generate.assert_not_awaited()

    async def test_cancel_does_not_get_captured_by_public_query_model(self):
        await self.event("击杀记录 " + A)
        event = await self.event("取消")
        self.assertIn("取消", event.send.call_args.args[0].chain[0].text)
        self.model.generate.assert_not_awaited()
        self.assertFalse(self.client.calls)

    async def test_no_at_or_member_never_reads_personal_history(self):
        await self.event("击杀记录 " + A, mention=False)
        self.client.player_kills.assert_not_awaited()
        event = await self.event("击杀记录 " + A, role="member")
        self.client.player_kills.assert_not_awaited()
        self.assertIn("管理员", event.send.call_args.args[0].chain[0].text)
        self.model.generate.assert_not_awaited()
