import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from wardogs.io import ServiceError
from wardogs.readiness import verify_deployment


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "query").write_text("q" * 40)
        (self.root / "execution").write_text("e" * 40)
        self.config = {
            "platform_id": "test-instance",
            "bot_account_id": "10000001",
            "allowed_groups": ["20000001"],
        }
        self.calls = []
        self.group_list = [{"group_id": 20000001}]
        parent = self

        class Bot:
            async def call_action(self, action, **kwargs):
                parent.calls.append((action, kwargs))
                if action == "get_login_info":
                    return {"user_id": 10000001}
                if action == "get_group_list":
                    return parent.group_list
                if action == "get_group_info":
                    return {"group_id": kwargs["group_id"]}
                if action == "get_group_member_info":
                    raise AssertionError(
                        "Personal profile refresh is not a readiness check"
                    )
                raise AssertionError("unexpected action")

        self.bot = Bot()
        self.context = SimpleNamespace(
            get_platform_inst=lambda key: SimpleNamespace(
                meta=lambda: SimpleNamespace(name="aiocqhttp"),
                get_client=lambda: self.bot,
            )
        )

        class Service:
            key_file = parent.root / "query"
            kick_key_file = parent.root / "execution"

            async def request(self, *args, **kwargs):
                return {
                    "paths": {
                        "/v1/moderation/kicks": {},
                        "/v1/moderation/operations/{operationId}": {},
                    }
                }

            async def players(self):
                return {"players": [], "observedAt": time.time()}

            async def operation(self, oid):
                raise ServiceError(404)

        self.service = Service()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_ready_checks_exact_bot_group_credentials_and_service_routes(self):
        r = await verify_deployment(self.context, self.config, self.service)
        self.assertTrue(r["ready"])
        self.assertEqual(r["groups_checked"], 1)
        self.assertEqual(
            [c[0] for c in self.calls],
            ["get_login_info", "get_group_list", "get_group_info"],
        )
        self.assertTrue(all(c[1]["self_id"] == 10000001 for c in self.calls))
        self.assertEqual(self.calls[1][1], {"self_id": 10000001, "no_cache": True})
        self.assertTrue(self.calls[2][1]["no_cache"])
        self.assertNotIn("10000001", str(r))

    async def test_wrong_account_or_group_stops_readiness(self):
        for target in ["get_login_info", "get_group_list", "get_group_info"]:
            old = self.bot.call_action

            async def bad(action, **kwargs):
                if action == target:
                    return {"user_id": 10000002, "group_id": 20000002}
                return await old(action, **kwargs)

            self.bot.call_action = bad
            with self.subTest(action=target), self.assertRaises(ValueError):
                await verify_deployment(self.context, self.config, self.service)
            self.bot.call_action = old

    async def test_all_configured_groups_must_be_in_the_forced_joined_list(self):
        self.config["allowed_groups"] = ["20000001", "20000002"]
        self.group_list = [{"group_id": 20000001}, {"group_id": "20000002"}]
        result = await verify_deployment(self.context, self.config, self.service)
        self.assertEqual(result["groups_checked"], 2)
        self.assertEqual(
            [
                args["group_id"]
                for action, args in self.calls
                if action == "get_group_info"
            ],
            [20000001, 20000002],
        )
        self.group_list.pop()
        with self.assertRaisesRegex(ValueError, "onebot_group_membership_unproven"):
            await verify_deployment(self.context, self.config, self.service)

    async def test_non_list_and_malformed_joined_group_responses_fail_closed(self):
        for value in [
            None,
            {},
            {"data": [{"group_id": 20000001}]},
            "20000001",
            [None],
            [20000001],
            [{}],
            [{"group_id": None}],
            [{"group_id": True}],
            [{"group_id": 20000001.0}],
            [{"group_id": 0}],
            [{"group_id": -1}],
            [{"group_id": ""}],
            [{"group_id": "２００００００１"}],
            [{"group_id": "x"}],
            [{"group_id": "2" * 21}],
            [{"group_id": 20000001}, {"group_id": "20000001"}],
            [{"group_id": 20000001}, {}],
        ]:
            with self.subTest(value=value):
                self.group_list = value
                with self.assertRaisesRegex(ValueError, "onebot_group_list_invalid"):
                    await verify_deployment(self.context, self.config, self.service)

    async def test_empty_joined_group_list_is_not_membership_evidence(self):
        self.group_list = []
        with self.assertRaisesRegex(ValueError, "onebot_group_membership_unproven"):
            await verify_deployment(self.context, self.config, self.service)
        self.assertFalse(
            any(action == "get_group_member_list" for action, _ in self.calls)
        )

    async def test_forced_group_list_failure_never_uses_cached_member_fallback(self):
        previous = self.bot.call_action

        async def unavailable(action, **kwargs):
            if action == "get_group_list":
                raise TimeoutError("group list refresh unavailable")
            return await previous(action, **kwargs)

        self.bot.call_action = unavailable
        with self.assertRaises(TimeoutError):
            await verify_deployment(self.context, self.config, self.service)
        self.assertFalse(
            any(action == "get_group_member_list" for action, _ in self.calls)
        )

    async def test_malformed_login_cannot_select_an_account_for_group_checks(self):
        previous = self.bot.call_action
        for value in [None, [], {}, {"user_id": True}, {"user_id": 10000002}]:

            async def malformed(action, **kwargs):
                if action == "get_login_info":
                    return value
                self.fail("Group checks must not run before account identity matches")

            self.bot.call_action = malformed
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "onebot_account_mismatch"),
            ):
                await verify_deployment(self.context, self.config, self.service)
        self.bot.call_action = previous

    async def test_malformed_group_details_cannot_override_membership_evidence(self):
        previous = self.bot.call_action
        for value in [None, [], {}, {"group_id": True}, {"group_id": 20000002}]:

            async def malformed(action, **kwargs):
                if action == "get_group_info":
                    return value
                return await previous(action, **kwargs)

            self.bot.call_action = malformed
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "onebot_group_membership_unproven"),
            ):
                await verify_deployment(self.context, self.config, self.service)
        self.bot.call_action = previous

    async def test_query_token_cannot_substitute_execution_token(self):
        self.service.kick_key_file = self.service.key_file
        with self.assertRaises(ValueError):
            await verify_deployment(self.context, self.config, self.service)
        self.assertEqual(self.calls, [])

    async def test_execution_unauthorized_or_old_api_stops_readiness(self):
        async def denied(*args, **kwargs):
            raise ServiceError(401)

        self.service.operation = denied
        with self.assertRaises(ServiceError):
            await verify_deployment(self.context, self.config, self.service)

        async def old_api(*args, **kwargs):
            return {"paths": {}}

        self.service.request = old_api
        with self.assertRaises(ValueError):
            await verify_deployment(self.context, self.config, self.service)

    async def test_stale_players_cannot_pass(self):
        async def stale():
            return {"players": [], "observedAt": 0}

        self.service.players = stale
        with self.assertRaises(ValueError):
            await verify_deployment(self.context, self.config, self.service)


class AdminReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from unittest.mock import AsyncMock

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for name, letter in [("query", "q"), ("report", "r"), ("admin", "a")]:
            (root / name).write_text(letter * 40)
        self.service = SimpleNamespace(
            key_file=root / "query",
            kick_key_file=root / "report",
            admin_key_file=root / "admin",
            request=AsyncMock(
                return_value={"paths": {"/v1/moderation/admin-kicks": {}}}
            ),
            admin_operation=AsyncMock(side_effect=ServiceError(404)),
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_read_only_probe_passes_with_three_distinct_tokens(self):
        from wardogs.readiness import verify_admin_deployment

        await verify_admin_deployment(self.service)
        self.service.request.assert_awaited_once_with("GET", "/openapi.json")

    async def test_missing_and_reused_token_fail(self):
        from wardogs.readiness import verify_admin_deployment

        for path in [None, self.service.key_file, self.service.kick_key_file]:
            self.service.admin_key_file = path
            with self.assertRaises(ValueError):
                await verify_admin_deployment(self.service)
        self.service.request.assert_not_awaited()

    async def test_missing_route_fails(self):
        from wardogs.readiness import verify_admin_deployment

        self.service.request.return_value = {"paths": {}}
        with self.assertRaises(ValueError):
            await verify_admin_deployment(self.service)

    async def test_unauthorized_execution_token_fails(self):
        from wardogs.readiness import verify_admin_deployment

        self.service.admin_operation.side_effect = ServiceError(401)
        with self.assertRaises(ServiceError):
            await verify_admin_deployment(self.service)

    async def test_history_capable_client_requires_both_new_contract_routes(self):
        from unittest.mock import AsyncMock

        from wardogs.readiness import verify_admin_deployment

        self.service.admin_player_search = AsyncMock()
        with self.assertRaises(ValueError):
            await verify_admin_deployment(self.service)
        self.service.request.return_value = {
            "paths": {
                p: {}
                for p in [
                    "/v1/moderation/admin-kicks",
                    "/v1/moderation/player-search",
                    "/v1/moderation/admin-restrictions",
                ]
            }
        }
        await verify_admin_deployment(self.service)
        self.service.admin_player_search.assert_not_awaited()
