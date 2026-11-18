"""Explicit QQ grants remain bound to the original official identity and scope."""

import unittest
from unittest.mock import AsyncMock

import test_admin_auth as fixture

from wardogs.admin_auth import management_authorization, management_role

QQ = "123456789"


class ConfiguredQQTests(unittest.IsolatedAsyncioTestCase):
    def setup(self):
        event, context, config = fixture.AdminAuthTests().setup_official("member")
        binding = {
            "__template_key": "qq_member",
            "qq": QQ,
            "platform_id": "official-fixture",
            "app_id": "1234567890",
            "group_id": "g",
            "member_id": "u",
        }
        config.update(authorized_qq=[QQ], official_qq_bindings=[binding])
        return event, context, config, binding

    async def test_explicit_binding_grants_effective_role_without_rewriting_platform_role(
        self,
    ):
        event, context, config, _ = self.setup()
        proof = await management_authorization(event, context, config)
        self.assertEqual(
            proof,
            {
                "role": "admin",
                "source": "configured_qq",
                "qq": QQ,
                "platform_role": "member",
            },
        )
        self.assertEqual(
            event.message_obj.raw_message.raw_data["author"]["member_role"], "member"
        )

    async def test_qq_alone_and_unbound_member_never_authorize(self):
        event, context, config, _ = self.setup()
        config["official_qq_bindings"] = []
        event.message_obj.raw_message.raw_data["author"]["qq"] = QQ
        self.assertEqual(await management_role(event, context, config), "")

    async def test_each_scope_and_identity_field_is_required(self):
        for key in ["qq", "platform_id", "app_id", "group_id", "member_id"]:
            event, context, config, binding = self.setup()
            binding[key] = "other"
            self.assertEqual(await management_role(event, context, config), "", key)

    async def test_revocation_or_duplicate_identity_denies(self):
        event, context, config, binding = self.setup()
        self.assertEqual(await management_role(event, context, config), "admin")
        config["authorized_qq"] = []
        self.assertEqual(await management_role(event, context, config), "")
        config["authorized_qq"] = [QQ]
        config["official_qq_bindings"].append(dict(binding, member_id="different"))
        self.assertEqual(await management_role(event, context, config), "")

    async def test_forged_raw_author_or_bot_cannot_use_binding(self):
        for change in [{"member_openid": "other"}, {"bot": True}]:
            event, context, config, _ = self.setup()
            event.message_obj.raw_message.raw_data["author"].update(change)
            self.assertEqual(await management_role(event, context, config), "")

    async def test_original_platform_admin_does_not_need_allowlist(self):
        event, context, config = fixture.AdminAuthTests().setup_official("owner")
        self.assertEqual(await management_role(event, context, config), "owner")

    async def test_delegated_group_event_and_live_recheck(self):
        event, context, config, _ = self.setup()
        plugin = fixture.AdminAuthTests().plugin(context, config)
        # Capture audit separately; no real database or external service.
        plugin.audit_management_authorization = AsyncMock()
        await plugin.on_admin_order(event)
        self.assertEqual(plugin.admin_kicks.handle.await_args.args[1], "admin")
        callback = plugin.admin_kicks.handle.await_args.kwargs["role_check"]
        config["authorized_qq"] = []
        self.assertEqual(await callback(), "")

    async def test_binding_does_not_open_an_unconfigured_management_group(self):
        event, context, config, _ = self.setup()
        config["official_admin_scopes"] = []
        self.assertEqual(await management_role(event, context, config), "")
