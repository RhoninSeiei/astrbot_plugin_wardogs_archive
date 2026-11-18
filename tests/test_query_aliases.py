"""Player phrasing and query boundaries, independently specified examples."""

import unittest

import test_public_queries as fixture


class LocalQueryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PublicQueryTests.asyncSetUp
    asyncTearDown = fixture.PublicQueryTests.asyncTearDown
    handle = fixture.PublicQueryTests.handle
    answer = fixture.PublicQueryTests.answer

    async def test_population_shortcuts_and_colloquial_requests_skip_model(self):
        for text in [
            "人数",
            "多少人",
            "几个人",
            "服里几个人",
            "服务器人数",
            "在线人数是多少",
            "服务器现在有多少人在线",
            "还有位置吗",
            "有坑吗",
            "满员没",
            "服里现在多少人",
            "服务器人多不多",
            "在线人树",
            "在现人数",
            "在线人输",
            "还有坑位没",
            "满人没",
            "服满了吗，还有空位吗",
            "人數",
            "/wd 人数",
        ]:
            with self.subTest(text=text):
                self.assertTrue(await self.handle(text, text))
                self.assertIn("31 / 64", self.replies[-1])
                self.assertNotIn("对局比分", self.replies[-1])
        self.context.llm_generate.assert_not_awaited()
        self.client.admin_kick.assert_not_awaited()

    async def test_score_phrases_and_curated_typos_skip_model(self):
        for text in [
            "比分",
            "查比分",
            "比份",
            "比芬",
            "得分怎么样了",
            "比分是多少",
            "比分几比几",
            "现在多少分",
            "红蓝绿多少分",
            "票数",
            "票數",
            "几比几",
            "打几比几了",
            "谁领先",
            "戰況",
            "看看战况",
            "ＢＦ",
            "bf",
            "/wd 比分",
        ]:
            with self.subTest(text=text):
                self.assertTrue(await self.handle(text, text))
                self.assertIn("红方 120", self.replies[-1])
                self.assertNotIn("在线人数", self.replies[-1])
        self.context.llm_generate.assert_not_awaited()

    async def test_status_and_combined_queries_skip_model(self):
        for text in [
            "状况",
            "状态",
            "服务器状况",
            "服务器状况怎么样了",
            "服务器情况",
            "服况",
            "服咋样了",
            "服务器咋样",
            "服务器咋样了",
            "fwqzt",
            "状太",
            "服务器装态",
            "比分和人数",
            "人数，比分",
            "人数多少，比分多少",
            "比分＋在线人数",
            "查人数顺便看看比分",
            "在线人数及战况",
            "请查一下服务器现在人数和比分，谢谢",
        ]:
            with self.subTest(text=text):
                self.assertTrue(await self.handle(text, text))
                self.assertIn("31 / 64", self.replies[-1])
                self.assertIn("红方 120", self.replies[-1])
        self.context.llm_generate.assert_not_awaited()

    async def test_punctuation_spacing_politeness_and_traditional_text(self):
        for text in [
            "请问，現在伺服器有幾個人？",
            "麻烦看看，比 分？谢谢",
            "幫忙查下 服務器狀態！",
            "？比分？？",
            "查一下人数呗",
            "现在比分呢～",
        ]:
            with self.subTest(text=text):
                self.assertTrue(await self.handle(text, text))
                self.assertNotIn("理解暂不可用", self.replies[-1])
        self.context.llm_generate.assert_not_awaited()

    async def test_local_help_is_available_with_no_model_or_backend(self):
        for text in [
            "帮助",
            "查询帮助",
            "怎么查比分",
            "请问怎么查人数",
            "菜单",
            "指令",
            "help",
            "/wd 帮助",
        ]:
            self.assertTrue(await self.handle(text, text))
            for example in ["人数", "比分", "状况"]:
                self.assertIn(example, self.replies[-1])
        self.context.llm_generate.assert_not_awaited()
        self.client.match.assert_not_awaited()
        self.client.state.assert_not_awaited()

    async def test_negation_quotes_history_groups_and_mutations_are_not_queries(self):
        for text in [
            "别查比分",
            "別查比份",
            "他說比份",
            "群裏幾個人",
            "不用查人数了",
            "他说比分",
            "转发：人数",
            "“比分”",
            "昨天比分",
            "没问比分",
            "不看比分",
            "不需要查人数",
            "不是要查人数",
            "管理员说查人数",
            "刚才管理员说查人数",
            "有人在问比分",
            "群在线人数",
            "微信群多少人",
            "上周比分",
            "上轮比分",
            "服务器上周的比分",
            "比分然后禁言Example",
            "查比分并发布通知",
            "上局多少分",
            "群里多少人",
            "QQ群人数",
            "比分并发公告",
            "人数并重启服务器",
            "查询玩家Example",
            "比分和封禁Example",
            "比分是多少，踢Example",
            "撤回比分",
            "怎么改比分",
            "设置人数64",
            "足球比分",
            "英超比分",
        ]:
            with self.subTest(text=text):
                self.assertFalse(await self.handle(text, text))
        self.assertFalse(self.replies)
        self.context.llm_generate.assert_not_awaited()
        self.client.match.assert_not_awaited()
        self.client.state.assert_not_awaited()

    async def test_unknown_current_game_expression_keeps_model_fallback(self):
        self.answer("both")
        self.assertTrue(await self.handle("请告知此刻战场的兵力规模以及各阵营交锋结果"))
        self.context.llm_generate.assert_awaited_once()
        self.assertIn("红方 120", self.replies[-1])

    async def test_duplicate_local_query_does_not_repeat_backend_or_reply(self):
        await self.handle("比分")
        await self.handle("比分")
        self.context.llm_generate.assert_not_awaited()
        self.client.match.assert_awaited_once()
        self.assertEqual(len(self.replies), 1)

    async def test_known_command_works_when_model_is_offline(self):
        self.context.llm_generate.side_effect = AssertionError("LLM forbidden")
        self.assertTrue(await self.handle("得分怎么样了"))
        self.context.llm_generate.assert_not_awaited()
        self.assertIn("红方 120", self.replies[-1])

    async def test_normal_words_containing_bie_can_still_use_model(self):
        for text in [
            "分别报一下人数和比分",
            "人数和比分分别多少",
            "帮忙分别看看人数和比分",
            "特别想知道比分",
        ]:
            self.assertTrue(await self.handle(text, text))
            self.assertIn("红方 120", self.replies[-1])
        self.assertEqual(self.context.llm_generate.await_count, 4)
