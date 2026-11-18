"""Scoped administrator release of native bans and local rejoin restrictions."""

import asyncio
import json
import logging
import re
import time

from .admin_history import actor

ID = re.compile(r"[0-9]{17}", re.ASCII)


def unban_request(text):
    value = text.strip().rstrip("。！!")
    if value.endswith(("？", "?")):
        return None
    match = re.fullmatch(
        r"(?:请\s*|麻烦\s*)?(?:取消封禁|解除封禁|取消封号|解封)\s*(.*)", value
    )
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


def unban_confirmation(text):
    value = text.strip().rstrip("。！!")
    if re.fullmatch(r"[1-9][0-9]{0,3}", value) or ID.fullmatch(value):
        return value
    match = re.fullmatch(r"确认解封\s*(?:第\s*)?([0-9]{1,17})\s*(?:号|个)?", value)
    return match[1] if match else None


class UnbanSupport:
    def __init__(self, store, client):
        self.db, self.client = store.db, client
        self.receipts = {}
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS admin_unban_drafts(
                  platform TEXT,grp TEXT,sender TEXT,candidates TEXT,expires REAL,
                  PRIMARY KEY(platform,grp,sender));
                CREATE TABLE IF NOT EXISTS admin_unban_operations(
                  id TEXT PRIMARY KEY,target TEXT,status TEXT,created REAL,
                  payload TEXT,result TEXT,operation_id TEXT);
            """)
            self.db.execute(
                "UPDATE admin_unban_operations SET status='outcome_unknown' WHERE status='dispatching'"
            )

    def clear(self, scope):
        with self.db:
            self.db.execute(
                "DELETE FROM admin_unban_drafts WHERE platform=? AND grp=? AND sender=?",
                scope,
            )

    def snapshot(self, scope, at):
        row = self.db.execute(
            "SELECT candidates,expires FROM admin_unban_drafts WHERE platform=? AND grp=? AND sender=?",
            scope,
        ).fetchone()
        return tuple(row) if row and at >= row[1] - 120 else None

    def existing(self, oid):
        return self.db.execute(
            "SELECT status FROM admin_unban_operations WHERE id=?", (oid,)
        ).fetchone()

    def held(self, target):
        return self.db.execute(
            "SELECT id FROM admin_unban_operations WHERE target=? AND status IN ('dispatching','accepted','outcome_unknown','unknown','partial')",
            (target,),
        ).fetchone()

    @staticmethod
    def result_state(result, target, operation=None):
        if (
            not isinstance(result, dict)
            or result.get("steamId") != target
            or (result.get("source") != "admin" or result.get("action") != "unban")
        ):
            raise ValueError("invalid_unban_receipt")
        oid = result.get("operationId")
        if (
            not isinstance(oid, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", oid)
            or (operation is not None and oid != operation)
        ):
            raise ValueError("invalid_unban_operation")
        if result.get("unbanStatus") == "unbanned":
            if (
                result.get("status") != "completed"
                or result.get("nativeUnbanStatus")
                not in ("unbanned", "already_unbanned")
                or result.get("localReleaseStatus")
                not in ("released", "already_released", "not_restricted")
            ):
                raise ValueError("unconfirmed_unban")
            return oid, "released"
        if result.get("status") == "partial" or result.get("unbanStatus") == "partial":
            return oid, "partial"
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
                    "解封目标不明确，未执行；请发送“解封 玩家昵称或精确SteamID64”。"
                )
                return True
            query = request["query"]
        else:
            current = self.snapshot(scope, message.at)
            if snapshot is None or current != snapshot or snapshot[1] < time.time():
                await tell(
                    "解封候选尚未就绪、已更新或已过期；请重新发送“解封 玩家昵称”。"
                )
                return True
            try:
                candidates = json.loads(snapshot[0])
                value = unban_confirmation(message.text)
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
                await tell("解封候选序号或ID无效，未执行。")
                return True
        try:
            online = fresh_players(await self.client.players())
            search = getattr(self.client, "admin_unban_search", None)
            if callable(search):

                async def normalized_search(payload):
                    data = await search(payload)
                    if (
                        data.get("complete") is not True
                        or not isinstance(data.get("players"), list)
                        or type(data.get("truncated")) is not bool
                    ):
                        raise ValueError("incomplete_unban_directory")
                    if any(
                        not isinstance(p, dict)
                        or (
                            p.get("online") is not None
                            and type(p["online"]) is not bool
                        )
                        for p in data["players"]
                    ):
                        raise ValueError("invalid_unban_online_state")
                    online_ids = {p["steamId"] for p in online}
                    return dict(
                        data,
                        historyComplete=True,
                        players=[
                            dict(p, online=p.get("steamId") in online_ids)
                            for p in data["players"]
                        ],
                    )
            else:
                normalized_search = None
            directory, unavailable = await controller.history.lookup(
                [query], message.who, role, online, search=normalized_search
            )
        except Exception:
            await tell("玩家名单不可核验，暂不解封。")
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
                await tell("解封候选身份暂不可核验，未执行；请重新发起请求。")
                return True
            matches = exact or controller.history.matches(directory, [query.casefold()])
            if unavailable and (
                not matches or any(not p.get("online", True) for p in matches)
            ):
                await tell("历史玩家检索暂不可核验，未解封。")
                return True
            if not matches:
                await tell(
                    "在线和历史记录中均未匹配到解封目标，请提供精确SteamID64或历史昵称。"
                )
                return True
            matches = matches[:5]
            display = [
                f"{i}. "
                + re.sub(r"[@\x00-\x1f\x7f]", "", str(p.get("name", "")))[:40]
                + " "
                + p["steamId"]
                + (" [离线历史]" if not p.get("online", True) else "")
                for i, p in enumerate(matches, 1)
            ]
            await tell(
                "解封候选，尚未解封：\n"
                + "\n".join(display)
                + "\n确认解除游戏原生封禁和本地重入限制。120秒内由本次管理员@同一机器人回复序号，或“确认解封 1”；也可发送候选精确SteamID64。"
            )
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO admin_unban_drafts VALUES(?,?,?,?,?)",
                    (*scope, json.dumps(matches), time.time() + 120),
                )
            return True
        if (
            self.held(target)
            or controller.bans.held(target)
            or controller.cycle.held(target)
            or controller.history.held(target)
            or self.db.execute(
                "SELECT id FROM admin_kick_operations WHERE target=? AND status IN ('dispatching','accepted','outcome_unknown','unknown','partial')",
                (target,),
            ).fetchone()
        ):
            await tell("该玩家存在待核实的处置记录，暂不重复提交解封。")
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
                "INSERT INTO admin_unban_operations VALUES(?,?,'dispatching',?,?,?,NULL)",
                (oid, target, time.time(), json.dumps(payload), "{}"),
            )
        try:
            result = await self.client.admin_unban(payload, "wd-unban-" + oid)
            operation, state = self.result_state(result, target)
            with self.db:
                self.db.execute(
                    "UPDATE admin_unban_operations SET status=?,result=?,operation_id=? WHERE id=?",
                    (state, json.dumps(result), operation, oid),
                )
        except BaseException as error:
            with self.db:
                self.db.execute(
                    "UPDATE admin_unban_operations SET status='outcome_unknown' WHERE id=?",
                    (oid,),
                )
            if isinstance(error, asyncio.CancelledError):
                raise
            await tell("解封结果待核实，请勿重复提交。")
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
            "SELECT id,operation_id,target FROM admin_unban_operations WHERE status='accepted' ORDER BY created LIMIT 10"
        ).fetchall():
            try:
                result = await self.client.admin_operation(operation)
                _, state = self.result_state(result, target, operation)
                if state != "accepted":
                    with self.db:
                        self.db.execute(
                            "UPDATE admin_unban_operations SET status=?,result=? WHERE id=? AND status='accepted'",
                            (state, json.dumps(result), oid),
                        )
            except Exception:
                continue
        await self.notify_receipts()

    async def notify_receipts(self):
        for oid, (notify, expires, failures) in list(self.receipts.items()):
            row = self.db.execute(
                "SELECT target,status,result FROM admin_unban_operations WHERE id=?",
                (oid,),
            ).fetchone()
            if row is None:
                self.receipts.pop(oid, None)
                continue
            target, state, raw_result = row
            prefix = {
                "released": "已解封，原生封禁和本地重入限制均已解除：",
                "partial": "部分解封，尚未确认全部解除，请核实：",
                "failed": "解封失败：",
                "rejected": "解封被拒绝：",
                "cancelled": "解封已取消：",
                "superseded": "解封被其他操作替代，请核实：",
                "unknown": "解封结果待核实，请勿重复提交：",
                "outcome_unknown": "解封结果待核实，请勿重复提交：",
            }.get(state)
            if state in ("superseded", "rejected", "failed"):
                try:
                    blocked = json.loads(raw_result)
                except (TypeError, ValueError):
                    blocked = {}
                error = blocked.get("error") if isinstance(blocked, dict) else None
                if isinstance(error, dict):
                    prefix = {
                        "target_already_coordinated": (
                            "该玩家的其他处置正在执行或结果尚未核实，本次未解封："
                        ),
                        "unknown_punishment_not_reconcilable": (
                            "该玩家的旧处罚结果仍无法安全核实，本次未解封，需人工核实："
                        ),
                        "unknown_handoff_revalidation_failed": (
                            "该玩家的处置状态已变化，无法安全处理，本次未解封："
                        ),
                    }.get(error.get("code"), prefix)
            if state == "partial":
                try:
                    result = json.loads(raw_result)
                except (TypeError, ValueError):
                    result = {}
                local = {
                    "released": "本地重入限制已解除",
                    "already_released": "本地重入限制此前已解除",
                    "not_restricted": "原无本地重入限制",
                }.get(result.get("localReleaseStatus"), "本地重入限制结果待核实")
                native = {
                    "unbanned": "原生封禁已解除",
                    "already_unbanned": "原无原生封禁",
                    "failed": "原生解封失败",
                }.get(result.get("nativeUnbanStatus"), "原生解封结果待核实")
                prefix = "解封未全部完成；" + local + "；" + native + "："
            if prefix is None:
                if time.monotonic() < expires:
                    continue
                prefix = "解封仍待核实，请勿重复提交："
            try:
                await asyncio.wait_for(notify(prefix + target), 15)
            except Exception:
                logging.getLogger(__name__).warning(
                    "WARDOGS unban result reply unavailable; preserving audit."
                )
                if failures < 2 and time.monotonic() < expires:
                    self.receipts[oid] = (notify, expires, failures + 1)
                    continue
            self.receipts.pop(oid, None)

    def close(self):
        self.receipts.clear()
