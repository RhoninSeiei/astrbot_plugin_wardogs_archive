import unittest

import test_official_menu as fixture
import test_plugin as pt

from wardogs.batch_restrictions import RestrictionOffer


class BulkOptionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.MenuTests.asyncSetUp
    asyncTearDown = fixture.MenuTests.asyncTearDown

    async def test_confirm_and_cancel_carry_batch_binding_and_admin_permission(self):
        self.event.message_obj.raw_message.raw_data["author"]["member_role"] = "admin"
        text = RestrictionOffer("离线补封候选，仅登记永久重入限制", "123abc456def")
        result = await pt.plugin_main.send_restriction_offer(
            self.event, self.context, self.config, text
        )
        self.assertTrue(result)
        body = self.api.request.call_args.kwargs["json"]
        buttons = body["keyboard"]["content"]["rows"][0]["buttons"]
        self.assertEqual(
            [b["action"]["data"] for b in buttons],
            ["确认补封全部 123abc456def", "取消批量补封 123abc456def"],
        )
        self.assertTrue(all(b["action"]["permission"]["type"] == 1 for b in buttons))
        self.plugin.admin_kicks.handle.assert_not_awaited()

    async def test_plain_text_or_member_cannot_create_restriction_buttons(self):
        self.assertFalse(
            await pt.plugin_main.send_restriction_offer(
                self.event, self.context, self.config, "ordinary"
            )
        )
        with self.assertRaises(PermissionError):
            await pt.plugin_main.send_restriction_offer(
                self.event,
                self.context,
                self.config,
                RestrictionOffer("text", "123abc456def"),
            )
        self.api.request.assert_not_awaited()

    async def test_explicit_rejection_can_fall_back_but_unknown_must_not(self):
        self.event.message_obj.raw_message.raw_data["author"]["member_role"] = "admin"
        error = pt.plugin_main.send_restriction_offer.__globals__["JoinError"]
        text = RestrictionOffer("text", "123abc456def")
        self.api.request.side_effect = [error(400, "denied"), {"id": "text-confirmed"}]
        self.assertTrue(
            await pt.plugin_main.send_restriction_offer(
                self.event, self.context, self.config, text
            )
        )
        self.assertEqual(self.api.request.call_args.kwargs["json"]["msg_type"], 0)
        self.api.request.side_effect = error(0, "transport_error")
        with self.assertRaises(error):
            await pt.plugin_main.send_restriction_offer(
                self.event, self.context, self.config, text
            )

    async def test_disabled_keyboard_still_requires_confirmed_text_delivery(self):
        self.event.message_obj.raw_message.raw_data["author"]["member_role"] = "admin"
        self.config["official_menu_buttons_enabled"] = False
        offer = RestrictionOffer("permanent restriction", "123abc456def")
        self.assertTrue(
            await pt.plugin_main.send_restriction_offer(
                self.event, self.context, self.config, offer
            )
        )
        self.api.request.assert_awaited_once()
        self.assertEqual(self.api.request.call_args.kwargs["json"]["msg_type"], 0)
