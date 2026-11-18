import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_admin import WHO, A, B, Client

from wardogs.admin import AdminKicks
from wardogs.domain import Identity, Message, Store


def payload(count=12):
    now = time.time()
    return {
        "observedAt": now,
        "windowStart": now - 60,
        "windowEnd": now,
        "timeBasis": "archive_received_at",
        "complete": True,
        "events": [
            {
                "archiveId": str(i + 1),
                "serverId": "server-fixture",
                "eventId": f"event-{i}",
                "receivedAt": now - i * (50 / max(count, 1)),
                "killer": {"steamId": str(76561190000000100 + i), "name": f"击杀者{i}"},
                "weapon": "Id.Item.FixtureRifle",
                "victim": {"steamId": B, "name": f"受害者{i}"},
            }
            for i in range(count)
        ],
    }


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db")
        self.client = Client()
        self.result = payload()
        self.client.players = AsyncMock(side_effect=lambda: self.client.data)
        self.client.recent_kills = AsyncMock(side_effect=lambda _: self.result)
        self.client.data["players"] += [
            dict(e["killer"]) for e in self.result["events"]
        ]
        self.actions = AdminKicks(self.store, self.client)
        self.replies = []

    async def asyncTearDown(self):
        self.actions.close()
        self.store.close()
        self.tmp.cleanup()

    async def handle(
        self, text="回溯", mid="query", who=WHO, role="admin", notify=None
    ):
        return await self.actions.handle(
            Message(who, mid, time.time(), text), role, notify or self.replies.append
        )

    async def test_lists_all_records_and_selects_killer_beyond_five(self):
        self.assertTrue(await self.handle())
        text = "\n".join(self.replies)
        for i in range(12):
            self.assertIn(f"{i + 1}. ", text)
            self.assertIn(f"击杀者{i}", text)
            self.assertIn(f"受害者{i}", text)
        self.assertIn("FixtureRifle", text)
        self.assertIn("接收", text)
        self.assertFalse(self.client.calls)
        await self.handle("10", mid="confirm")
        self.assertEqual(
            self.client.calls[0][0]["steamId"],
            self.result["events"][9]["killer"]["steamId"],
        )
        self.assertNotEqual(self.client.calls[0][0]["steamId"], B)

    async def test_member_cannot_read_or_select(self):
        self.assertTrue(await self.handle(role="member"))
        self.client.recent_kills.assert_not_awaited()
        self.assertIn("管理员", self.replies[-1])
        await self.handle()
        await self.handle("1", mid="member-confirm", role="member")
        self.assertFalse(self.client.calls)

    async def test_snapshot_stays_fixed_when_query_result_changes(self):
        await self.handle()
        target = self.result["events"][0]["killer"]["steamId"]
        self.result["events"].reverse()
        await self.handle("1", mid="confirm")
        self.assertEqual(self.client.calls[0][0]["steamId"], target)

    async def test_unknown_killer_visible_but_not_selectable(self):
        self.result["events"][0]["killer"] = {"steamId": None, "name": None}
        self.result["events"][0]["weapon"] = None
        await self.handle()
        self.assertIn("不可选", "".join(self.replies))
        await self.handle("1", mid="confirm")
        self.assertFalse(self.client.calls)
        self.assertIn("ID", self.replies[-1])

    async def test_failure_or_empty_invalidates_previous_normal_candidate(self):
        await self.handle("踢出 Example", mid="old")
        self.result["events"] = []
        await self.handle()
        self.assertIn("没有", self.replies[-1])
        await self.handle("1", mid="confirm")
        self.assertFalse(self.client.calls)

    async def test_invalid_or_incomplete_payload_does_not_offer_targets(self):
        original = payload()
        for i, changes in enumerate(
            [
                {"complete": False},
                {"timeBasis": "game_guess"},
                {"observedAt": time.time() - 100},
                {"windowStart": 0},
                {"events": [original["events"][0]] * 2},
            ]
        ):
            self.result = dict(original, **changes)
            await self.handle(mid=f"invalid-{i}")
            self.assertIn("不可用", self.replies[-1])
            await self.handle("1", mid=f"confirm-{i}")
        self.assertFalse(self.client.calls)

    async def test_ordinary_new_order_replaces_replay(self):
        await self.handle()
        await self.handle("踢出 Example", mid="new")
        await self.handle("1", mid="confirm")
        self.assertEqual(self.client.calls[0][0]["steamId"], A)

    async def test_replay_failure_replaces_previous_replay(self):
        await self.handle()
        self.client.recent_kills.side_effect = TimeoutError()
        await self.handle(mid="new")
        self.assertIn("不可用", self.replies[-1])
        await self.handle("1", mid="confirm")
        self.assertFalse(self.client.calls)

    async def test_group_sender_and_bot_scopes_are_preserved(self):
        await self.handle()
        for i, who in enumerate(
            [
                Identity(WHO.platform, WHO.group, "other"),
                Identity(WHO.platform, "other", WHO.sender),
                Identity("other:bot", WHO.group, WHO.sender),
            ]
        ):
            await self.handle("1", mid=f"confirm-{i}", who=who)
        self.assertFalse(self.client.calls)

    async def test_duplicate_command_does_not_refresh_mapping_or_fetch(self):
        await self.handle()
        self.result["events"].reverse()
        await self.handle()
        self.client.recent_kills.assert_awaited_once()
        await self.handle("1", mid="confirm")
        self.assertEqual(self.client.calls[0][0]["steamId"], "76561190000000100")

    async def test_failed_list_send_cannot_activate_mapping(self):
        async def fail(_):
            raise RuntimeError("synthetic_send_failure")

        with self.assertRaises(RuntimeError):
            await self.handle(notify=fail)
        await self.handle("1", mid="confirm")
        self.assertFalse(self.client.calls)

    async def test_query_ack_pending_does_not_accept_ordinal(self):
        sending, release = asyncio.Event(), asyncio.Event()

        async def notify(text):
            sending.set()
            await release.wait()
            self.replies.append(text)

        task = asyncio.create_task(self.handle(notify=notify))
        await asyncio.wait_for(sending.wait(), 1)
        confirm = asyncio.create_task(self.handle("1", mid="early"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(task, confirm)
        self.assertFalse(self.client.calls)
        await self.handle("1", mid="after")
        self.assertEqual(self.client.calls[0][0]["steamId"], "76561190000000100")

    async def test_public_query_lets_replay_through_without_llm(self):
        from wardogs.queries import PublicQueries

        model = SimpleNamespace(
            generate=AsyncMock(side_effect=AssertionError("LLM forbidden"))
        )
        queries = PublicQueries(self.client, None)
        self.assertFalse(
            await queries.handle(
                Message(WHO, "q", time.time(), "回溯"), model, self.replies.append
            )
        )
        model.generate.assert_not_awaited()

    async def test_expired_replay_cannot_be_confirmed(self):
        await self.handle()
        self.store.db.execute("UPDATE admin_replay_drafts SET expires=0")
        self.store.db.commit()
        await self.handle("1", mid="expired")
        self.assertIn("过期", self.replies[-1])
        self.assertFalse(self.client.calls)

    async def test_restart_restores_exact_event_mapping(self):
        await self.handle()
        self.actions.close()
        self.actions = AdminKicks(self.store, self.client)
        await self.handle("1", mid="after-restart")
        self.assertEqual(self.client.calls[0][0]["steamId"], "76561190000000100")

    async def test_large_list_supports_hundredth_killer_and_bounded_messages(self):
        self.result = payload(200)
        self.client.data["players"] = [dict(e["killer"]) for e in self.result["events"]]
        await self.handle()
        self.assertIn("200. ", "\n".join(self.replies))
        self.assertTrue(all(len(s) <= 1700 for s in self.replies))
        await self.handle("确认踢出100", mid="hundred")
        self.assertEqual(
            self.client.calls[0][0]["steamId"],
            self.result["events"][99]["killer"]["steamId"],
        )

    async def test_offline_killer_requires_second_restriction_confirmation(self):
        import test_admin_history as h

        self.actions.close()
        self.client = h.HistoryClient()
        self.client.recent_kills = AsyncMock(return_value=payload(1))
        self.client.recent_kills.return_value["events"][0]["killer"] = {
            "steamId": h.H,
            "name": "DepartedFixture",
        }
        self.actions = AdminKicks(self.store, self.client)
        await self.handle()
        await self.handle("1", mid="choose")
        self.assertIn("离线", self.replies[-1])
        self.assertFalse(self.client.calls)
        self.assertFalse(self.client.restrictions)
        await self.handle("1", mid="confirm-offline")
        self.assertEqual(self.client.restrictions[0][0]["steamId"], h.H)
        self.assertFalse(self.client.calls)

    async def test_duplicate_older_query_does_not_replace_newer_mapping(self):
        await self.handle(mid="old")
        self.result["events"].reverse()
        await self.handle(mid="new")
        await self.handle(mid="old")
        self.assertEqual(self.client.recent_kills.await_count, 2)
        await self.handle("1", mid="confirm")
        self.assertEqual(self.client.calls[0][0]["steamId"], "76561190000000111")

    async def test_recent_kills_client_uses_admin_token_and_fixed_read_endpoint(self):
        from aiohttp import web

        from wardogs.io import ServiceClient

        calls = []

        async def handler(request):
            calls.append(
                (
                    request.method,
                    request.path,
                    request.headers.get("Authorization"),
                    await request.json(),
                )
            )
            return web.json_response(self.result)

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        root = Path(self.tmp.name)
        query, admin = root / "query", root / "admin"
        query.write_text("fake-query")
        admin.write_text("fake-admin")
        client = ServiceClient(
            "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1]),
            query,
            admin_key_file=admin,
        )
        body = {"actor": {"role": "admin"}, "requestedAt": time.time()}
        try:
            await client.recent_kills(body)
            self.assertEqual(
                calls,
                [("POST", "/v1/moderation/recent-kills", "Bearer fake-admin", body)],
            )
        finally:
            await client.close()
            await runner.cleanup()

    async def test_message_received_before_list_publication_cannot_select_it(self):
        before = time.time()
        await self.handle()
        await self.actions.handle(
            Message(WHO, "early-role-delayed", before, "1"),
            "admin",
            self.replies.append,
        )
        self.assertFalse(self.client.calls)
        self.assertIn("就绪", self.replies[-1])

    async def test_full_minute_with_long_names_fits_bounded_http_response(self):
        self.result = payload(300)
        for event in self.result["events"]:
            event["killer"]["name"] = "火" * 128
            event["victim"]["name"] = "星" * 128
            event["weapon"] = "R" * 256
        await self.test_recent_kills_client_uses_admin_token_and_fixed_read_endpoint()
