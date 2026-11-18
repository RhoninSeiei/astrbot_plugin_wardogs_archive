"""Uses real AstrBot event/components in the exact-version candidate."""

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
    QQOfficialMessageEvent,
)

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "wardogs_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
pkg = importlib.util.module_from_spec(spec)
sys.modules["wardogs_plugin"] = pkg
spec.loader.exec_module(pkg)
plugin_main = __import__("wardogs_plugin.main", fromlist=["WardogsPlugin"])
WardogsPlugin = plugin_main.WardogsPlugin
ScopedFilter = plugin_main.ScopedFilter
BOT_ID = "10000001"


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def event(
        self,
        text="举报 Example",
        images=True,
        group="g",
        platform="default",
        bot=BOT_ID,
        adapter="aiocqhttp",
    ):
        m = AstrBotMessage()
        m.message_id = "m1"
        m.self_id = bot
        m.type = MessageType.GROUP_MESSAGE
        m.sender = MessageMember("u", "Test")
        m.group = Group(group_id=group)
        m.session_id = group
        m.message = [
            Comp.Plain(text),
            *([Comp.Image.fromURL("https://gchat.qpic.cn/test.png")] if images else []),
        ]
        m.message_str = text
        event_class = (
            QQOfficialMessageEvent
            if adapter == "qq_official"
            else AiocqhttpMessageEvent
        )
        e = event_class(
            text,
            m,
            PlatformMetadata(name=adapter, description="test", id=platform),
            group,
            None,
        )
        e.is_at_or_wake_command = True
        return e

    async def test_only_configured_onebot_account_instance_and_group_are_allowed(self):
        p = WardogsPlugin(types.SimpleNamespace(), {})
        p.ready = True
        p.config = {
            "platform_id": "default",
            "bot_account_id": BOT_ID,
            "allowed_groups": ["g"],
        }
        self.assertTrue(p.allowed(self.event()))
        for event in [
            self.event(platform="qq2"),
            self.event(bot="10000002"),
            self.event(bot=""),
            self.event(bot=None),
            self.event(bot=False),
            self.event(bot="not-a-number"),
            self.event(group="other"),
            self.event(group=""),
            self.event(adapter="qq_official", platform="FixtureBot"),
        ]:
            with self.subTest(event=event):
                self.assertFalse(p.allowed(event))
        for key in ["platform_id", "bot_account_id"]:
            old = p.config.pop(key)
            self.assertFalse(p.allowed(self.event()))
            p.config[key] = old
        p.config["allowed_groups"] = []
        self.assertFalse(p.allowed(self.event()))

    async def test_bot_namespace_separates_cache_and_durable_case_access(self):
        from wardogs.domain import Buffer, Message, Store

        a = WardogsPlugin.message(self.event())
        b = WardogsPlugin.message(self.event(bot="10000002"))
        self.assertNotEqual(a.who, b.who)
        buf = Buffer()
        buf.add(a)
        self.assertEqual(buf.previous(b), [])
        quote = Message(b.who, "quote", b.at, reply_to=a.id)
        self.assertIsNone(buf.quoted(quote))
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.db")
            try:
                cid = store.create(a, "report")
                self.assertIsNone(store.visible(cid, b.who, admin=True))
                self.assertNotEqual(cid, store.create(b, "other account"))
            finally:
                store.close()

    async def test_extracts_real_image_and_quoted_components(self):
        e = self.event()
        e.message_obj.message.append(
            Comp.Reply(
                id="quoted",
                chain=[Comp.Image.fromURL("https://gchat.qpic.cn/quoted.png")],
            )
        )
        m = WardogsPlugin.message(e)
        self.assertEqual(m.who.sender, "u")
        self.assertEqual(m.reply_to, "quoted")
        self.assertEqual(len(m.reply_images), 1)
        self.assertEqual(len(m.images), 1)
        self.assertTrue(ScopedFilter().filter(e, {}))
        self.assertFalse(ScopedFilter().filter(self.event(adapter="qq_official"), {}))

    async def test_message_display_name_is_optional_and_never_falls_back_to_openid(
        self,
    ):
        e = self.event()
        e.message_obj.sender.nickname = " Victim "
        self.assertEqual(WardogsPlugin.message(e).sender_nickname, "Victim")
        for name in ["", "u", "bad\nname"]:
            e.message_obj.sender.nickname = name
            self.assertEqual(WardogsPlugin.message(e).sender_nickname, "")

    async def test_initialize_accept_real_report_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = WardogsPlugin(types.SimpleNamespace(), {})
            p.config = {
                "platform_id": "default",
                "bot_account_id": BOT_ID,
                "allowed_groups": ["g"],
                "collect_seconds": 8,
                "auto_execute": False,
                "service_base": "http://127.0.0.1:18761",
            }
            p.data_root = Path(tmp)
            await p.initialize()
            sent = []
            e = self.event()

            async def send(result):
                sent.append(result)

            e.send = send
            await p.on_message(e)
            self.assertEqual(len(p.reports.pending), 1)
            self.assertTrue(sent)
            await p.terminate()
            self.assertFalse(p.reports.tasks)
            self.assertIsNone(p.client.session)

    async def test_real_group_event_primary_intent_failure_uses_luna(self):
        calls = []

        class Context:
            async def llm_generate(self, **kw):
                calls.append(kw["chat_provider_id"])
                return types.SimpleNamespace(
                    completion_text="invalid"
                    if len(calls) == 1
                    else '{"is_report":true}'
                )

        with tempfile.TemporaryDirectory() as tmp:
            p = WardogsPlugin(Context(), {})
            p.config = {
                "platform_id": "default",
                "bot_account_id": BOT_ID,
                "allowed_groups": ["g"],
            }
            p.data_root = Path(tmp)
            await p.initialize()
            e = self.event("请处理这位玩家的违规行为")

            async def send(result):
                pass

            e.send = send
            try:
                await p.on_message(e)
                self.assertEqual(
                    calls, ["阶越星辰/step-5-preview", "openai_oauth/gpt-6-luna"]
                )
                self.assertEqual(len(p.reports.pending), 1)
            finally:
                await p.terminate()

    async def test_onebot_followup_image_without_mention_is_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = WardogsPlugin(types.SimpleNamespace(), {})
            p.config = {
                "platform_id": "default",
                "bot_account_id": BOT_ID,
                "allowed_groups": ["g"],
            }
            p.data_root = Path(tmp)
            await p.initialize()

            sent = []

            async def send(result):
                sent.extend(
                    part.text for part in result.chain if isinstance(part, Comp.Plain)
                )

            try:
                e = self.event("举报 Example", images=False)
                e.send = send
                await p.on_message(e)
                follow = self.event("", images=True)
                follow.message_obj.message_id = "followup"
                follow.is_at_or_wake_command = False
                follow.send = send
                await p.on_message(follow)
                self.assertEqual(len(p.reports.pending), 1)
                case = next(iter(p.reports.pending.values()))
                self.assertEqual(len(case.urls), 1)
                self.assertNotIn("FixtureBot", "".join(sent))
            finally:
                await p.terminate()

    async def test_other_account_message_and_report_tool_are_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = WardogsPlugin(types.SimpleNamespace(), {})
            p.config = {
                "platform_id": "default",
                "bot_account_id": BOT_ID,
                "allowed_groups": ["g"],
            }
            p.data_root = Path(tmp)
            await p.initialize()
            sent = []

            async def send(result):
                sent.append(result)

            try:
                e = self.event(bot="10000002")
                e.send = send
                await p.on_message(e)
                self.assertIn("未启用", await p.report_tool(e, "举报 Example"))
                self.assertFalse(p.reports.pending)
                self.assertFalse(sent)
            finally:
                await p.terminate()

    async def test_onebot_report_tool_preserves_actual_display_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = WardogsPlugin(types.SimpleNamespace(), {})
            p.config = {
                "platform_id": "default",
                "bot_account_id": BOT_ID,
                "allowed_groups": ["g"],
            }
            p.data_root = Path(tmp)
            await p.initialize()

            async def send(result):
                pass

            try:
                e = self.event()
                e.message_obj.sender.nickname = "Victim"
                e.send = send
                self.assertIn("已受理", await p.report_tool(e, "举报 Example"))
                case = next(iter(p.reports.pending.values()))
                self.assertEqual(case.reporter_nickname, "Victim")
            finally:
                await p.terminate()

    async def test_unconfigured_group_cannot_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = WardogsPlugin(types.SimpleNamespace(), {})
            p.config = {
                "platform_id": "default",
                "bot_account_id": BOT_ID,
                "allowed_groups": [],
            }
            p.data_root = Path(tmp)
            await p.initialize()
            await p.on_message(self.event())
            self.assertFalse(p.reports.pending)
            await p.terminate()

    async def test_ordinary_member_cannot_manually_broadcast_but_can_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = WardogsPlugin(types.SimpleNamespace(), {})
            p.config = {
                "platform_id": "default",
                "bot_account_id": BOT_ID,
                "allowed_groups": ["g"],
            }
            p.data_root = Path(tmp)
            await p.initialize()
            sent = []
            e = self.event('wd 公告 "中" "en" "ja"')

            async def send(result):
                sent.append(result)

            e.send = send
            await p.on_message(e)
            self.assertEqual(
                p.store.db.execute("SELECT count(*) FROM drafts").fetchone()[0], 0
            )
            self.assertTrue(sent)
            await p.terminate()

    async def test_failed_initialize_closes_storage(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            p = WardogsPlugin(types.SimpleNamespace(), {})
            p.config = {
                "platform_id": "default",
                "bot_account_id": BOT_ID,
                "allowed_groups": ["g"],
                "collect_seconds": 0,
            }
            p.data_root = Path(tmp)
            with self.assertRaises(ValueError):
                await p.initialize()
            self.assertFalse(p.ready)
            with self.assertRaises(sqlite3.ProgrammingError):
                p.store.db.execute("SELECT 1")

    async def test_model_generated_announcement_only_creates_authorized_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = WardogsPlugin(types.SimpleNamespace(), {})
            p.config = {
                "platform_id": "default",
                "bot_account_id": BOT_ID,
                "allowed_groups": ["g"],
            }
            p.data_root = Path(tmp)
            await p.initialize()
            e = self.event("请发公告")
            sent = []

            async def send(result):
                sent.append(result)

            e.send = send
            denied = await p.announcement_tool(e, "中文", "English", "日本語")
            self.assertIn("管理员", denied)
            self.assertEqual(
                p.store.db.execute("SELECT count(*) FROM drafts").fetchone()[0], 0
            )
            e.is_admin = lambda: True
            result = await p.announcement_tool(e, "中文", "English", "日本語")
            self.assertIn("确认", result)
            self.assertEqual(
                p.store.db.execute("SELECT status FROM drafts").fetchone()[0], "draft"
            )
            self.assertIsNone(p.client.session)
            await p.terminate()


class CachePathInitialization(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_uses_astrbot_temp_directory_for_media(self):
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

        with tempfile.TemporaryDirectory() as folder:
            p = WardogsPlugin(types.SimpleNamespace(), {})
            p.data_root = Path(folder)
            await p.initialize()
            try:
                self.assertEqual(
                    p.media.cache_root, Path(get_astrbot_temp_path()).resolve()
                )
            finally:
                await p.terminate()
