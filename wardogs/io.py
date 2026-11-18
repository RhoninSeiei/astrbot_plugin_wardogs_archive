"""Bounded asynchronous service and QQ-media HTTP clients."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import re
import socket
import stat
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp

from .domain import clean_text


class ServiceError(Exception):
    def __init__(self, status=0):
        self.status = status
        super().__init__(f"wardogs_service_error:{status}")


async def bounded_body(response, limit):
    chunks = []
    size = 0
    async for chunk in response.content.iter_chunked(16384):
        size += len(chunk)
        if size > limit:
            raise ValueError("response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


class ServiceClient:
    def __init__(self, base, key_file, kick_key_file=None, admin_key_file=None):
        u = urlsplit(base)
        if (
            u.scheme not in ("http", "https")
            or not u.hostname
            or u.username
            or u.password
            or u.query
            or u.fragment
            or u.path not in ("", "/")
        ):
            raise ValueError("invalid_service_base")
        self.base = base.rstrip("/")
        self.key_file = Path(key_file) if key_file else None
        self.kick_key_file = Path(kick_key_file) if kick_key_file else None
        self.admin_key_file = Path(admin_key_file) if admin_key_file else None
        self.session = None

    async def request(
        self,
        method,
        path,
        body=None,
        key=None,
        kick=False,
        admin=False,
        response_limit=262144,
    ):
        try:
            token_file = (
                self.admin_key_file
                if admin
                else (self.kick_key_file if kick else self.key_file)
            )
            if token_file is None:
                raise ValueError("token_not_configured")
            token = token_file.read_text(encoding="utf-8-sig").strip()
            if not token or "\n" in token or "\r" in token:
                raise ValueError("token_not_configured")
            headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
            if key:
                headers["Idempotency-Key"] = clean_text(key, 128)
            if self.session is None:
                self.session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=15, connect=5), trust_env=False
                )
            async with self.session.request(
                method,
                self.base + path,
                json=body,
                headers=headers,
                allow_redirects=False,
            ) as r:
                if not 200 <= r.status < 300:
                    raise ServiceError(r.status)
                data = json.loads(await bounded_body(r, response_limit))
                if not isinstance(data, dict):
                    raise ValueError("invalid_response")
                return data
        except ServiceError:
            raise
        except Exception:
            raise ServiceError() from None

    async def players(self):
        return await self.request("GET", "/v1/players")

    async def match(self):
        return await self.request("GET", "/v1/match")

    async def server_identity(self):
        return await self.request("GET", "/v1/server-identity", response_limit=4096)

    async def ban_statistics(self):
        return await self.request("GET", "/v1/moderation/ban-statistics")

    async def state(self):
        return await self.request("GET", "/v1/state")

    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", value
        ):
            raise ValueError("invalid_id")
        return quote(value, safe="")

    async def admin_kick(self, payload, key):
        return await self.request(
            "POST", "/v1/moderation/admin-kicks", payload, key, admin=True
        )

    async def punishment_search(self, payload):
        return await self.request(
            "POST",
            "/v1/moderation/punishment-search",
            payload,
            admin=True,
            response_limit=65536,
        )

    async def punishment_history(self, payload):
        return await self.request(
            "POST",
            "/v1/moderation/punishment-history",
            payload,
            admin=True,
            response_limit=65536,
        )

    async def current_restrictions(self, payload):
        return await self.request(
            "POST",
            "/v1/moderation/current-restrictions",
            payload,
            admin=True,
            response_limit=65536,
        )

    async def admin_unban_search(self, payload):
        return await self.request(
            "POST", "/v1/moderation/unban-search", payload, admin=True
        )

    async def admin_ban(self, payload, key):
        return await self.request(
            "POST", "/v1/moderation/admin-bans", payload, key, admin=True
        )

    async def admin_unban(self, payload, key):
        return await self.request(
            "POST", "/v1/moderation/admin-unbans", payload, key, admin=True
        )

    async def cycle_list(self, payload):
        return await self.request(
            "POST",
            "/v1/moderation/cycle-list",
            payload,
            admin=True,
            response_limit=1048576,
        )

    async def admin_cycle_add(self, payload, key):
        return await self.request(
            "POST", "/v1/moderation/admin-cycle-adds", payload, key, admin=True
        )

    async def player_sanction_summary(self, payload):
        return await self.request(
            "POST",
            "/v1/moderation/player-sanction-summary",
            payload,
            admin=True,
            response_limit=8192,
        )

    async def player_kills(self, payload):
        return await self.request(
            "POST",
            "/v1/moderation/player-kills",
            payload,
            admin=True,
            response_limit=131072,
        )

    async def recent_kills(self, payload):
        return await self.request(
            "POST",
            "/v1/moderation/recent-kills",
            payload,
            admin=True,
            response_limit=4194304,
        )

    async def admin_player_search(self, payload):
        return await self.request(
            "POST", "/v1/moderation/player-search", payload, admin=True
        )

    async def admin_restrict(self, payload, key):
        return await self.request(
            "POST", "/v1/moderation/admin-restrictions", payload, key, admin=True
        )

    async def admin_operation(self, oid):
        return await self.request(
            "GET", "/v1/moderation/operations/" + self.identifier(oid), admin=True
        )

    async def operation(self, oid):
        return await self.request(
            "GET", "/v1/moderation/operations/" + self.identifier(oid), kick=True
        )

    async def job(self, jid):
        return await self.request("GET", "/v1/jobs/" + self.identifier(jid))

    async def broadcast(self, texts, key):
        if len(texts) != 3:
            raise ValueError("three_languages_required")
        for text in texts:
            clean_text(text)
        return await self.request(
            "POST",
            "/v1/broadcast",
            dict(zip(["message", "english", "japanese"], texts)),
            key,
        )

    async def kick(self, case, d):
        payload = {
            "reportId": case["id"],
            "steamId": d["target_steam_id"],
            "ruleId": d["rule_id"],
            "ruleVersion": d["rule_version"],
            "evidenceVersion": str(case.get("evidence_version", 1)),
            "evidenceRefs": d["evidence_refs"],
            "decision": {
                "verified": True,
                "identityScore": d["identity_confidence"],
                "violationScore": d["violation_confidence"],
                "observedAt": case["decided_at"],
            },
            "reason": {"zh": d["reason"], "en": d["english"], "ja": d["japanese"]},
        }
        return await self.request(
            "POST",
            "/v1/moderation/kicks",
            payload,
            f"wd-report-{case['id']}-v{case.get('evidence_version', 1)}",
            kick=True,
        )

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None


class PublicResolver(aiohttp.resolver.DefaultResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await super().resolve(host, port, family)
        if not records or any(
            not ipaddress.ip_address(r["host"]).is_global for r in records
        ):
            raise OSError("private_media_address")
        return records


class Media:
    def __init__(self, hosts, cache_root=None):
        self.cache_root = Path(cache_root).resolve() if cache_root is not None else None
        self.hosts = {h.lower() for h in hosts}
        self.session = None

    def validate_url(self, url):
        u = urlsplit(url)
        if (
            u.scheme != "https"
            or u.hostname not in self.hosts
            or u.username
            or u.password
            or u.port not in (None, 443)
            or u.fragment
        ):
            raise ValueError("untrusted_media_url")
        return url

    def _read_cache(self, source):
        path = Path(source)
        if self.cache_root is None or not path.is_absolute():
            raise ValueError("untrusted_cache_path")
        try:
            parts = path.relative_to(self.cache_root).parts
        except ValueError:
            raise ValueError("untrusted_cache_path") from None
        if (
            not parts
            or ".." in parts
            or not all(
                hasattr(os, flag)
                for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")
            )
        ):
            raise ValueError("untrusted_cache_path")
        directory = None
        descriptor = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            directory = os.open(self.cache_root, flags)
            for part in parts[:-1]:
                next_directory = os.open(part, flags, dir_fd=directory)
                os.close(directory)
                directory = next_directory
            descriptor = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("cache_image_unavailable")
            limit = 8 * 1024 * 1024
            if info.st_size > limit:
                raise ValueError("image_too_large")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                body = stream.read(limit + 1)
            if len(body) > limit:
                raise ValueError("image_too_large")
            return body
        except (OSError, NotImplementedError):
            raise ValueError("cache_image_unavailable") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory is not None:
                os.close(directory)

    @staticmethod
    def _image_uri(body):
        if body.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif body.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif body[:6] in (b"GIF87a", b"GIF89a"):
            mime = "image/gif"
        elif body.startswith(b"RIFF") and body[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            raise ValueError("unsupported_image")
        return f"data:{mime};base64," + base64.b64encode(body).decode("ascii")

    async def load(self, url):
        if Path(url).is_absolute():
            return self._image_uri(await asyncio.to_thread(self._read_cache, url))
        self.validate_url(url)
        if self.session is None:
            self.session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=PublicResolver()),
                timeout=aiohttp.ClientTimeout(total=20, connect=5),
                trust_env=False,
            )
        async with self.session.get(url, allow_redirects=False) as r:
            if r.status != 200:
                raise ValueError("media_unavailable")
            body = await bounded_body(r, 8 * 1024 * 1024)
            return self._image_uri(body)

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None
