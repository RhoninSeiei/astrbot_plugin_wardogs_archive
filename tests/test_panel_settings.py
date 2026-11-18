"""Panel choices must affect execution routing, without network or real credentials."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from wardogs.model import ContextModel, provider_chain


class ModelSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_vision_and_fallback_are_separate(self):
        context = SimpleNamespace(
            llm_generate=AsyncMock(
                return_value=SimpleNamespace(completion_text='{"ok":true}')
            )
        )
        model = ContextModel(
            context,
            "fixture",
            vision_provider_id="vision-fixture",
            default_provider_id="text-fixture",
            fallback_provider_id="fallback-fixture",
        )
        await model.generate("hello", "system")
        self.assertEqual(
            context.llm_generate.await_args.kwargs["chat_provider_id"], "text-fixture"
        )
        await model.generate("image", "system", ["fixture-image"])
        self.assertEqual(
            context.llm_generate.await_args.kwargs["chat_provider_id"], "vision-fixture"
        )
        self.assertEqual(provider_chain(model), ["text-fixture", "fallback-fixture"])
        self.assertEqual(
            provider_chain(model, vision=True), ["vision-fixture", "fallback-fixture"]
        )
        self.assertIsNone(context.llm_generate.await_args.kwargs["tools"])

    async def test_empty_primary_inherits_current_session_provider(self):
        context = SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="session-fixture"),
            llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text="{}")),
        )
        model = ContextModel(
            context,
            "fixture-origin",
            default_provider_id="",
            vision_provider_id="",
            fallback_provider_id="",
        )
        await model.generate("hello", "system")
        context.get_current_chat_provider_id.assert_awaited_once_with("fixture-origin")
        self.assertEqual(
            context.llm_generate.await_args.kwargs["chat_provider_id"],
            "session-fixture",
        )
        self.assertEqual(provider_chain(model), [""])

    async def test_custom_fallback_used_and_empty_disables_it(self):
        calls = []

        async def generate(**kwargs):
            calls.append(kwargs["chat_provider_id"])
            if kwargs["chat_provider_id"] == "text-fixture":
                raise TimeoutError()
            return SimpleNamespace(completion_text='{"is_report":false}')

        context = SimpleNamespace(llm_generate=generate)
        model = ContextModel(
            context,
            "fixture",
            default_provider_id="text-fixture",
            fallback_provider_id="backup-fixture",
        )
        self.assertFalse(await model.intent("fixture"))
        self.assertEqual(calls, ["text-fixture", "backup-fixture"])
        calls.clear()
        model.fallback_provider_id = ""
        with self.assertRaises(TimeoutError):
            await model.intent("fixture")
        self.assertEqual(calls, ["text-fixture"])


class PanelSchemaTests(unittest.TestCase):
    def test_model_fields_use_native_provider_selector_and_explain_blank(self):
        schema = json.loads(
            (Path(__file__).parents[1] / "_conf_schema.json").read_text()
        )
        for key in [
            "default_provider_id",
            "assessment_provider_id",
            "fallback_provider_id",
        ]:
            self.assertEqual(schema[key]["_special"], "select_provider")
            self.assertIn("清空", schema[key]["hint"])
        self.assertEqual(schema["authorized_qq"]["default"], [])
        self.assertEqual(schema["official_qq_bindings"]["type"], "template_list")
        fields = schema["official_qq_bindings"]["templates"]["qq_member"]["items"]
        self.assertEqual(
            set(fields), {"qq", "platform_id", "app_id", "group_id", "member_id"}
        )


class ModelEntryRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_text_classification_uses_custom_text_and_fallback(self):
        from wardogs.queries import classify

        model = ContextModel(
            object(),
            "fixture",
            default_provider_id="custom-text",
            vision_provider_id="custom-vision",
            fallback_provider_id="custom-backup",
        )
        model.generate = AsyncMock(
            side_effect=[TimeoutError(), {"query": "none", "confidence": 0.99}]
        )
        await classify(model, "fixture")
        self.assertEqual(
            [c.kwargs["provider_id"] for c in model.generate.await_args_list],
            ["custom-text", "custom-backup"],
        )

    async def test_admin_text_and_screenshot_use_separate_primary_and_custom_fallback(
        self,
    ):
        from wardogs.admin_intent import interpret
        from wardogs.admin_vision import interpret_image

        model = ContextModel(
            object(),
            "fixture",
            default_provider_id="custom-text",
            vision_provider_id="custom-vision",
            fallback_provider_id="custom-backup",
        )
        model.generate = AsyncMock(
            side_effect=[
                TimeoutError(),
                {"action": "none", "target": "", "confidence": 0.99},
            ]
        )
        await interpret(model, "fixture", [])
        self.assertEqual(
            [c.kwargs["provider_id"] for c in model.generate.await_args_list],
            ["custom-text", "custom-backup"],
        )
        model.generate = AsyncMock(
            side_effect=[
                TimeoutError(),
                {"action": "kick", "targets": ["Example"], "confidence": 0.99},
            ]
        )
        media = SimpleNamespace(
            load=AsyncMock(return_value="data:image/png;base64,fixture")
        )
        await interpret_image(model, "踢", ["fixture-image"], media)
        self.assertEqual(
            [c.kwargs["provider_id"] for c in model.generate.await_args_list],
            ["custom-vision", "custom-backup"],
        )

    async def test_review_uses_configured_fallback_and_can_disable_it(self):
        from wardogs.flow import Reports

        class FakeStore:
            def __init__(self):
                self.rows = []

            def update(self, cid, **kw):
                self.rows.append(kw)

        class ReviewHarness:
            _review = Reports._review
            rules = {}
            assessment_timeout = 30

            def __init__(self):
                self.store = FakeStore()

            def _validate_review(self, d, *args):
                return d

        h = ReviewHarness()
        model = SimpleNamespace(
            provider_id="custom-vision",
            fallback_provider_id="custom-backup",
            assess=AsyncMock(side_effect=TimeoutError()),
            fallback_assess=AsyncMock(return_value={"verdict": "substantiated"}),
        )
        await h._review("case", model, {}, {}, [])
        model.fallback_assess.assert_awaited_once()
        self.assertEqual(
            h.store.rows[-1]["assessment_attempts"][-1]["provider"], "custom-backup"
        )
        model.fallback_provider_id = ""
        model.fallback_assess.reset_mock()
        with self.assertRaises(TimeoutError):
            await h._review("case", model, {}, {}, [])
        model.fallback_assess.assert_not_awaited()


class NativePanelConfigTests(unittest.TestCase):
    def test_native_schema_roundtrip_preserves_existing_settings_and_binding_cards(
        self,
    ):
        import tempfile

        from astrbot.core.config import AstrBotConfig

        schema = json.loads(
            (Path(__file__).parents[1] / "_conf_schema.json").read_text()
        )
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "settings.json"
            config = AstrBotConfig(config_path=str(file), schema=schema)
            config["assessment_provider_id"] = "vision-fixture"
            config["default_provider_id"] = "text-fixture"
            config["fallback_provider_id"] = "backup-fixture"
            config["authorized_qq"] = ["123456789"]
            binding = {
                "__template_key": "qq_member",
                "qq": "123456789",
                "platform_id": "fixture",
                "app_id": "1234567890",
                "group_id": "fixture-group",
                "member_id": "fixture-member",
            }
            config["official_qq_bindings"] = [binding]
            config.save_config()
            loaded = AstrBotConfig(config_path=str(file), schema=schema)
            self.assertEqual(loaded["default_provider_id"], "text-fixture")
            self.assertEqual(loaded["assessment_provider_id"], "vision-fixture")
            self.assertEqual(loaded["fallback_provider_id"], "backup-fixture")
            self.assertEqual(loaded["authorized_qq"], ["123456789"])
            self.assertEqual(loaded["official_qq_bindings"], [binding])

    def test_native_defaults_add_no_qq_permissions_and_keep_legacy_models(self):
        import tempfile

        from astrbot.core.config import AstrBotConfig

        schema = json.loads(
            (Path(__file__).parents[1] / "_conf_schema.json").read_text()
        )
        with tempfile.TemporaryDirectory() as directory:
            config = AstrBotConfig(
                config_path=str(Path(directory) / "settings.json"), schema=schema
            )
            self.assertEqual(config["authorized_qq"], [])
            self.assertEqual(config["official_qq_bindings"], [])
            self.assertEqual(config["default_provider_id"], "阶越星辰/step-5-preview")
            self.assertEqual(config["fallback_provider_id"], "openai_oauth/gpt-6-luna")
