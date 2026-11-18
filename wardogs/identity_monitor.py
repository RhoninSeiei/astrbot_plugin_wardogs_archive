"""Single-target automatic notices; ambiguous writes are never replayed."""

import asyncio
import json
import logging

from .server_identity import content, validate


def number(value):
    value = str(value)
    if not value.isascii() or not value.isdigit() or int(value) <= 0:
        raise ValueError("invalid_notice_scope")
    return value


def notice_target(config):
    platform = config.get("platform_id", "")
    account = number(config.get("bot_account_id", ""))
    group = number(config.get("server_identity_notice_group", ""))
    if (
        not isinstance(platform, str)
        or not platform
        or group not in {str(x) for x in config.get("allowed_groups", [])}
    ):
        raise ValueError("unscoped_notice_target")
    return platform, account, group


async def prepare_notice(context, target):
    """All read-only preflight checks complete before claiming a write."""
    platform_id, account, group = target
    platform = context.get_platform_inst(platform_id)
    if platform is None or platform.meta().name != "aiocqhttp":
        raise PermissionError("notice_channel_unavailable")
    client = platform.get_client()

    async def call(action, **kwargs):
        return await asyncio.wait_for(
            client.call_action(action=action, self_id=int(account), **kwargs), 10
        )

    login = await call("get_login_info")
    if str(login.get("user_id")) != account:
        raise PermissionError("notice_account_mismatch")
    groups = await call("get_group_list")
    if not isinstance(groups, list) or not any(
        isinstance(item, dict) and str(item.get("group_id")) == group for item in groups
    ):
        raise PermissionError("notice_group_unverified")
    role = await call(
        "get_group_member_info",
        group_id=int(group),
        user_id=int(account),
        no_cache=True,
    )
    if (
        str(role.get("group_id")) != group
        or str(role.get("user_id")) != account
        or role.get("role") not in ("owner", "admin")
    ):
        raise PermissionError("notice_bot_not_admin")

    async def publish(text):
        await call("_send_group_notice", group_id=int(group), content=text)

    return publish


class IdentityMonitor:
    def __init__(self, db, client, target, prepare):
        self.db, self.client, self.prepare = db, client, prepare
        self.target = json.dumps(target, separators=(",", ":"))
        self.status = "starting"
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS wd_server_identity_state(target TEXT PRIMARY KEY,server_id TEXT NOT NULL,observed REAL NOT NULL,status TEXT NOT NULL,pending_id TEXT)"
            )
            # Only this exact target belongs to this monitor.
            db.execute(
                "UPDATE wd_server_identity_state SET status='unknown' WHERE target=? AND status='dispatching'",
                (self.target,),
            )

    async def tick(self):
        try:
            data = validate(await asyncio.wait_for(self.client.server_identity(), 10))
        except Exception:
            self.status = "identity_unavailable"
            return
        sid = data["serverId"]
        with self.db:
            created = self.db.execute(
                "INSERT OR IGNORE INTO wd_server_identity_state VALUES(?,?,?,'baseline',NULL)",
                (self.target, sid, data["observedAt"]),
            ).rowcount
        if created:
            self.status = "baseline"
            return
        row = self.db.execute(
            "SELECT server_id,status FROM wd_server_identity_state WHERE target=?",
            (self.target,),
        ).fetchone()
        if row[1] in ("unknown", "dispatching"):
            self.status = row[1]
            return
        if row[0] == sid:
            self.status = "unchanged"
            return
        try:
            publish = await asyncio.wait_for(self.prepare(), 35)
            # Preflight may take time; re-read to avoid posting an obsolete ID.
            latest = validate(await asyncio.wait_for(self.client.server_identity(), 10))
            if latest["serverId"] != sid:
                self.status = "identity_changed_during_preflight"
                return
            text = content(latest)
        except Exception:
            self.status = "preflight_unavailable"
            return
        with self.db:
            claimed = self.db.execute(
                "UPDATE wd_server_identity_state SET status='dispatching',pending_id=? WHERE target=? AND server_id=? AND status IN ('baseline','submitted')",
                (sid, self.target, row[0]),
            ).rowcount
        if not claimed:
            return
        try:
            await asyncio.wait_for(publish(text), 15)
        except BaseException as error:
            with self.db:
                self.db.execute(
                    "UPDATE wd_server_identity_state SET status='unknown' WHERE target=?",
                    (self.target,),
                )
            self.status = "unknown"
            if isinstance(error, asyncio.CancelledError):
                raise
            return
        with self.db:
            self.db.execute(
                "UPDATE wd_server_identity_state SET server_id=?,observed=?,status='submitted',pending_id=NULL WHERE target=?",
                (sid, latest["observedAt"], self.target),
            )
        self.status = "submitted"

    async def run(self):
        try:
            await asyncio.wait_for(self.prepare(), 35)
            logging.getLogger(__name__).info(
                "WARDOGS server identity notice channel verified; no notice sent."
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).warning(
                "WARDOGS server identity notice channel unverified; changes require a fresh successful preflight."
            )
        previous = None
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status = "monitor_unavailable"
            if self.status != previous:
                # State labels only: never log raw API exceptions or payloads.
                logging.getLogger(__name__).info(
                    "WARDOGS server identity monitor: %s", self.status
                )
                previous = self.status
            await asyncio.sleep(60)
