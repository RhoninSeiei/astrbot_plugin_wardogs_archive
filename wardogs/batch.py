"""Bounded multi-ordinal kicks bound to one immutable administrator proposal."""

import asyncio
import hashlib
import json
import logging
import re
import time

MAX_SELECTIONS = 20


def batch_selection(text):
    value = text.strip().rstrip("。！!")
    value = re.sub(
        r"^(?:确认(?:踢出|踢掉|踢|封禁|ban|kick)?|踢出|踢掉|踢|封禁|ban|kick)\s*",
        "",
        value,
        flags=re.I,
    )
    if not re.fullmatch(r"[0-9]+(?:\s+[0-9]+)+", value):
        return None
    parts = value.split()
    if len(parts) > MAX_SELECTIONS or any(
        len(p) > 4 or not 1 <= int(p) <= 9999 for p in parts
    ):
        return []
    return list(dict.fromkeys(int(p) for p in parts))


class BatchKicks:
    def __init__(self, store):
        self.db = store.db
        self.receipts = {}
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS admin_kick_batches(id TEXT PRIMARY KEY,platform TEXT,grp TEXT,sender TEXT,created REAL,status TEXT,plan TEXT,results TEXT)"
            )
            self.db.execute(
                "UPDATE admin_kick_batches SET status='interrupted' WHERE status IN ('prepared','running','waiting')"
            )

    def seen(self, oid):
        return self.db.execute(
            "SELECT status FROM admin_kick_batches WHERE id=?", (oid,)
        ).fetchone()

    async def handle(
        self,
        controller,
        message,
        role,
        oid,
        ordinals,
        replay_snapshot,
        kick_snapshot,
        tell,
        role_check=None,
    ):
        from .admin import fresh_players
        from .batch_restrictions import ban_state, banned_label

        scope = (message.who.platform, message.who.group, message.who.sender)
        if role not in ("owner", "admin"):
            await tell("批量踢出需要平台可核验的群主或管理员身份。")
            return True
        if self.seen(oid):
            await tell("此批量指令已记录，请核实原操作，勿重复提交。")
            return True
        with self.db:
            self.db.execute(
                "INSERT INTO admin_kick_batches VALUES(?,?,?,?,?,'prepared','[]','{}')",
                (oid, *scope, time.time()),
            )
        plan = {}
        error = None
        if not ordinals:
            error = "一次最多选择20个序号，序号须为有效正整数；本次未执行。"
        elif replay_snapshot is not None:
            for number in ordinals:
                target, error = controller.replay.select(scope, replay_snapshot, number)
                if error:
                    break
                item = plan.setdefault(
                    target, {"target": target, "ordinals": [], "offline_hint": False}
                )
                item["ordinals"].append(number)
        elif kick_snapshot is not None:
            current = self.db.execute(
                "SELECT candidates,expires FROM admin_kick_drafts WHERE platform=? AND grp=? AND sender=?",
                scope,
            ).fetchone()
            if (
                current is None
                or tuple(current) != kick_snapshot
                or current[1] < time.time()
            ):
                error = "踢出候选已更新或过期，请重新发起请求。"
            else:
                ids = controller.history.candidate_ids(current[0])
                stored = json.loads(current[0])
                for number in ordinals:
                    if not 1 <= number <= len(ids):
                        error = "候选序号不存在，本次全部未执行。"
                        break
                    target = ids[number - 1]
                    item = plan.setdefault(
                        target,
                        {
                            "target": target,
                            "ordinals": [],
                            "offline_hint": (
                                "offline:" + target in stored
                                or controller.history.action(scope, current[1], target)
                                == "restriction"
                            ),
                        },
                    )
                    item["ordinals"].append(number)
        else:
            error = "批量踢出需要本次管理员尚未过期的踢出候选或回溯列表；不适用于循环或解封候选。"
        if error:
            with self.db:
                self.db.execute(
                    "UPDATE admin_kick_batches SET status='rejected',results=? WHERE id=?",
                    (json.dumps({"error": error}), oid),
                )
            await tell(error)
            return True
        items = list(plan.values())
        with self.db:
            self.db.execute(
                "UPDATE admin_kick_batches SET status='running',plan=? WHERE id=?",
                (json.dumps(items), oid),
            )
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
        }
        self.receipts[oid] = info

        def remember(target, value):
            info["results"][target] = value
            with self.db:
                self.db.execute(
                    "UPDATE admin_kick_batches SET results=? WHERE id=?",
                    (json.dumps(info["results"]), oid),
                )

        offline_items = []
        try:
            for item in items:
                target = item["target"]

                async def child_notify(text, target=target):
                    if text == "已受理":
                        info["accepted"].add(target)
                    else:
                        remember(target, text)
                    if info["ready"]:
                        await self.flush(oid)

                current_role = role
                if role_check is not None:
                    try:
                        current_role = await role_check()
                    except Exception:
                        current_role = ""
                if current_role not in ("owner", "admin"):
                    remember(target, "管理员权限不可核验，未提交踢出：" + target)
                    continue
                try:
                    players = fresh_players(await controller.client.players())
                except Exception:
                    remember(target, "在线名单不可核验，未提交踢出：" + target)
                    continue
                online = len([p for p in players if p["steamId"] == target]) == 1
                if not online:
                    try:
                        state = await ban_state(
                            controller.client, message.who, current_role, target
                        )
                    except Exception:
                        remember(
                            target, "玩家已离线，封禁状态不可核验；未执行踢出或补封。"
                        )
                        continue
                    if state["banned"]:
                        remember(target, banned_label(state))
                    elif state["online"]:
                        remember(target, "玩家已上线，未自动执行；请重新选择。")
                    elif controller.pending_target(target):
                        remember(target, "已有待核实处置，未重复补封。")
                    else:
                        remember(target, "玩家已离线，尚未封禁；已列入统一补封候选。")
                        offline_items.append(item)
                    continue
                if item["offline_hint"]:
                    remember(
                        target, "原离线补封候选已上线，未执行；请重新发起在线请求。"
                    )
                    continue
                if controller.pending_target(target):
                    remember(target, "存在待核实处置，未重复提交：" + target)
                    continue
                child_id = hashlib.sha256(
                    json.dumps(["batch-kick", oid, target]).encode()
                ).hexdigest()
                await controller.submit_kick(
                    message, current_role, target, child_id, child_notify
                )
            with self.db:
                self.db.execute(
                    "UPDATE admin_kick_batches SET status='waiting' WHERE id=?", (oid,)
                )
            proposal = controller.offline_batch.offer(scope, oid, offline_items)
            accepted = len(info["accepted"])
            acknowledgement = (
                f"批量选择{len(items)}名玩家，已受理{accepted}名踢出请求；各人结果将汇总回复。"
                + ("\n" + proposal if proposal else "")
            )
            if proposal:
                acknowledgement = type(proposal)(
                    acknowledgement, proposal.restriction_code
                )
            await tell(acknowledgement)
            if proposal:
                controller.offline_batch.publish(oid)
            info["ready"] = True
            await self.flush(oid)
        except BaseException:
            with self.db:
                self.db.execute(
                    "UPDATE admin_kick_batches SET status='interrupted' WHERE id=?",
                    (oid,),
                )
            raise
        return True

    async def flush(self, oid):
        info = self.receipts.get(oid)
        if info is None or info.get("sending"):
            return
        info["sending"] = True
        try:
            await self._flush(oid)
        finally:
            info["sending"] = False

    async def _flush(self, oid):
        info = self.receipts.get(oid)
        if info is None or not info["ready"]:
            return
        if len(info["results"]) < len(info["items"]):
            if time.monotonic() < info["expires"]:
                return
            for item in info["items"]:
                info["results"].setdefault(
                    item["target"],
                    ("补封" if info.get("kind") == "restriction" else "踢出")
                    + "结果待核实，请勿重复提交："
                    + item["target"],
                )
        lines = [
            "批量补封结果：" if info.get("kind") == "restriction" else "批量踢出结果："
        ]
        for item in info["items"]:
            lines.append(
                "序号"
                + "、".join(map(str, item["ordinals"]))
                + "："
                + info["results"][item["target"]]
            )
        chunks = []
        chunk = ""
        for line in lines:
            if chunk and len(chunk) + len(line) + 1 > 1700:
                chunks.append(chunk)
                chunk = ""
            chunk += ("\n" if chunk else "") + line
        if chunk:
            chunks.append(chunk)
        try:
            for part in chunks[info["sent_chunks"] :]:
                await asyncio.wait_for(info["notify"](part), 15)
                info["sent_chunks"] += 1
        except Exception:
            info["failures"] += 1
            logging.getLogger(__name__).warning(
                "WARDOGS batch result reply unavailable; preserving audit."
            )
            if info["failures"] < 3 and time.monotonic() < info["expires"]:
                return
        with self.db:
            self.db.execute(
                "UPDATE admin_kick_batches SET status='completed',results=? WHERE id=?",
                (json.dumps(info["results"]), oid),
            )
        self.receipts.pop(oid, None)

    async def poll(self):
        for oid in list(self.receipts):
            await self.flush(oid)

    def close(self):
        self.receipts.clear()
