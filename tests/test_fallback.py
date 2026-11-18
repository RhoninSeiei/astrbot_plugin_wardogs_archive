import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_flow import Media, Service
from test_views import picture

from test_domain import ID, combat_fixture, decision
from wardogs.domain import Identity, Message, Store
from wardogs.flow import Reports
from wardogs.model import ContextModel

LUNA = "openai_oauth/gpt-6-luna"


class Router:
    provider_id = "阶越星辰/step-5-preview"

    def __init__(self, primary=None, secondary=None):
        self.primary, self.secondary = primary, secondary
        self.calls = []

    async def respond(self, name, response, case, images, players, rules):
        self.calls.append((name, case, images, players, rules))
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return await response(case, images, players, rules)
        ref = next(iter(images))
        payload = dict(
            combat_evidence=combat_fixture(ref),
            observed_at=time.time(),
            evidence_refs=[ref],
            identity_evidence=[ref],
        )
        payload.update(response or {})
        return decision(**payload)

    async def assess(self, *args):
        return await self.respond("step", self.primary, *args)

    async def fallback_assess(self, *args):
        return await self.respond("luna", self.secondary, *args)


class FallbackFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.db")
        self.service = Service()
        self.replies = []
        self.reports = Reports(
            self.store,
            None,
            self.service,
            Media(),
            rules={"outside_combat": {"version": "1", "text": "rule"}},
            auto_execute=True,
        )

    async def asyncTearDown(self):
        await self.reports.close()
        self.store.close()
        self.tmp.cleanup()

    async def run_case(self, router):
        self.reports.model = router

        async def notify(text):
            self.replies.append(text)

        cid = await self.reports.ingest(
            Message(
                Identity("FixtureBot", "g", "u"),
                "report",
                time.time(),
                "举报",
                ("https://cdn.example/a",),
            ),
            True,
            notify,
        )
        await self.reports.finish(cid)
        return self.store.get(cid)

    async def test_primary_success_executes_once_without_luna(self):
        router = Router()
        c = await self.run_case(router)
        self.assertEqual(c["status"], "submitted")
        self.assertEqual([v[0] for v in router.calls], ["step"])
        self.assertEqual(len(self.service.kicks), 1)

    async def test_primary_negative_is_a_valid_verdict_without_luna(self):
        router = Router({"verdict": "not_substantiated"})
        c = await self.run_case(router)
        self.assertEqual(c["status"], "not_substantiated")
        self.assertEqual([v[0] for v in router.calls], ["step"])
        self.assertEqual(self.service.kicks, [])

    async def test_primary_insufficient_uses_luna_final_target_and_fixed_reason(self):
        router = Router(
            {"verdict": "insufficient", "target_steam_id": "bad"},
            {
                "reason": "arbitrary model text",
                "observed_facts": ["luna final evidence"],
            },
        )
        c = await self.run_case(router)
        self.assertEqual(c["status"], "submitted")
        self.assertEqual([v[0] for v in router.calls], ["step", "luna"])
        self.assertEqual(c["assessment_provider"], LUNA)
        self.assertEqual(len(c["assessment_attempts"]), 2)
        self.assertEqual(
            [v["status"] for v in c["assessment_attempts"]], ["failed", "accepted"]
        )
        self.assertEqual(
            c["assessment_attempts"][0]["error_code"], "model_insufficient"
        )
        self.assertEqual(c["decision"]["observed_facts"], ["luna final evidence"])
        self.assertEqual(self.service.kicks[0][1]["target_steam_id"], ID)
        self.assertNotEqual(self.service.kicks[0][1]["reason"], "arbitrary model text")
        self.assertEqual(router.calls[0][1:], router.calls[1][1:])
        self.assertNotIn("step_result", router.calls[1][1])

    async def test_primary_low_confidence_is_replaced_by_luna(self):
        c = await self.run_case(Router({"identity_confidence": 0.1}))
        self.assertEqual(c["status"], "submitted")
        self.assertEqual(c["decision"]["identity_confidence"], 0.999)

    async def test_primary_identity_failure_luna_negative_wins(self):
        c = await self.run_case(
            Router(
                {"target_steam_id": "76561190000000002"},
                {"verdict": "not_substantiated"},
            )
        )
        self.assertEqual(c["status"], "not_substantiated")
        self.assertEqual(c["assessment_provider"], LUNA)
        self.assertEqual(self.service.kicks, [])

    async def test_primary_provider_exception_uses_luna_without_exposing_error(self):
        c = await self.run_case(Router(RuntimeError("secret must not escape")))
        self.assertEqual(c["status"], "submitted")
        self.assertNotIn("secret", json.dumps(c))
        self.assertNotIn("secret", "".join(self.replies))
        self.assertEqual(c["assessment_attempts"][0]["error_type"], "RuntimeError")

    async def test_primary_deadline_cancels_it_and_gives_luna_own_budget(self):
        cancelled = []

        async def slow(*args):
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.append(True)

        self.reports.assessment_timeout = 0.05
        c = await self.run_case(Router(slow))
        self.assertEqual(c["status"], "submitted")
        self.assertTrue(cancelled)
        self.assertEqual(c["assessment_attempts"][0]["error_type"], "TimeoutError")

    async def test_luna_missing_time_is_not_overridden_by_primary(self):
        c = await self.run_case(Router({"verdict": "insufficient"}, {"observed_at": 0}))
        self.assertEqual(c["status"], "needs_evidence")
        self.assertEqual(c["assessment_provider"], LUNA)
        self.assertEqual(self.service.kicks, [])

    async def test_both_insufficient_request_evidence_without_execution(self):
        c = await self.run_case(
            Router({"verdict": "insufficient"}, {"verdict": "insufficient"})
        )
        self.assertEqual(c["status"], "needs_evidence")
        self.assertEqual(len(c["assessment_attempts"]), 2)
        self.assertEqual(self.service.kicks, [])

    async def test_luna_exception_has_no_retry_or_primary_reuse(self):
        router = Router(TimeoutError(), RuntimeError("private error"))
        c = await self.run_case(router)
        self.assertEqual(c["status"], "technical_failure")
        self.assertEqual([v[0] for v in router.calls], ["step", "luna"])
        self.assertEqual(self.service.kicks, [])

    async def test_actual_duplicate_json_routes_to_luna_and_submits_once(self):
        calls = []

        class Context:
            async def llm_generate(self, **kw):
                calls.append(kw)
                if kw["chat_provider_id"] != LUNA:
                    return SimpleNamespace(
                        completion_text='{"verdict":"insufficient","verdict":"substantiated"}'
                    )
                ref = json.loads(kw["prompt"])["image_order"][0]
                d = decision(
                    combat_evidence=combat_fixture(ref),
                    observed_at=time.time(),
                    evidence_refs=[ref],
                    identity_evidence=[ref],
                )
                return SimpleNamespace(completion_text=json.dumps(d))

        class PngMedia:
            async def load(self, url):
                return picture(30, 30)

        self.reports.media = PngMedia()
        c = await self.run_case(ContextModel(Context(), "scope"))
        self.assertEqual(c["status"], "submitted")
        self.assertEqual(
            [v["chat_provider_id"] for v in calls], ["阶越星辰/step-5-preview", LUNA]
        )
        self.assertTrue(all(v["tools"] is None for v in calls))
        self.assertEqual(c["assessment_attempts"][0]["error_type"], "ValueError")
        self.assertEqual(c["assessment_attempts"][0]["error_code"], "duplicate_field")
        self.assertEqual(len(self.service.kicks), 1)

    async def test_luna_invalid_json_is_not_accepted_or_retried(self):
        calls = []

        class Context:
            async def llm_generate(self, **kw):
                calls.append(kw["chat_provider_id"])
                return SimpleNamespace(
                    completion_text='{"verdict":null,"verdict":"substantiated"}'
                )

        class PngMedia:
            async def load(self, url):
                return picture(30, 30)

        self.reports.media = PngMedia()
        c = await self.run_case(ContextModel(Context(), "scope"))
        self.assertEqual(c["status"], "needs_evidence")
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.service.kicks, [])

    async def test_luna_deadline_leaves_technical_failure_without_execution(self):
        async def slow(*args):
            await asyncio.sleep(60)

        self.reports.assessment_timeout = 0.05
        c = await self.run_case(Router(TimeoutError(), slow))
        self.assertEqual(c["status"], "technical_failure")
        self.assertEqual(len(c["assessment_attempts"]), 2)
        self.assertEqual(self.service.kicks, [])

    async def test_luna_primary_does_not_call_same_model_twice(self):
        router = Router(TimeoutError())
        router.provider_id = LUNA
        c = await self.run_case(router)
        self.assertEqual(c["status"], "technical_failure")
        self.assertEqual(len(router.calls), 1)
        self.assertEqual(self.service.kicks, [])

    async def test_cancelled_assessment_does_not_start_luna(self):
        entered = asyncio.Event()

        async def slow(*args):
            entered.set()
            await asyncio.sleep(60)

        router = Router(slow)
        task = asyncio.create_task(self.run_case(router))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([v[0] for v in router.calls], ["step"])
        self.assertEqual(self.service.kicks, [])

    async def test_kick_timeout_never_reinvokes_models(self):
        self.service.error = True
        router = Router()
        c = await self.run_case(router)
        self.assertEqual(c["status"], "outcome_unknown")
        self.assertEqual([v[0] for v in router.calls], ["step"])
        self.assertEqual(len(self.service.kicks), 1)


class ProviderFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_uses_fixed_luna_and_same_images_without_tools(self):
        calls = []

        class Context:
            async def llm_generate(self, **kw):
                calls.append(kw)
                return SimpleNamespace(completion_text='{"verdict":"insufficient"}')

        m = ContextModel(Context(), "scope")
        images = {"e1": picture(30, 30)}
        await m.assess({}, images, [], {})
        await m.fallback_assess({}, images, [], {})
        self.assertEqual([v["chat_provider_id"] for v in calls], [m.provider_id, LUNA])
        self.assertEqual(calls[0]["image_urls"], calls[1]["image_urls"])
        self.assertTrue(all(v["tools"] is None for v in calls))

    async def test_intent_invalid_json_uses_luna(self):
        calls = []

        class Context:
            async def llm_generate(self, **kw):
                calls.append(kw["chat_provider_id"])
                return SimpleNamespace(
                    completion_text="invalid"
                    if len(calls) == 1
                    else '{"is_report":true}'
                )

        self.assertTrue(await ContextModel(Context(), "scope").intent("举报违规"))
        self.assertEqual(calls, ["阶越星辰/step-5-preview", LUNA])

    async def test_intent_primary_timeout_has_independent_luna_budget(self):
        from unittest.mock import patch

        calls = []

        class Context:
            async def llm_generate(self, **kw):
                calls.append(kw["chat_provider_id"])
                if kw["chat_provider_id"] != LUNA:
                    await asyncio.sleep(60)
                return SimpleNamespace(completion_text='{"is_report":true}')

        with patch("wardogs.model.INTENT_TIMEOUT", 0.05):
            self.assertTrue(await ContextModel(Context(), "scope").intent("举报"))
        self.assertEqual(calls, ["阶越星辰/step-5-preview", LUNA])

    async def test_intent_missing_boolean_uses_luna(self):
        class Context:
            async def llm_generate(self, **kw):
                return SimpleNamespace(
                    completion_text="{}"
                    if kw["chat_provider_id"] != LUNA
                    else '{"is_report":true}'
                )

        self.assertTrue(await ContextModel(Context(), "scope").intent("举报违规"))
