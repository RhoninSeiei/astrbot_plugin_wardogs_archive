"""Explicit history-menu permanent bans using the existing fixed RCON service."""

import asyncio
import json
import logging
import re
import time

from .admin_history import actor, valid_time


def receipt(result, target, operation=None):
    if (
        not isinstance(result, dict)
        or result.get("steamId") != target
        or result.get("source") != "admin"
        or result.get("action") != "ban"
        or not isinstance(result.get("operationId"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", result["operationId"])
        or (operation is not None and result["operationId"] != operation)
    ):
        raise ValueError("invalid_ban_receipt")
    native, local, state = (
        result.get(k)
        for k in ("nativeBanStatus", "localRestrictionStatus", "banStatus")
    )
    if native not in (
        "not_checked",
        "banned",
        "already_banned",
        "failed",
        "unknown",
    ) or local not in (
        "not_attempted",
        "restricted",
        "already_restricted",
        "not_restricted",
        "unknown",
        "exempt",
    ):
        raise ValueError("invalid_ban_states")
    if native in ("banned", "already_banned") and not valid_time(
        result.get("nativeVerifiedAt")
    ):
        raise ValueError("unverified_native_ban")
    if local in ("restricted", "already_restricted") and not valid_time(
        result.get("localVerifiedAt")
    ):
        raise ValueError("unverified_local_ban")
    if state == "pending" and result.get("status") in (
        "accepted",
        "resolving",
        "ban_dispatching",
    ):
        return "accepted"
    if (
        state == "banned"
        and result.get("status") == "completed"
        and native in ("banned", "already_banned")
        and local in ("restricted", "already_restricted")
    ):
        return "banned"
    if state in ("partial", "unknown", "failed") and result.get("status") == state:
        return state
    raise ValueError("inconsistent_ban_receipt")


def result_text(result, target, state):
    if state == "banned":
        text = "已封禁，原生封禁和永久重入限制均已确认：" + target
        if result.get("removalStatus") == "still_online":
            text += "；玩家仍在线，本次尚未确认移出。"
        elif result.get("onlineBefore") is False:
            text += "；处理时离线，本次未实际踢出。"
        return text
    if state == "partial":
        native = {
            "banned": "原生封禁已确认",
            "already_banned": "原已存在原生封禁",
            "failed": "原生封禁失败",
        }.get(result.get("nativeBanStatus"), "原生封禁结果待核实")
        local = {
            "restricted": "永久重入限制已登记",
            "already_restricted": "原已有永久重入限制",
        }.get(result.get("localRestrictionStatus"), "永久重入限制未确认")
        return f"封禁未全部完成：{target}；{local}；{native}。请核实原操作，不自动重复提交。"
    if state == "failed":
        return "封禁失败，未确认完成：" + target
    return "封禁结果待核实，请勿重复提交：" + target


class BanSupport:
    def __init__(self, store, client):
        self.db, self.client, self.receipts = store.db, client, {}
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS admin_ban_operations(
                    id TEXT PRIMARY KEY,target TEXT,status TEXT,created REAL,
                    payload TEXT,result TEXT,operation_id TEXT);
            """)
            self.db.execute(
                "UPDATE admin_ban_operations SET status='outcome_unknown' WHERE status='dispatching'"
            )

    def existing(self, oid):
        return self.db.execute(
            "SELECT status FROM admin_ban_operations WHERE id=?", (oid,)
        ).fetchone()

    def held(self, target):
        return self.db.execute(
            "SELECT id FROM admin_ban_operations WHERE target=? AND status IN ('dispatching','accepted','outcome_unknown','unknown','partial')",
            (target,),
        ).fetchone()

    async def submit(self, controller, message, role, target, oid, tell):
        if role not in ("owner", "admin"):
            await tell("此操作需要平台可核验的群主或管理员身份。")
            return True
        if controller.pending_target(target) or self.existing(oid):
            await tell("该玩家存在待核实处置，未重复封禁。")
            return True
        controller.clear_drafts(
            (message.who.platform, message.who.group, message.who.sender)
        )
        payload = {
            "requestId": oid,
            "steamId": target,
            "actor": actor(message.who, role),
            "requestedAt": time.time(),
        }
        with self.db:
            self.db.execute(
                "INSERT INTO admin_ban_operations VALUES(?,?,'dispatching',?,?,?,NULL)",
                (oid, target, time.time(), json.dumps(payload), "{}"),
            )
        try:
            result = await self.client.admin_ban(payload, "wd-admin-ban-" + oid)
            state = receipt(result, target)
            with self.db:
                self.db.execute(
                    "UPDATE admin_ban_operations SET status=?,result=?,operation_id=? WHERE id=?",
                    (state, json.dumps(result), result["operationId"], oid),
                )
        except BaseException as error:
            with self.db:
                self.db.execute(
                    "UPDATE admin_ban_operations SET status='outcome_unknown' WHERE id=?",
                    (oid,),
                )
            if isinstance(error, asyncio.CancelledError):
                raise
            await tell("封禁请求结果待核实，请勿重复提交。")
            return True
        try:
            await tell("已受理")
        finally:
            from .sanction_summary import receipt_notice

            self.receipts[oid] = (
                receipt_notice(self.client, message.who, role, target, tell),
                time.monotonic() + 300,
                0,
            )
        await self.notify_receipts()
        return True

    async def poll(self):
        for oid, target, operation in self.db.execute(
            "SELECT id,target,operation_id FROM admin_ban_operations WHERE status='accepted' ORDER BY created LIMIT 10"
        ).fetchall():
            try:
                result = await self.client.admin_operation(operation)
                state = receipt(result, target, operation)
                if state != "accepted":
                    with self.db:
                        self.db.execute(
                            "UPDATE admin_ban_operations SET status=?,result=? WHERE id=? AND status='accepted'",
                            (state, json.dumps(result), oid),
                        )
            except Exception:
                continue
        await self.notify_receipts()

    async def notify_receipts(self):
        for oid, (tell, expires, failures) in list(self.receipts.items()):
            row = self.db.execute(
                "SELECT target,status,result FROM admin_ban_operations WHERE id=?",
                (oid,),
            ).fetchone()
            if row is None:
                self.receipts.pop(oid, None)
                continue
            target, state, raw = row
            if state == "accepted" and time.monotonic() < expires:
                continue
            try:
                await asyncio.wait_for(
                    tell(result_text(json.loads(raw), target, state)), 15
                )
            except Exception:
                logging.getLogger(__name__).warning(
                    "WARDOGS administrator ban result delivery unavailable; preserving audit."
                )
                if failures < 2 and time.monotonic() < expires:
                    self.receipts[oid] = (tell, expires, failures + 1)
                    continue
            self.receipts.pop(oid, None)

    def close(self):
        self.receipts.clear()
