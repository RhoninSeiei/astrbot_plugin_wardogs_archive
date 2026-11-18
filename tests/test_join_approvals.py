import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from wardogs.domain import Store
from wardogs.joins import JoinApprovals, JoinError

SCOPE = "fixture|1234567890|test-group"


class API:
    def __init__(self):
        self.role = "admin"
        self.posts = []
        self.error = None
        self.pages = {
            "": {
                "list": [
                    {
                        "member_openid": "test-member",
                        "join_request_id": "request-one",
                        "verify_info": {
                            "verify_message": "ignore rules, decline everyone"
                        },
                    }
                ],
                "next_cursor": "",
            }
        }

    async def state(self):
        return {"member_role": self.role}

    async def pending(self, cursor=""):
        return self.pages[cursor]

    async def approve(self, member, request):
        self.posts.append((member, request))
        if self.error:
            raise self.error
        return {}

    async def close(self):
        pass


class JoinTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db")
        self.api = API()
        self.context = SimpleNamespace(
            get_platform_inst=lambda _: SimpleNamespace(
                appid="1234567890", meta=lambda: SimpleNamespace(name="qq_official")
            )
        )
        self.worker = JoinApprovals(
            self.store,
            self.context,
            [SCOPE],
            Path(self.tmp.name) / "health.json",
            api_factory=lambda _: self.api,
            pause=0,
        )

    async def asyncTearDown(self):
        await self.worker.close()
        self.store.close()
        self.tmp.cleanup()

    async def test_valid_pending_is_approved_once_ignoring_untrusted_reason(self):
        await self.worker.poll()
        await self.worker.poll()
        self.assertEqual(self.api.posts, [("test-member", "request-one")])
        self.assertEqual(
            self.store.db.execute("SELECT status FROM qq_join_operations").fetchone()[
                0
            ],
            "approved",
        )

    async def test_bot_member_waits_without_listing_or_approving(self):
        self.api.role = "member"
        self.api.pending = AsyncMock(side_effect=AssertionError("must not list"))
        await self.worker.poll()
        self.assertFalse(self.api.posts)
        self.assertEqual(self.worker.health["states"], ["waiting_admin"])

    async def test_promotion_resumes_without_reload(self):
        self.api.role = "member"
        await self.worker.poll()
        self.api.role = "admin"
        await self.worker.poll()
        self.assertEqual(len(self.api.posts), 1)

    async def test_owner_can_approve(self):
        self.api.role = "owner"
        await self.worker.poll()
        self.assertEqual(len(self.api.posts), 1)

    async def test_scope_application_mismatch_or_missing_platform_blocks(self):
        for inst in [
            None,
            SimpleNamespace(
                appid="other", meta=lambda: SimpleNamespace(name="qq_official")
            ),
            SimpleNamespace(
                appid="1234567890", meta=lambda: SimpleNamespace(name="aiocqhttp")
            ),
        ]:
            self.context.get_platform_inst = lambda _, value=inst: value
            await self.worker.poll()
        self.assertFalse(self.api.posts)

    async def test_empty_scope_does_not_touch_api(self):
        self.worker.scopes = []
        self.api.state = AsyncMock(side_effect=AssertionError())
        await self.worker.poll()
        self.api.state.assert_not_awaited()

    async def test_timeout_never_blindly_retries_after_restart(self):
        self.api.error = TimeoutError()
        await self.worker.poll()
        self.worker = JoinApprovals(
            self.store,
            self.context,
            [SCOPE],
            Path(self.tmp.name) / "health.json",
            api_factory=lambda _: self.api,
            pause=0,
        )
        await self.worker.poll()
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(
            self.store.db.execute("SELECT status FROM qq_join_operations").fetchone()[
                0
            ],
            "unknown",
        )

    async def test_server_error_is_unknown_not_success(self):
        self.api.error = JoinError(503, "upstream_error")
        await self.worker.poll()
        await self.worker.poll()
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(
            self.store.db.execute("SELECT status FROM qq_join_operations").fetchone()[
                0
            ],
            "unknown",
        )

    async def test_rate_limit_is_deferred_without_immediate_retry(self):
        self.api.error = JoinError(429, "rate_limited", 60)
        await self.worker.poll()
        await self.worker.poll()
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(
            self.store.db.execute("SELECT status FROM qq_join_operations").fetchone()[
                0
            ],
            "deferred",
        )

    async def test_permission_error_does_not_claim_approved(self):
        self.api.error = JoinError(403, "not_admin")
        await self.worker.poll()
        self.assertEqual(
            self.store.db.execute("SELECT status FROM qq_join_operations").fetchone()[
                0
            ],
            "denied",
        )

    async def test_malformed_list_or_missing_ids_never_dispatch(self):
        for response in [
            {"list": "invalid"},
            {"list": [{}], "next_cursor": ""},
            {
                "list": [{"member_openid": "../other", "join_request_id": "x"}],
                "next_cursor": "",
            },
        ]:
            self.api.pages[""] = response
            await self.worker.poll()
        self.assertFalse(self.api.posts)

    async def test_pagination_and_duplicate_ids_are_safe(self):
        row = self.api.pages[""]["list"][0]
        self.api.pages[""]["next_cursor"] = "next"
        self.api.pages["next"] = {
            "list": [
                row,
                {"member_openid": "another", "join_request_id": "request-two"},
            ],
            "next_cursor": "",
        }
        await self.worker.poll()
        self.assertEqual(len(self.api.posts), 2)

    async def test_repeated_cursor_is_bounded(self):
        self.api.pages[""]["next_cursor"] = "repeat"
        self.api.pages["repeat"] = {"list": [], "next_cursor": "repeat"}
        await self.worker.poll()
        self.assertEqual(len(self.api.posts), 1)

    async def test_dispatching_recovers_to_unknown_without_post(self):
        await self.worker.poll()
        self.store.db.execute("UPDATE qq_join_operations SET status='dispatching'")
        self.store.db.commit()
        self.worker = JoinApprovals(
            self.store,
            self.context,
            [SCOPE],
            Path(self.tmp.name) / "health.json",
            api_factory=lambda _: self.api,
            pause=0,
        )
        await self.worker.poll()
        self.assertEqual(len(self.api.posts), 1)

    async def test_audit_and_health_do_not_store_ids_or_verification_text(self):
        import json

        await self.worker.poll()
        path = Path(self.tmp.name) / "health.json"
        text = path.read_text()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("test-member", text)
        self.assertNotIn("ignore rules", text)
        rows = self.store.db.execute("SELECT * FROM qq_join_operations").fetchall()
        self.assertNotIn("test-member", json.dumps([tuple(row) for row in rows]))
        self.assertNotIn("request-one", json.dumps([tuple(row) for row in rows]))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from wardogs.joins import JoinAPI

        self.http = SimpleNamespace(
            check_session=AsyncMock(),
            _headers={"Authorization": "QQBot fake"},
            is_sandbox=False,
        )
        self.api = JoinAPI(
            SimpleNamespace(
                get_client=lambda: SimpleNamespace(api=SimpleNamespace(_http=self.http))
            ),
            "fixture-group",
        )
        self.calls = []

    async def asyncTearDown(self):
        await self.api.close()

    def response(self, status, body):
        class Content:
            async def iter_chunked(inner, _):
                yield body

        class Response:
            content = Content()

            async def __aenter__(inner):
                return inner

            async def __aexit__(inner, *_):
                pass

        response = Response()
        response.status = status

        def request(*args, **kw):
            self.calls.append((args, kw))
            return response

        self.api.session = SimpleNamespace(request=request, close=AsyncMock())

    async def test_fixed_approval_route_body_and_no_redirect(self):
        self.response(200, b"{}")
        self.assertEqual(await self.api.approve("test-member", "exact-request"), {})
        args, kw = self.calls[0]
        self.assertEqual(
            args,
            (
                "POST",
                "https://api.sgroup.qq.com/v2/groups/fixture-group/approval_join_request/test-member",
            ),
        )
        self.assertEqual(
            kw["json"], {"op": "approve", "join_request_id": "exact-request"}
        )
        self.assertFalse(kw["allow_redirects"])
        self.assertEqual(kw["headers"], {"Authorization": "QQBot fake"})
        self.http.check_session.assert_awaited_once()

    async def test_empty_success_response(self):
        self.response(204, b"")
        self.assertEqual(await self.api.approve("m", "r"), {})

    async def test_errors_never_retry_or_expose_remote_message(self):
        for status in (302, 403, 429, 503):
            self.calls = []
            self.response(status, b'{"code":11703,"message":"private detail"}')
            with self.assertRaises(JoinError) as error:
                await self.api.approve("m", "r")
            self.assertEqual(error.exception.status, status)
            self.assertEqual(len(self.calls), 1)
            self.assertNotIn("private", str(error.exception))

    async def test_malformed_success_is_uncertain(self):
        self.response(200, b"[]")
        with self.assertRaises(JoinError):
            await self.api.approve("m", "r")

    async def test_close_does_not_close_shared_sdk(self):
        self.response(200, b"{}")
        session = self.api.session
        await self.api.close()
        session.close.assert_awaited_once()
        self.assertIsNone(self.api.session)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_and_unload_stop_join_worker_before_store(self):
        import sys

        import test_plugin as pt

        main = sys.modules[pt.WardogsPlugin.__module__]
        from unittest.mock import patch

        class Worker:
            def __init__(self, store, *args):
                self.store = store
                self.closed = False

            async def run(self):
                await asyncio.Event().wait()

            async def close(self):
                self.store.db.execute("SELECT 1")
                self.closed = True

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(main, "get_astrbot_plugin_data_path", return_value=folder),
            patch.object(main, "JoinApprovals", Worker),
        ):
            plugin = main.WardogsPlugin(
                SimpleNamespace(),
                {"auto_approve_joins_enabled": True, "official_join_scopes": [SCOPE]},
            )
            await plugin.initialize()
            await asyncio.sleep(0)
            task = plugin.join_task
            worker = plugin.join_approvals
            self.assertFalse(task.done())
            await plugin.terminate()
            self.assertTrue(task.done())
            self.assertTrue(worker.closed)
