"""Fresh ban state and explicitly confirmed multi-target offline restrictions."""

import asyncio
import hashlib
import json
import re
import time

from .admin_history import actor


def confirmation(text):
    value = text.strip().rstrip("。！!")
    cancel = re.fullmatch(r"取消批量补封(?:\s+([a-f0-9]{12}))?", value)
    if cancel:
        return {"ordinals": None, "code": cancel[1], "cancel": True}
    match = re.fullmatch(r"确认补封全部(?:\s+([a-f0-9]{12}))?", value)
    if match:
        return {"ordinals": None, "code": match[1]}
    match = re.fullmatch(r"(?:确认)?补封\s+([0-9]+(?:\s+[0-9]+)*)", value)
    if not match:
        return None
    if re.fullmatch(r"[0-9]{17}", match[1]):
        return None
    parts = match[1].split()
    if len(parts) > 20 or any(len(p) > 4 or not 1 <= int(p) <= 9999 for p in parts):
        return {"ordinals": [], "code": None}
    return {"ordinals": list(dict.fromkeys(map(int, parts))), "code": None}


async def ban_state(client, who, role, target):
    data = await asyncio.wait_for(
        client.current_restrictions(
            {
                "steamIds": [target],
                "actor": actor(who, role),
                "requestedAt": time.time(),
            }
        ),
        15,
    )

    def fresh(value):
        return type(value) in (int, float) and 0 <= time.time() - value <= 15

    if (
        not isinstance(data, dict)
        or not fresh(data.get("observedAt"))
        or data.get("complete") is not True
        or not isinstance(data.get("players"), list)
    ):
        raise ValueError("ban_state_incomplete")
    rows = data["players"]
    if (
        len(rows) != 1
        or not isinstance(rows[0], dict)
        or rows[0].get("steamId") != target
    ):
        raise ValueError("ban_state_identity_unverified")
    row = rows[0]
    if any(
        type(row.get(key)) is not bool
        for key in ("online", "localRestricted", "nativeBanned", "sourceComplete")
    ) or any(
        not fresh(row.get(key))
        for key in ("onlineObservedAt", "localObservedAt", "nativeObservedAt")
    ):
        raise ValueError("ban_state_invalid")
    auto = row.get("currentAutomaticRestriction")
    if auto is not None and type(auto) is not bool:
        raise ValueError("ban_origin_invalid")
    sources = []
    for key in ["localSource", "nativeSource"]:
        source = row.get(key)
        if not isinstance(source, dict) or source.get("category") not in (
            "none",
            "automatic",
            "manual",
            "report",
            "unknown",
        ):
            raise ValueError("ban_origin_invalid")
        sources.append(source["category"])
    banned = row["localRestricted"] or row["nativeBanned"]
    automatic = (row["localRestricted"] and sources[0] == "automatic") or (
        row["nativeBanned"] and sources[1] == "automatic"
    )
    if (
        (auto is True and not automatic)
        or (automatic and auto is not True)
        or (not banned and auto is not False)
    ):
        raise ValueError("ban_origin_inconsistent")
    if not banned and row.get("nativeSyncState") is not None:
        raise ValueError("ban_state_pending")
    return {
        "banned": banned,
        "automatic": auto is True,
        "online": row["online"],
        "source_unknown": auto is None,
    }


def banned_label(state):
    return (
        "已被智能检测自动封禁，无需补封。"
        if state.get("automatic")
        else "已封禁，来源未能确认，无需重复补封。"
        if state.get("source_unknown")
        else "已封禁，无需重复补封。"
    )


class RestrictionOffer(str):
    def __new__(cls, text, code):
        result = super().__new__(cls, text)
        result.restriction_code = code
        return result


class RestrictionBatches:
    def __init__(self, store):
        self.db = store.db
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS admin_batch_restriction_drafts(id TEXT PRIMARY KEY,platform TEXT,grp TEXT,sender TEXT,created REAL,published REAL,expires REAL,status TEXT,items TEXT)"
            )

    def clear(self, scope):
        with self.db:
            self.db.execute(
                "UPDATE admin_batch_restriction_drafts SET status='superseded' WHERE platform=? AND grp=? AND sender=? AND status='draft'",
                scope,
            )

    def offer(self, scope, oid, items):
        if not items:
            return ""
        self.clear(scope)
        with self.db:
            self.db.execute(
                "INSERT INTO admin_batch_restriction_drafts VALUES(?,?,?,?,?,0,0,'draft',?)",
                (oid, *scope, time.time(), json.dumps(items)),
            )
        lines = ["离线补封候选："]
        for item in items:
            lines.append(
                "序号" + "、".join(map(str, item["ordinals"])) + "：" + item["target"]
            )
        lines.append(
            "120秒内由本次管理员@同一机器人回复“确认补封全部”统一补封，或“补封 3 5”选择原序号。仅登记永久重入限制，不表示本次踢出或原生封禁成功。"
        )
        return RestrictionOffer("\n".join(lines), oid[:12])

    def publish(self, oid):
        now = time.time()
        with self.db:
            self.db.execute(
                "UPDATE admin_batch_restriction_drafts SET published=?,expires=? WHERE id=? AND status='draft'",
                (now, now + 120, oid),
            )

    def snapshot(self, scope, at):
        row = self.db.execute(
            "SELECT id,items,published,expires FROM admin_batch_restriction_drafts WHERE platform=? AND grp=? AND sender=? AND status='draft' ORDER BY created DESC LIMIT 1",
            scope,
        ).fetchone()
        if row and row[2] > 0 and row[2] <= at <= row[3] and time.time() <= row[3]:
            return tuple(row)
        return None

    async def handle(
        self, controller, message, role, oid, request, snapshot, tell, role_check
    ):
        from .admin import fresh_players

        scope = (message.who.platform, message.who.group, message.who.sender)
        current = self.snapshot(scope, message.at)
        if (
            snapshot is None
            or current != snapshot
            or (request["code"] and request["code"] != current[0][:12])
        ):
            await tell(
                "没有属于本次管理员、机器人和群的有效补封候选，或列表已更新；请重新回溯并选择。"
            )
            return True
        if request.get("cancel"):
            with self.db:
                self.db.execute(
                    "UPDATE admin_batch_restriction_drafts SET status='cancelled' WHERE id=? AND status='draft'",
                    (current[0],),
                )
            await tell("本批补封已取消，未提交补封。")
            return True
        items = json.loads(current[1])
        wanted = request["ordinals"]
        available = {n for item in items for n in item["ordinals"]}
        if wanted is not None and (not wanted or not set(wanted) <= available):
            await tell("补封序号无效，本次全部未提交；请使用补封候选中的原序号。")
            return True
        items = [
            item
            for item in items
            if wanted is None or set(item["ordinals"]) & set(wanted)
        ]
        with self.db:
            changed = self.db.execute(
                "UPDATE admin_batch_restriction_drafts SET status='confirmed' WHERE id=? AND status='draft' AND expires>=?",
                (current[0], time.time()),
            ).rowcount
            if changed:
                self.db.execute(
                    "INSERT INTO admin_kick_batches VALUES(?,?,?,?,?,'running',?,'{}')",
                    (oid, *scope, time.time(), json.dumps(items)),
                )
        if not changed:
            await tell("此批补封已确认或过期，请核实原结果。")
            return True
        controller.clear_drafts(scope)
        info = {
            "items": items,
            "results": {},
            "accepted": set(),
            "notify": tell,
            "ready": False,
            "expires": time.monotonic() + 300,
            "failures": 0,
            "sent_chunks": 0,
            "kind": "restriction",
        }
        controller.batch.receipts[oid] = info

        def remember(target, text):
            info["results"][target] = text
            with self.db:
                self.db.execute(
                    "UPDATE admin_kick_batches SET results=? WHERE id=?",
                    (json.dumps(info["results"]), oid),
                )

        try:
            for item in items:
                target = item["target"]

                async def child(text, target=target):
                    if text.startswith("已受理"):
                        info["accepted"].add(target)
                    else:
                        remember(target, text)
                    if info["ready"]:
                        await controller.batch.flush(oid)

                current_role = role
                if role_check is not None:
                    try:
                        current_role = await role_check()
                    except Exception:
                        current_role = ""
                if current_role not in ("owner", "admin"):
                    remember(target, "管理员权限不可核验，未补封。")
                    continue
                try:
                    players = fresh_players(await controller.client.players())
                    state = await ban_state(
                        controller.client, message.who, current_role, target
                    )
                except Exception:
                    remember(target, "在线或封禁状态不可核验，未补封。")
                    continue
                if state["banned"]:
                    remember(target, banned_label(state))
                    continue
                if state["online"] or any(p["steamId"] == target for p in players):
                    remember(target, "玩家已上线，未补封也未自动改为踢出；请重新选择。")
                    continue
                if controller.pending_target(target):
                    remember(target, "已有待核实处置，未重复补封。")
                    continue
                child_id = hashlib.sha256(
                    json.dumps(["batch-restrict", oid, target]).encode()
                ).hexdigest()
                await controller.history.submit(
                    message, current_role, target, child_id, child
                )
            with self.db:
                self.db.execute(
                    "UPDATE admin_kick_batches SET status='waiting' WHERE id=?", (oid,)
                )
            await tell(
                f"批量补封已受理{len(info['accepted'])}名；实际登记结果将汇总回复。"
            )
            info["ready"] = True
            await controller.batch.flush(oid)
        except BaseException:
            with self.db:
                self.db.execute(
                    "UPDATE admin_kick_batches SET status='interrupted' WHERE id=?",
                    (oid,),
                )
            raise
        return True
