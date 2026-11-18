"""Scoped administrator registration in the existing cyclic punishment policy."""

import asyncio
import json
import logging
import re
import time

from .admin_history import actor, valid_time

ID = re.compile(r"[0-9]{17}", re.ASCII)


def cycle_request(text):
    value = text.strip().rstrip("。！!")
    if value.endswith(("？", "?")):
        return None
    match = re.fullmatch(r"(?:请\s*|麻烦\s*)?循环\s*(.*)", value)
    if not match:
        return None
    query = match[1].strip()
    ids = re.findall(r"(?<![A-Za-z0-9])[0-9]{17}(?![A-Za-z0-9])", query)
    return {
        "query": query,
        "clarify": not query
        or len(query) > 80
        or len(ids) > 1
        or bool(ids and not ID.fullmatch(query)),
    }


def cycle_confirmation(text):
    value = text.strip().rstrip("。！!")
    if re.fullmatch(r"[1-9][0-9]{0,3}", value) or ID.fullmatch(value):
        return value
    match = re.fullmatch(r"确认循环\s*(?:第\s*)?([0-9]{1,17})\s*(?:号|个)?", value)
    return match[1] if match else None


class CycleSupport:
    def __init__(self, store, client):
        self.db, self.client = store.db, client
        self.receipts = {}
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS admin_cycle_drafts(
                  platform TEXT,grp TEXT,sender TEXT,candidates TEXT,expires REAL,
                  PRIMARY KEY(platform,grp,sender));
                CREATE TABLE IF NOT EXISTS admin_cycle_operations(
                  id TEXT PRIMARY KEY,target TEXT,status TEXT,created REAL,
                  payload TEXT,result TEXT,operation_id TEXT);
            """)
            self.db.execute(
                "UPDATE admin_cycle_operations SET status='outcome_unknown' WHERE status='dispatching'"
            )

    def clear(self, scope):
        with self.db:
            self.db.execute(
                "DELETE FROM admin_cycle_drafts WHERE platform=? AND grp=? AND sender=?",
                scope,
            )

    def snapshot(self, scope, at):
        row = self.db.execute(
            "SELECT candidates,expires FROM admin_cycle_drafts WHERE platform=? AND grp=? AND sender=?",
            scope,
        ).fetchone()
        return tuple(row) if row and at >= row[1] - 120 else None

    def existing(self, oid):
        return self.db.execute(
            "SELECT status FROM admin_cycle_operations WHERE id=?", (oid,)
        ).fetchone()

    def held(self, target):
        return self.db.execute(
            "SELECT id FROM admin_cycle_operations WHERE target=? AND status IN ('dispatching','accepted','outcome_unknown')",
            (target,),
        ).fetchone()

    @staticmethod
    def result_state(result, target, operation=None):
        if (
            not isinstance(result, dict)
            or result.get("steamId") != target
            or (result.get("source") != "admin" or result.get("action") != "cycle_add")
        ):
            raise ValueError("invalid_cycle_receipt")
        oid = result.get("operationId")
        if (
            not isinstance(oid, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", oid)
            or (operation is not None and oid != operation)
        ):
            raise ValueError("invalid_cycle_operation")
        if result.get("cycleStatus") == "registered":
            if (
                result.get("status") != "completed"
                or not valid_time(result.get("registeredAt"))
                or type(result.get("alreadyEnabled")) is not bool
                or type(result.get("onlineAtCheck")) is not bool
            ):
                raise ValueError("unconfirmed_cycle_registration")
            return oid, "enabled"
        if result.get("status") in (
            "failed",
            "rejected",
            "cancelled",
            "superseded",
            "unknown",
        ):
            return oid, result["status"]
        return oid, "accepted"

    async def handle(self, controller, message, role, oid, request, snapshot, tell):
        from .admin import fresh_players

        scope = (message.who.platform, message.who.group, message.who.sender)
        confirm = request is None
        if not confirm:
            controller.clear_drafts(scope)
            if request["clarify"]:
                await tell(
                    "循环目标不明确，未执行；请发送“循环 玩家昵称或精确SteamID64”。"
                )
                return True
            query = request["query"]
        else:
            current = self.snapshot(scope, message.at)
            if snapshot is None or current != snapshot or snapshot[1] < time.time():
                await tell(
                    "循环候选尚未就绪、已更新或已过期；请重新发送“循环 玩家昵称”。"
                )
                return True
            try:
                candidates = json.loads(snapshot[0])
                value = cycle_confirmation(message.text)
                if ID.fullmatch(value or ""):
                    choices = [p for p in candidates if p["steamId"] == value]
                    if len(choices) != 1:
                        raise ValueError("unknown_target")
                    query = value
                else:
                    number = int(value)
                    if not 1 <= number <= len(candidates):
                        raise ValueError("invalid_selection")
                    query = candidates[number - 1]["steamId"]
            except (TypeError, ValueError, KeyError):
                await tell("循环候选序号或ID无效，未执行。")
                return True
        try:
            online = fresh_players(await self.client.players())
            directory, unavailable = await controller.history.lookup(
                [query], message.who, role, online
            )
        except Exception:
            await tell("玩家名单不可核验，暂不加入循环惩罚名单。")
            return True
        exact = (
            [p for p in directory if p["steamId"] == query]
            if ID.fullmatch(query)
            else []
        )
        if len(exact) == 1 and (
            exact[0].get("online", True) or (confirm and not unavailable)
        ):
            target = exact[0]["steamId"]
        else:
            if confirm:
                self.clear(scope)
                await tell("循环候选身份暂不可核验，未执行；请重新发起请求。")
                return True
            matches = exact or controller.history.matches(directory, [query.casefold()])
            if unavailable and (
                not matches or any(not p.get("online", True) for p in matches)
            ):
                await tell("历史玩家检索暂不可核验，未加入循环名单。")
                return True
            if not matches:
                await tell(
                    "在线和历史记录中均未匹配到循环目标，请提供精确SteamID64或历史昵称。"
                )
                return True
            matches = matches[:5]
            display = [
                f"{i}. "
                + re.sub(r"[@\x00-\x1f\x7f]", "", str(p.get("name", "")))[:40]
                + " "
                + p["steamId"]
                + (" [离线；上线后生效]" if not p.get("online", True) else "")
                for i, p in enumerate(matches, 1)
            ]
            await tell(
                "循环惩罚候选，尚未加入名单：\n"
                + "\n".join(display)
                + "\n确认将按服务器现有规则循环惩罚。120秒内由本次管理员@同一机器人回复序号，或“确认循环 1”；也可发送候选精确SteamID64。"
            )
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO admin_cycle_drafts VALUES(?,?,?,?,?)",
                    (*scope, json.dumps(matches), time.time() + 120),
                )
            return True
        if (
            self.held(target)
            or controller.bans.held(target)
            or controller.unban.held(target)
            or controller.history.held(target)
            or self.db.execute(
                "SELECT id FROM admin_kick_operations WHERE target=? AND status IN ('dispatching','accepted','outcome_unknown')",
                (target,),
            ).fetchone()
        ):
            await tell("该玩家存在待核实的处置记录，暂不重复提交。")
            return True
        controller.clear_drafts(scope)
        payload = {
            "requestId": oid,
            "steamId": target,
            "actor": actor(message.who, role),
            "requestedAt": time.time(),
        }
        with self.db:
            self.db.execute(
                "INSERT INTO admin_cycle_operations VALUES(?,?,'dispatching',?,?,?,NULL)",
                (oid, target, time.time(), json.dumps(payload), "{}"),
            )
        try:
            result = await self.client.admin_cycle_add(payload, "wd-cycle-" + oid)
            operation, state = self.result_state(result, target)
            with self.db:
                self.db.execute(
                    "UPDATE admin_cycle_operations SET status=?,result=?,operation_id=? WHERE id=?",
                    (state, json.dumps(result), operation, oid),
                )
        except BaseException as error:
            with self.db:
                self.db.execute(
                    "UPDATE admin_cycle_operations SET status='outcome_unknown' WHERE id=?",
                    (oid,),
                )
            if isinstance(error, asyncio.CancelledError):
                raise
            await tell("循环名单登记结果待核实，请勿重复提交。")
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
        for oid, operation, target in self.db.execute(
            "SELECT id,operation_id,target FROM admin_cycle_operations WHERE status='accepted' ORDER BY created LIMIT 10"
        ).fetchall():
            try:
                result = await self.client.admin_operation(operation)
                _, state = self.result_state(result, target, operation)
                if state != "accepted":
                    with self.db:
                        self.db.execute(
                            "UPDATE admin_cycle_operations SET status=?,result=? WHERE id=? AND status='accepted'",
                            (state, json.dumps(result), oid),
                        )
            except Exception:
                continue
        await self.notify_receipts()

    async def notify_receipts(self):
        for oid, (notify, expires, failures) in list(self.receipts.items()):
            row = self.db.execute(
                "SELECT target,status FROM admin_cycle_operations WHERE id=?", (oid,)
            ).fetchone()
            if row is None:
                self.receipts.pop(oid, None)
                continue
            target, state = row
            prefix = {
                "enabled": "已加入循环惩罚名单；在线时按服务器既有规则生效：",
                "failed": "循环名单登记失败：",
                "rejected": "循环名单登记被拒绝：",
                "cancelled": "循环名单登记已取消：",
                "superseded": "循环名单登记被其他操作替代，请核实：",
                "unknown": "循环名单登记结果待核实，请勿重复提交：",
                "outcome_unknown": "循环名单登记结果待核实，请勿重复提交：",
            }.get(state)
            if prefix is None:
                if time.monotonic() < expires:
                    continue
                prefix = "循环名单登记仍待核实，请勿重复提交："
            try:
                await asyncio.wait_for(notify(prefix + target), 15)
            except Exception:
                logging.getLogger(__name__).warning(
                    "WARDOGS cycle result reply unavailable; preserving audit."
                )
                if failures < 2 and time.monotonic() < expires:
                    self.receipts[oid] = (notify, expires, failures + 1)
                    continue
            self.receipts.pop(oid, None)

    def close(self):
        self.receipts.clear()
