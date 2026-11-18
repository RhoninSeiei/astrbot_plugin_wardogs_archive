"""Bounded official QQ join approvals with durable uncertainty handling."""

import asyncio
import hashlib
import json
import os
import re
import time
from urllib.parse import quote

import aiohttp

from .io import bounded_body


class JoinError(Exception):
    def __init__(self, status=0, code="transport_error", retry_after=60):
        self.status = status
        self.code = (
            str(code) if re.fullmatch(r"[A-Za-z0-9_]{1,64}", str(code)) else "api_error"
        )
        self.retry_after = max(60, min(3600, retry_after))
        super().__init__(f"qq_join_error:{status}:{self.code}")


class JoinAPI:
    def __init__(self, platform, group):
        self.http = platform.get_client().api._http
        self.base = (
            "https://sandbox.api.sgroup.qq.com"
            if getattr(self.http, "is_sandbox", False)
            else "https://api.sgroup.qq.com"
        )
        self.path = "/v2/groups/" + quote(group, safe="")
        self.session = None

    async def request(self, method, suffix, **kwargs):
        try:
            await self.http.check_session()
            if self.session is None:
                self.session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=15, connect=5), trust_env=False
                )
            async with self.session.request(
                method,
                self.base + self.path + suffix,
                headers=dict(self.http._headers),
                allow_redirects=False,
                **kwargs,
            ) as response:
                body = await bounded_body(response, 262144)
                if not 200 <= response.status < 300:
                    try:
                        code = json.loads(body).get("code", "api_error")
                    except (ValueError, AttributeError):
                        code = "api_error"
                    raise JoinError(response.status, code)
                data = json.loads(body) if body else {}
                if not isinstance(data, dict):
                    raise JoinError(0, "invalid_response")
                return data
        except JoinError:
            raise
        except Exception:
            raise JoinError() from None

    async def state(self):
        return await self.request("GET", "/bot_state")

    async def pending(self, cursor=""):
        return await self.request(
            "GET", "/join_request_list", params={"limit": 50, "cursor": cursor}
        )

    async def approve(self, member, request):
        return await self.request(
            "POST",
            "/approval_join_request/" + quote(member, safe=""),
            json={"op": "approve", "join_request_id": request},
        )

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None


class JoinApprovals:
    def __init__(
        self, store, context, scopes, health_path, api_factory=None, pause=1.1
    ):
        self.store, self.context, self.scopes = (
            store,
            context,
            list(dict.fromkeys(scopes)),
        )
        self.health_path, self.api_factory, self.pause = health_path, api_factory, pause
        self.apis, self.cursors, self.health = {}, {}, {}
        store.db.execute(
            "CREATE TABLE IF NOT EXISTS qq_join_operations (key TEXT PRIMARY KEY,status TEXT,created REAL,updated REAL,error_code TEXT)"
        )
        store.db.execute(
            "UPDATE qq_join_operations SET status='unknown' WHERE status='dispatching'"
        )
        store.db.commit()

    async def poll(self):
        states = []
        for scope in self.scopes:
            try:
                platform_id, appid, group = scope.split("|")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", group):
                    raise ValueError()
                platform = self.context.get_platform_inst(platform_id)
                if (
                    platform is None
                    or platform.meta().name != "qq_official"
                    or str(platform.appid) != appid
                ):
                    raise ValueError()
                if scope not in self.apis:
                    self.apis[scope] = (
                        self.api_factory(platform)
                        if self.api_factory
                        else JoinAPI(platform, group)
                    )
                api = self.apis[scope]
                state = await api.state()
                if state.get("member_role") not in ("owner", "admin"):
                    states.append("waiting_admin")
                    continue
                cursor = self.cursors.get(scope, "")
                seen, dispatched = set(), 0
                for _ in range(5):
                    if cursor in seen:
                        break
                    seen.add(cursor)
                    page = await api.pending(cursor)
                    if not isinstance(page, dict) or not isinstance(
                        page.get("list"), list
                    ):
                        raise ValueError()
                    exhausted = True
                    for row in page["list"][:50]:
                        if not isinstance(row, dict):
                            continue
                        member, request = (
                            row.get("member_openid"),
                            row.get("join_request_id"),
                        )
                        if (
                            not isinstance(member, str)
                            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", member)
                            or not isinstance(request, str)
                            or not 1 <= len(request) <= 2048
                            or not request.isascii()
                            or not request.isprintable()
                        ):
                            continue
                        key = hashlib.sha256(
                            json.dumps([scope, request]).encode()
                        ).hexdigest()
                        previous = self.store.db.execute(
                            "SELECT status,updated FROM qq_join_operations WHERE key=?",
                            (key,),
                        ).fetchone()
                        now = time.time()
                        if previous and not (
                            previous[0] == "deferred" and previous[1] <= now
                        ):
                            continue
                        if dispatched >= 10:
                            exhausted = False
                            break
                        self.store.db.execute(
                            "INSERT INTO qq_join_operations VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET status='dispatching',updated=excluded.updated,error_code=''",
                            (key, "dispatching", now, now, ""),
                        )
                        self.store.db.commit()
                        status, code, updated = "unknown", "", now
                        try:
                            result = await api.approve(member, request)
                            if not isinstance(result, dict):
                                raise JoinError(0, "invalid_response")
                            status = "approved"
                        except asyncio.CancelledError:
                            raise
                        except JoinError as error:
                            code = error.code
                            if error.status == 429:
                                status, updated = "deferred", now + error.retry_after
                            elif 400 <= error.status < 500:
                                status = "denied"
                        except Exception:
                            code = "transport_error"
                        finally:
                            self.store.db.execute(
                                "UPDATE qq_join_operations SET status=?,updated=?,error_code=? WHERE key=?",
                                (status, updated, code, key),
                            )
                            self.store.db.commit()
                        dispatched += 1
                        await asyncio.sleep(self.pause)
                        if status != "approved":
                            exhausted = False
                            break
                    next_cursor = page.get("next_cursor", "")
                    if not exhausted:
                        break
                    if not isinstance(next_cursor, str) or len(next_cursor) > 2048:
                        raise ValueError()
                    cursor = next_cursor
                    if not cursor:
                        break
                self.cursors[scope] = cursor
                states.append("ready")
            except asyncio.CancelledError:
                raise
            except Exception:
                states.append("unavailable")
        counts = dict(
            self.store.db.execute(
                "SELECT status,COUNT(*) FROM qq_join_operations GROUP BY status"
            )
        )
        self.health = {
            "checked_at": time.time(),
            "states": states,
            "operations": counts,
        }
        temp = self.health_path.with_suffix(".tmp")
        with os.fdopen(
            os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), "w"
        ) as stream:
            json.dump(self.health, stream)
        os.chmod(temp, 0o600)
        os.replace(temp, self.health_path)

    async def run(self):
        while True:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(30)

    async def close(self):
        for api in self.apis.values():
            await api.close()
        self.apis.clear()
