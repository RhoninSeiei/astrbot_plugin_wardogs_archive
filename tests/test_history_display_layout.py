"""Record frames and fixed navigation labels cannot form Markdown lists."""

import time
import unittest

import test_player_kills as fixture
from test_admin import WHO, A

from wardogs.domain import Message
from wardogs.player_kills import render


class HistoryLayoutTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PlayerKillTests.asyncSetUp
    asyncTearDown = fixture.PlayerKillTests.asyncTearDown
    handle = fixture.PlayerKillTests.handle

    def test_records_are_framed_and_menu_is_a_separate_message(self):
        chunks = render(fixture.response(50), "模拟玩家")
        records = "\n".join(chunks[:-1])
        self.assertEqual(records.count("┌─ 记录"), 50)
        self.assertEqual(records.count("└"), 50)
        self.assertIn("记录50", records)
        for chunk in chunks[:-1]:
            self.assertEqual(chunk.count("┌─ 记录"), chunk.count("└"))
            self.assertNotIn("【1】踢出", chunk)
            self.assertLessEqual(len(chunk), 1800)
        menu = chunks[-1]
        self.assertIn("操作对象：模拟玩家", menu)
        self.assertIn(A, menu)
        for value in ["【1】踢出", "【2】封禁", "【3】解封", "【4】加入哈哈名单"]:
            self.assertIn(value, menu)
        self.assertNotIn("记录50", menu)
        self.assertNotRegex("\n".join(chunks), r"(?m)^\d+\.\s")

    async def test_numeric_input_still_selects_action_and_requires_confirmation(self):
        await self.handle("查 " + A + " 50")
        self.assertIn("【1】踢出", self.replies[-1])
        await self.handle("1")
        self.assertIn("【1】确认踢出", self.replies[-1])
        self.assertFalse(self.client.calls)
        await self.handle("1")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.calls[0][0]["steamId"], A)

    async def test_failed_menu_send_never_publishes_actions(self):
        async def fail_menu(text):
            self.replies.append(text)
            if "操作对象：" in text:
                raise TimeoutError("synthetic menu send failure")

        with self.assertRaises(TimeoutError):
            await self.actions.handle(
                Message(WHO, "lost-menu", time.time(), "查 " + A), "admin", fail_menu
            )
        await self.handle("1")
        self.assertFalse(self.client.calls)
        self.assertIn("记录01", "\n".join(self.replies))

    async def test_candidate_numbers_do_not_use_markdown_list_syntax(self):
        await self.handle("查 Exam")
        self.assertIn("【1】", self.replies[-1])
        self.assertIn("【2】", self.replies[-1])
        self.assertNotRegex(self.replies[-1], r"(?m)^\d+\.\s")
