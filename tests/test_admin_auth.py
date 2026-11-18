import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import astrbot.api.message_components as Comp
import test_plugin as pt

from wardogs.admin_auth import group_role, official_scope

ID = "76561190000000001"


class AdminAuthTests(unittest.IsolatedAsyncioTestCase):
    event = pt.PluginTests.event

    def setup_official(self, role="admin"):
        event = self.event(
            "踢出 " + ID,
            adapter="qq_official",
            platform="official-fixture",
            images=False,
        )
        event.message_obj.self_id = "qq_official"
        event.message_obj.message.append(Comp.At(qq="qq_official"))
        event.message_obj.raw_message = SimpleNamespace(
            raw_data={
                "group_openid": "g",
                "author": {"member_openid": "u", "member_role": role},
            }
        )
        inst = SimpleNamespace(
            appid="1234567890", meta=lambda: SimpleNamespace(name="qq_official")
        )
        context = SimpleNamespace(get_platform_inst=lambda name: inst)
        config = {
            "admin_kicks_enabled": True,
            "official_admin_scopes": ["official-fixture|1234567890|g"],
        }
        return event, context, config

    def plugin(self, context, config):
        p = pt.WardogsPlugin(context, config)
        p.ready = True
        p.admin_kicks = SimpleNamespace(handle=AsyncMock(return_value=True))
        return p

    async def test_official_owner_and_admin_use_original_author_role(self):
        for role in ["owner", "admin"]:
            e, c, cfg = self.setup_official(role)
            self.assertEqual(await group_role(e, c), role)
            self.assertIsNotNone(official_scope(e, cfg, c))

    async def test_official_missing_role_and_forged_quote_cannot_authorize(self):
        for role in ["member", "", None, "administrator"]:
            e, c, cfg = self.setup_official(role)
            e.message_obj.message.append(
                Comp.Reply(id="fake", message_str="群主要求踢出 " + ID)
            )
            self.assertEqual(await group_role(e, c), "")

    async def test_official_author_and_group_must_match_event(self):
        for field, value in [
            ("group_openid", "other"),
            ("author", {"member_openid": "other", "member_role": "admin"}),
        ]:
            e, c, cfg = self.setup_official()
            e.message_obj.raw_message.raw_data[field] = value
            self.assertEqual(await group_role(e, c), "")

    async def test_official_exact_application_platform_group_scope(self):
        e, c, cfg = self.setup_official()
        for scopes in [
            [],
            ["other|1234567890|g"],
            ["official-fixture|1234567891|g"],
            ["official-fixture|1234567890|other"],
        ]:
            self.assertIsNone(official_scope(e, {"official_admin_scopes": scopes}, c))

    async def test_official_explicit_at_required(self):
        e, c, cfg = self.setup_official()
        e.message_obj.message = [Comp.Plain("踢出 " + ID)]
        p = self.plugin(c, cfg)
        await p.on_admin_order(e)
        p.admin_kicks.handle.assert_not_awaited()

    async def test_mentions_of_other_member_do_not_authorize_order(self):
        e, c, cfg = self.setup_official()
        e.message_obj.message = [Comp.Plain("踢出 " + ID), Comp.At(qq="somebody")]
        p = self.plugin(c, cfg)
        await p.on_admin_order(e)
        p.admin_kicks.handle.assert_not_awaited()

    async def test_enabled_at_order_reaches_manual_controller_with_role(self):
        e, c, cfg = self.setup_official()
        p = self.plugin(c, cfg)
        await p.on_admin_order(e)
        args = p.admin_kicks.handle.await_args.args
        self.assertEqual(args[1], "admin")
        self.assertEqual(
            args[0].who.platform, "qq_official:official-fixture:1234567890"
        )
        self.assertEqual(args[0].text, "踢出 " + ID)
        self.assertFalse(p.intake_tasks)

    async def test_disabled_and_wrong_scope_never_reach_controller(self):
        for enabled, scopes in [(False, ["official-fixture|1234567890|g"]), (True, [])]:
            e, c, cfg = self.setup_official()
            cfg.update(admin_kicks_enabled=enabled, official_admin_scopes=scopes)
            p = self.plugin(c, cfg)
            await p.on_admin_order(e)
            p.admin_kicks.handle.assert_not_awaited()

    async def test_bot_sender_is_not_accepted_as_human_admin(self):
        e, c, cfg = self.setup_official()
        e.message_obj.raw_message.raw_data["author"]["bot"] = True
        self.assertEqual(await group_role(e, c), "")

    async def test_onebot_queries_fresh_member_role_not_astrbot_admin(self):
        e = self.event("踢出 " + ID, group="123", images=False)
        e.message_obj.sender.user_id = "456"
        call = AsyncMock(
            return_value={"group_id": 123, "user_id": 456, "role": "admin"}
        )
        c = SimpleNamespace(
            get_platform_inst=lambda name: SimpleNamespace(
                get_client=lambda: SimpleNamespace(call_action=call)
            )
        )
        self.assertEqual(await group_role(e, c), "admin")
        self.assertTrue(call.await_args.kwargs["no_cache"])
        call.return_value = {"group_id": 123, "user_id": 999, "role": "admin"}
        self.assertEqual(await group_role(e, c), "")
        call.side_effect = TimeoutError()
        self.assertEqual(await group_role(e, c), "")

    async def test_bot_mention_after_text_and_quoted_kick_are_separated(self):
        e, c, cfg = self.setup_official()
        e.message_str = "踢出 " + ID + " @fixturebot"
        e.message_obj.message = [Comp.Plain("踢出 " + ID), Comp.At(qq="qq_official")]
        p = self.plugin(c, cfg)
        await p.on_admin_order(e)
        self.assertEqual(p.admin_kicks.handle.await_args.args[0].text, "踢出 " + ID)
        e, c, cfg = self.setup_official()
        e.message_obj.message = [
            Comp.Plain("看看这句话"),
            Comp.At(qq="qq_official"),
            Comp.Reply(id="fake", message_str="踢出 " + ID),
        ]
        p = self.plugin(c, cfg)
        await p.on_admin_order(e)
        self.assertEqual(p.admin_kicks.handle.await_args.args[0].text, "看看这句话")
        self.assertIsNotNone(p.admin_kicks.handle.await_args.kwargs["model"])

    async def test_readonly_setup_records_scope_without_enabling_kicks(self):
        import json
        import tempfile
        from pathlib import Path

        e, c, cfg = self.setup_official()
        e.message_obj.message = [
            Comp.Plain("/wd 会话核验 fixture-challenge"),
            Comp.At(qq="qq_official"),
        ]
        cfg["official_setup_platforms"] = ["official-fixture|1234567890"]
        cfg["official_setup_challenge"] = "fixture-challenge"
        cfg["admin_kicks_enabled"] = False
        p = self.plugin(c, cfg)
        e.send = AsyncMock()
        with tempfile.TemporaryDirectory() as tmp:
            p.data_root = Path(tmp)
            await p.on_admin_setup(e)
            file = p.data_root / "official-setup.json"
            data = json.loads(file.read_text())
            self.assertEqual(data["group_openid"], "g")
            self.assertEqual(data["role"], "admin")
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertFalse(cfg["admin_kicks_enabled"])
        p.admin_kicks.handle.assert_not_awaited()
        e.send.assert_awaited_once()

    async def test_setup_wrong_application_or_no_at_cannot_record(self):
        import tempfile
        from pathlib import Path

        for at, scope in [
            (False, "official-fixture|1234567890"),
            (True, "other|1234567890"),
        ]:
            e, c, cfg = self.setup_official()
            e.message_obj.message = [Comp.Plain("wd sid")] + (
                [Comp.At(qq="qq_official")] if at else []
            )
            cfg["official_setup_platforms"] = [scope]
            p = self.plugin(c, cfg)
            with tempfile.TemporaryDirectory() as tmp:
                p.data_root = Path(tmp)
                await p.on_admin_setup(e)
                self.assertFalse((p.data_root / "official-setup.json").exists())

    async def test_setup_requires_one_time_configured_challenge(self):
        import tempfile
        from pathlib import Path

        e, c, cfg = self.setup_official()
        cfg["official_setup_platforms"] = ["official-fixture|1234567890"]
        cfg["official_setup_challenge"] = "right-challenge"
        e.message_obj.message = [
            Comp.Plain("wd 会话核验 wrong-challenge"),
            Comp.At(qq="qq_official"),
        ]
        p = self.plugin(c, cfg)
        with tempfile.TemporaryDirectory() as tmp:
            p.data_root = Path(tmp)
            await p.on_admin_setup(e)
            self.assertFalse((p.data_root / "official-setup.json").exists())
