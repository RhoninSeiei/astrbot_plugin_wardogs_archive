import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from test_admin import WHO, A, B, Client

from wardogs.admin import AdminKicks
from wardogs.domain import Identity, Message, Store
from wardogs.player_kills import history_request


def response(limit=20, target=A):
    now = time.time()
    return {
        "steamId": target,
        "observedAt": now,
        "timeBasis": "archive_received_at",
        "limit": limit,
        "hasMore": False,
        "complete": True,
        "coverage": {
            "scope": "retained_local_archive",
            "historyComplete": False,
            "caughtUp": True,
            "sourceObservedAt": now,
            "recordsSince": now - 86400,
        },
        "events": [
            {
                "archiveId": str(i + 1),
                "serverId": "fixture-server",
                "eventId": f"kill-{i}",
                "receivedAt": now - i,
                "killer": {"steamId": target, "name": "Example"},
                "victim": {"steamId": B if target != B else A, "name": "Victim"},
                "weapon": "Id.Item.FixtureRifle",
                "relationship": "friendly" if i == 0 else "enemy",
            }
            for i in range(limit)
        ],
    }


class PlayerKillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db")
        self.client = Client()
        self.client.player_kills = AsyncMock(
            side_effect=lambda p: response(p["limit"], p["steamId"])
        )
        self.actions = AdminKicks(self.store, self.client)
        self.replies = []
        self.number = 0

    async def asyncTearDown(self):
        self.actions.close()
        self.store.close()
        self.tmp.cleanup()

    async def handle(self, text, who=WHO, role="admin", **kwargs):
        self.number += 1
        return await self.actions.handle(
            Message(who, str(self.number), time.time(), text),
            role,
            self.replies.append,
            **kwargs,
        )

    async def test_exact_id_read_has_time_weapon_relationship_and_no_action(self):
        self.assertTrue(await self.handle("击杀记录 " + A))
        self.assertEqual(self.client.player_kills.call_args.args[0]["limit"], 20)
        text = "\n".join(self.replies)
        for word in [
            A,
            "友军",
            "敌军",
            "FixtureRifle",
            "北京时间",
            "【1】踢出",
            "【2】封禁",
            "【3】解封",
            "【4】加入哈哈名单",
        ]:
            self.assertIn(word, text)
        self.assertFalse(self.client.calls)

    async def test_50_records_are_chunked_with_date_times(self):
        await self.handle("击杀记录 " + A + " 50")
        self.assertEqual(self.client.player_kills.call_args.args[0]["limit"], 50)
        self.assertGreater(len(self.replies), 1)
        self.assertTrue(all(len(x) <= 1800 for x in self.replies))
        self.assertIn("记录50", "\n".join(self.replies))
        self.assertRegex(
            "\n".join(self.replies),
            r"20[0-9]{2}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}",
        )

    async def test_fuzzy_query_selects_identity_before_actions(self):
        await self.handle("击杀历史 Exam")
        self.client.player_kills.assert_not_awaited()
        self.assertIn("选择玩家", self.replies[-1])
        await self.handle("1")
        self.assertEqual(self.client.player_kills.call_args.args[0]["steamId"], A)
        self.assertFalse(self.client.calls)
        await self.handle("1")
        self.assertFalse(self.client.calls)
        self.assertIn("确认踢出", self.replies[-1])
        await self.handle("1")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.calls[0][0]["steamId"], A)

    async def test_member_cannot_query_or_confirm(self):
        await self.handle("击杀记录 " + A, role="member")
        self.client.player_kills.assert_not_awaited()
        await self.handle("击杀记录 " + A)
        await self.handle("1", role="member")
        self.assertFalse(self.client.calls)
        self.assertIn("管理员", self.replies[-1])

    async def test_other_sender_group_bot_cannot_navigate(self):
        await self.handle("击杀记录 " + A)
        for who in [
            Identity(WHO.platform, WHO.group, "other"),
            Identity(WHO.platform, "other", WHO.sender),
            Identity("official:fixture:other", WHO.group, WHO.sender),
        ]:
            await self.handle("1", who=who)
        self.assertFalse(self.client.calls)
        await self.handle("1")
        self.assertIn("确认踢出", self.replies[-1])

    async def test_cancel_then_number_does_not_kick(self):
        await self.handle("击杀记录 " + A)
        await self.handle("1")
        await self.handle("2")
        self.assertIn("取消", self.replies[-1])
        await self.handle("1")
        self.assertFalse(self.client.calls)

    async def test_failed_refresh_revokes_previous_navigation(self):
        await self.handle("击杀记录 " + A)
        self.client.player_kills.side_effect = TimeoutError()
        await self.handle("击杀记录 " + B)
        await self.handle("1")
        self.assertFalse(self.client.calls)
        self.assertIn("暂不可用", "\n".join(self.replies))

    async def test_bad_response_has_no_menu(self):
        for mutate in [
            lambda d: d.update(steamId=B),
            lambda d: d.update(observedAt=time.time() - 30),
            lambda d: d.update(timeBasis="guessed"),
            lambda d: d["events"][0]["killer"].update(steamId=B),
            lambda d: d["events"][0].update(relationship="guessed"),
            lambda d: d.update(complete=False),
            lambda d: d["events"].append(d["events"][0]),
        ]:
            data = response()
            mutate(data)
            self.client.player_kills.side_effect = None
            self.client.player_kills.return_value = data
            await self.handle("击杀记录 " + A)
            self.assertIn("暂不可用", self.replies[-1])
            await self.handle("1")
        self.assertFalse(self.client.calls)

    async def test_role_is_checked_again_before_action(self):
        await self.handle("击杀记录 " + A)
        await self.handle("1")
        await self.handle("1", role_check=AsyncMock(return_value="member"))
        self.assertFalse(self.client.calls)
        self.assertIn("管理员", self.replies[-1])

    async def test_new_regular_order_supersedes_navigation(self):
        await self.handle("击杀记录 " + A)
        self.client.data["players"][1]["name"] = "DifferentFixture"
        await self.handle("踢出 DifferentFixture")
        await self.handle("1")
        self.assertEqual(self.client.calls[0][0]["steamId"], B)

    async def test_read_only_public_query_does_not_select_action(self):
        await self.handle("击杀记录 " + A)
        self.assertFalse(await self.handle("服况"))
        self.assertFalse(self.client.calls)

    async def test_replaced_snapshot_cannot_accept_waiting_number(self):
        await self.handle("击杀记录 " + A)
        await self.actions.lock.acquire()
        task = asyncio.create_task(self.handle("1"))
        await asyncio.sleep(0)
        self.actions.kill_history.clear((WHO.platform, WHO.group, WHO.sender))
        self.actions.lock.release()
        await task
        self.assertFalse(self.client.calls)
        self.assertIn("更新", self.replies[-1])

    def test_parser_and_invalid_limits(self):
        self.assertEqual(history_request("击杀记录 Exam")["limit"], 20)
        self.assertEqual(history_request("/击杀历史 Exam 50")["limit"], 50)
        self.assertEqual(
            history_request("查 Example 的击杀记录 50")["query"], "Example"
        )
        for value in ["击杀记录", "击杀记录 Example 51", "击杀记录 Example 0"]:
            self.assertTrue(history_request(value)["clarify"])
        self.assertIsNone(history_request("踢出 Example"))

    async def test_unknown_and_suicide_are_not_rendered_as_enemy(self):
        data = response()
        data["events"][0]["relationship"] = "unknown"
        data["events"][1]["relationship"] = "self"
        data["events"][1]["victim"]["steamId"] = A
        self.client.player_kills.side_effect = None
        self.client.player_kills.return_value = data
        await self.handle("击杀记录 " + A)
        self.assertIn("敌友未知", "\n".join(self.replies))
        self.assertIn("自杀", "\n".join(self.replies))

    async def test_empty_history_still_has_identity_bound_menu(self):
        data = response()
        data["events"] = []
        self.client.player_kills.side_effect = None
        self.client.player_kills.return_value = data
        await self.handle("击杀记录 " + A)
        self.assertIn("不能据此认定", "\n".join(self.replies))
        await self.handle("1")
        self.assertIn(A, self.replies[-1])
        self.assertFalse(self.client.calls)

    async def test_expired_menu_never_executes(self):
        await self.handle("击杀记录 " + A)
        self.store.db.execute(
            "UPDATE admin_player_kill_navigation SET expires=?", (time.time() - 1,)
        )
        self.store.db.commit()
        await self.handle("1")
        self.assertIn("过期", self.replies[-1])
        self.assertFalse(self.client.calls)

    async def test_failed_delivery_does_not_publish_actions(self):
        async def lost(_):
            raise TimeoutError("fixture lost reply")

        with self.assertRaises(TimeoutError):
            await self.actions.handle(
                Message(WHO, "lost-send", time.time(), "击杀记录 " + A), "admin", lost
            )
        await self.handle("1")
        self.assertFalse(self.client.calls)
        self.assertIsNone(
            self.actions.kill_history.snapshot(
                (WHO.platform, WHO.group, WHO.sender), time.time()
            )
        )

    async def test_duplicate_final_confirmation_does_not_repeat(self):
        await self.handle("击杀记录 " + A)
        await self.handle("1")
        m = Message(WHO, "same-confirmation", time.time(), "1")
        await self.actions.handle(m, "admin", self.replies.append)
        await self.actions.handle(m, "admin", self.replies.append)
        self.assertEqual(len(self.client.calls), 1)

    async def test_new_query_revokes_old_kick_confirmation(self):
        await self.handle("踢出 Example")
        await self.handle("击杀记录 " + B)
        await self.handle("1")
        self.assertFalse(self.client.calls)
        self.assertIn(B, self.replies[-1])

    async def test_unban_numeric_menu_uses_only_existing_unban(self):
        self.client.admin_unban = AsyncMock(
            return_value={
                "operationId": "unban-fixture",
                "steamId": A,
                "source": "admin",
                "action": "unban",
                "status": "accepted",
                "unbanStatus": "pending",
            }
        )
        await self.handle("击杀记录 " + A)
        await self.handle("3")
        self.client.admin_unban.assert_not_awaited()
        await self.handle("1")
        self.client.admin_unban.assert_awaited_once()
        self.assertEqual(self.client.admin_unban.call_args.args[0]["steamId"], A)
        self.assertFalse(self.client.calls)

    async def test_haha_numeric_menu_uses_only_existing_cycle_policy(self):
        self.client.admin_cycle_add = AsyncMock(
            return_value={
                "operationId": "cycle-fixture",
                "steamId": A,
                "source": "admin",
                "action": "cycle_add",
                "status": "completed",
                "cycleStatus": "registered",
                "registeredAt": time.time(),
                "alreadyEnabled": False,
                "onlineAtCheck": True,
            }
        )
        await self.handle("击杀记录 " + A)
        await self.handle("4")
        self.assertIn("持续循环惩罚名单", self.replies[-1])
        self.client.admin_cycle_add.assert_not_awaited()
        await self.handle("1")
        self.client.admin_cycle_add.assert_awaited_once()
        self.assertFalse(self.client.calls)

    async def test_offline_kick_does_not_silently_ban(self):
        self.client.data["players"] = []
        self.client.admin_player_search = AsyncMock(
            return_value={
                "observedAt": time.time(),
                "players": [
                    {
                        "steamId": A,
                        "name": "Example",
                        "online": False,
                        "aliases": [],
                        "source": "history",
                        "lastSeen": time.time() - 100,
                    }
                ],
                "truncated": False,
                "historyComplete": True,
            }
        )
        self.client.admin_restrict = AsyncMock()
        await self.handle("击杀记录 " + A)
        await self.handle("1")
        await self.handle("1")
        self.assertFalse(self.client.calls)
        self.client.admin_restrict.assert_not_awaited()
        self.assertIn("离线", self.replies[-1])

    async def test_source_coverage_and_game_time_are_validated(self):
        for mutate in [
            lambda d: d["coverage"].update(caughtUp=False),
            lambda d: d["coverage"].update(sourceObservedAt=time.time() - 16),
            lambda d: d["coverage"].update(historyComplete=True),
            lambda d: d.update(limit=True),
            lambda d: d["events"][0].update(gameTimeSeconds=-1),
        ]:
            data = response()
            mutate(data)
            self.client.player_kills.side_effect = None
            self.client.player_kills.return_value = data
            await self.handle("击杀记录 " + A)
            self.assertIn("暂不可用", self.replies[-1])
        data = response()
        data["events"][0]["gameTimeSeconds"] = 1200.5
        self.client.player_kills.return_value = data
        await self.handle("击杀记录 " + A)
        self.assertIn("游戏内计时1200.5秒", "\n".join(self.replies))


class ShortHistoryAliasTests(unittest.TestCase):
    def test_compact_alias_matches_spaced_query(self):
        for query in ["KAIKAI", "打灰大哥哥", A, "KAIKAI 50", "打灰 50"]:
            self.assertEqual(
                history_request("查" + query), history_request("查 " + query)
            )
        self.assertEqual(history_request("/查KAIKAI！")["query"], "KAIKAI")
        self.assertTrue(history_request("查KAIKAI 51")["clarify"])

    def test_compact_alias_keeps_existing_reserved_routes(self):
        for text in [
            "查人数",
            "查比分",
            "查服况",
            "查服务器id",
            "查服id",
            "查过去24小时封禁人数",
            "查封禁原因 Example",
            "查处罚记录Example",
            "查封禁记录Example",
            "查询菜单",
            "查询帮助",
        ]:
            self.assertIsNone(history_request(text), text)

    def test_short_name_and_precise_id_default_to_twenty(self):
        for query in ["打灰大哥哥", A]:
            self.assertEqual(
                history_request("查 " + query),
                {"query": query, "limit": 20, "clarify": False},
            )

    def test_short_query_supports_fifty_and_slash(self):
        self.assertEqual(
            history_request("/查 打灰 50"),
            {"query": "打灰", "limit": 50, "clarify": False},
        )
        self.assertTrue(history_request("查 打灰 51")["clarify"])
        self.assertTrue(history_request("查")["clarify"])

    def test_existing_public_and_management_queries_keep_their_routes(self):
        for text in [
            "查 人数",
            "查 比分",
            "查 服况",
            "查 服务器id",
            "查封禁原因 Example",
            "查询菜单",
            "查询帮助",
        ]:
            self.assertIsNone(history_request(text), text)
