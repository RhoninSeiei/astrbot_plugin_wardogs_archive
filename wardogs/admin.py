"""Explicit administrator orders, scoped exact-ID confirmation and durable dispatch."""

import asyncio
import hashlib
import inspect
import json
import logging
import math
import re
import time
from difflib import SequenceMatcher

from .admin_history import HistorySupport
from .admin_intent import (
    DENIAL_CUE,
    explicit_order_attempt,
    interpret,
    selection_request,
)
from .admin_vision import interpret_image
from .bans import BanSupport
from .batch import BatchKicks, batch_selection
from .batch_restrictions import (
    RestrictionBatches,
)
from .batch_restrictions import (
    confirmation as restriction_confirmation,
)
from .cycle import CycleSupport, cycle_confirmation, cycle_request
from .cycle_list import CycleListSupport, list_command
from .player_kills import PlayerKillHistory, history_request, navigation
from .records import NEXT, RecordQueries, record_request, record_selection
from .replay import ReplayDrafts, replay_command
from .unban import UnbanSupport, unban_confirmation, unban_request

ID = re.compile(r"(?<![A-Za-z0-9])[0-9]{17}(?![A-Za-z0-9])")


def parse_request(text):
    text = text.strip().lstrip("/")
    if len(text) > 500 or re.search(
        r"不要|别|取消|撤回|怎么|如何|能否|如果|他说|有人说|示例|测试|模拟|[？?]", text
    ):
        return None
    if ID.fullmatch(text):
        return {"confirm": True, "query": text, "ids": [text], "ambiguous": False}
    text = re.sub(r"^wd\s+", "", text)
    confirm = text.startswith("确认")
    body = text[2:].strip() if confirm else re.sub(r"^(?:请|麻烦)\s*", "", text)
    match = re.fullmatch(r"(?:踢出|踢掉|踢了|补封|kick)\s*(.+)", body, re.I)
    if match:
        query = match[1]
    else:
        match = re.fullmatch(
            r"(?:把\s*)?(.+?)\s*(?:踢出|踢掉|踢了)(?:服务器|游戏|一下)?", body
        )
        if match:
            query = match[1]
        elif confirm and ID.fullmatch(body):
            query = body
        else:
            return None
    query = query.strip().rstrip("。！!")
    query = re.sub(r"^(?:玩家|用户|ID|id|SteamID|steamid)\s*[:：]?\s*", "", query)
    ids = ID.findall(query)
    return {
        "confirm": confirm,
        "query": query[:80],
        "ids": ids,
        "ambiguous": bool(ids) and ID.fullmatch(query) is None,
    }


def fresh_players(data):
    observed = data.get("observedAt")
    if (
        isinstance(observed, bool)
        or not isinstance(observed, (int, float))
        or not math.isfinite(observed)
        or not 0 <= time.time() - observed <= 90
    ):
        raise ValueError("players_not_fresh")
    rows = data.get("players")
    if not isinstance(rows, list) or len(rows) > 2000:
        raise ValueError("invalid_players")
    valid = [
        p
        for p in rows
        if isinstance(p, dict)
        and isinstance(p.get("steamId"), str)
        and re.fullmatch(r"[0-9]{17}", p["steamId"])
    ]
    if len({p["steamId"] for p in valid}) != len(valid):
        raise ValueError("duplicate_online_identity")
    return valid


class AdminKicks:
    def __init__(self, store, client):
        self.db = store.db
        self.client = client
        self.lock = asyncio.Lock()
        self.poll_lock = asyncio.Lock()
        self.receipts = {}
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS admin_kick_drafts(
                  platform TEXT, grp TEXT, sender TEXT, candidates TEXT,
                  expires REAL, PRIMARY KEY(platform,grp,sender));
                CREATE TABLE IF NOT EXISTS admin_kick_operations(
                  id TEXT PRIMARY KEY, platform TEXT, grp TEXT, sender TEXT,
                  target TEXT, status TEXT, created REAL, payload TEXT,
                  result TEXT, operation_id TEXT);
            """)
            self.db.execute(
                "UPDATE admin_kick_operations SET status='outcome_unknown' WHERE status='dispatching'"
            )

        self.bans = BanSupport(store, client)
        self.history = HistorySupport(store, client)
        self.replay = ReplayDrafts(store, client)
        self.cycle = CycleSupport(store, client)
        self.unban = UnbanSupport(store, client)
        self.cycle_list = CycleListSupport(store, client)
        self.batch = BatchKicks(store)
        self.offline_batch = RestrictionBatches(store)
        self.records = RecordQueries(store, client)
        self.kill_history = PlayerKillHistory(store, client)

    def clear_drafts(self, scope):
        self.kill_history.clear(scope)
        self.offline_batch.clear(scope)
        self.records.clear(scope)
        with self.db:
            self.db.execute(
                "DELETE FROM admin_kick_drafts WHERE platform=? AND grp=? AND sender=?",
                scope,
            )
        self.history.clear(scope)
        self.replay.clear(scope)
        self.cycle.clear(scope)
        self.records.clear(scope)
        self.unban.clear(scope)

    async def offer(self, scope, matches, tell):
        self.kill_history.clear(scope)
        self.offline_batch.clear(scope)
        self.records.clear(scope)
        self.unban.clear(scope)
        self.cycle.clear(scope)
        with self.db:
            self.db.execute(
                "DELETE FROM admin_kick_drafts WHERE expires<?", (time.time(),)
            )
            self.db.execute(
                "DELETE FROM admin_kick_drafts WHERE platform=? AND grp=? AND sender=?",
                scope,
            )
        self.history.clear(scope)
        self.replay.clear(scope)
        if not matches:
            await tell(
                "在线和历史记录中均未匹配到玩家，请提供可核验的精确SteamID64或历史昵称。"
            )
            return
        display = [
            f"{index}. "
            + re.sub(r"[@\x00-\x1f\x7f]", "", str(p.get("name", "")))[:40]
            + " "
            + p["steamId"]
            + (" [离线历史；确认后永久重入限制]" if not p.get("online", True) else "")
            for index, p in enumerate(matches, 1)
        ]
        suffix = ""
        if any(not p.get("online", True) for p in matches):
            suffix = "\n离线补封仅登记永久重入限制，之后上线再处理；不代表当前踢出或原生封禁成功。"
        await tell(
            "仅匹配到候选，尚未踢出：\n"
            + "\n".join(display)
            + suffix
            + "\n120秒内由本次发起管理员@同一机器人发送：直接回复序号（例如：1），或“确认踢出 1”；也可输入精确SteamID64。"
        )
        expires = time.time() + 120
        with self.db:
            self.db.execute(
                "INSERT INTO admin_kick_drafts VALUES(?,?,?,?,?)",
                (*scope, json.dumps(self.history.stored_ids(matches)), expires),
            )
        self.history.remember(scope, expires, matches)

    async def handle(
        self, message, role, notify, *, model=None, media=None, role_check=None
    ):
        if not message.text.strip() or len(message.text) > 1000:
            return False
        kill_order = history_request(message.text)
        kill_snapshot = self.kill_history.snapshot(
            (message.who.platform, message.who.group, message.who.sender), message.at
        )
        kill_route = kill_order is not None or (
            kill_snapshot is not None and navigation(message.text) is not None
        )
        record_order = record_request(message.text)
        record_snapshot = self.records.snapshot(
            (message.who.platform, message.who.group, message.who.sender), message.at
        )
        record_route = (
            record_order is not None
            or message.text.strip() in NEXT
            or (
                record_snapshot is not None
                and record_selection(message.text) is not None
            )
        )
        restriction_order = restriction_confirmation(message.text)
        restriction_snapshot = (
            self.offline_batch.snapshot(
                (message.who.platform, message.who.group, message.who.sender),
                message.at,
            )
            if restriction_order is not None
            else None
        )
        batch_ordinals = batch_selection(message.text)
        is_replay = replay_command(message.text)
        is_cycle_list = list_command(message.text)
        cycle_order = cycle_request(message.text)
        unban_order = unban_request(message.text)
        image_sources = message.images + message.reply_images
        request = None if image_sources else selection_request(message.text)
        if request is None and model is None and not image_sources:
            request = parse_request(message.text)
        if (
            model is None
            and request is None
            and batch_ordinals is None
            and restriction_order is None
            and not record_route
            and not kill_route
            and not is_replay
            and not is_cycle_list
            and cycle_order is None
            and unban_order is None
            and not message.text.strip().startswith("确认解封")
            and not message.text.strip().startswith("确认循环")
        ):
            return False

        who = message.who
        scope = (who.platform, who.group, who.sender)
        unban_snapshot = self.unban.snapshot(scope, message.at)
        unban_route = (
            unban_order is not None
            or message.text.strip().startswith("确认解封")
            or (
                unban_snapshot is not None
                and unban_confirmation(message.text) is not None
            )
        )
        cycle_snapshot = self.cycle.snapshot(scope, message.at)
        cycle_route = (
            cycle_order is not None
            or message.text.strip().startswith("确认循环")
            or (
                cycle_snapshot is not None
                and cycle_confirmation(message.text) is not None
            )
        )
        # Capture before waiting on the controller lock or any model/network I/O.
        selection_snapshot = None
        replay_snapshot = None
        if (
            request is not None and "selection" in request
        ) or batch_ordinals is not None:
            replay_row = self.replay.snapshot(scope)
            if replay_row and message.at >= replay_row[1] - 120:
                replay_snapshot = replay_row
            row = self.db.execute(
                "SELECT candidates,expires FROM admin_kick_drafts WHERE platform=? AND grp=? AND sender=?",
                scope,
            ).fetchone()
            if row and message.at >= row[1] - 120:
                selection_snapshot = tuple(row)

        async def tell(text):
            value = notify(text)
            if inspect.isawaitable(value):
                await value

        if role not in ("owner", "admin"):
            if (
                model is not None
                and not is_replay
                and not record_route
                and not kill_route
                and batch_ordinals is None
                and restriction_order is None
                and not DENIAL_CUE.search(message.text)
            ):
                return False
            await tell("此操作需要可核验的群主、管理员或面板授权身份。")
            return True
        async with self.lock:
            if role_check is not None:
                try:
                    role = await role_check()
                except Exception:
                    role = ""
                if role not in ("owner", "admin"):
                    await tell("当前管理员或面板授权已失效或暂不可核验，本次未执行。")
                    return True
            who = message.who
            scope = (who.platform, who.group, who.sender)
            oid = hashlib.sha256(
                json.dumps([*scope, message.id], ensure_ascii=False).encode()
            ).hexdigest()
            existing = self.db.execute(
                "SELECT status FROM admin_kick_operations WHERE id=?", (oid,)
            ).fetchone()
            existing = (
                existing
                or self.bans.existing(oid)
                or self.history.existing(oid)
                or self.cycle.existing(oid)
                or self.unban.existing(oid)
                or self.batch.seen(oid)
                or self.records.existing(oid)
            )
            if existing:
                if record_route and self.records.existing(oid):
                    return True
                await tell(
                    "此指令已记录，状态："
                    + existing[0]
                    + "；请核实原操作，勿重复提交。"
                )
                return True
            if kill_route:
                selected = await self.kill_history.handle(
                    self, message, role, kill_order, kill_snapshot, tell, role_check
                )
                if selected is None:
                    return True
                role = selected["role"]
                target = selected["target"]
                action = selected["action"]
                if action == "ban":
                    return await self.bans.submit(
                        self, message, role, target, oid, tell
                    )
                if action in ("cycle", "unban"):
                    support = self.cycle if action == "cycle" else self.unban
                    return await support.handle(
                        self,
                        message,
                        role,
                        oid,
                        {"query": target, "clarify": False},
                        None,
                        tell,
                    )
                request = {
                    "confirm": False,
                    "query": target,
                    "ids": [target],
                    "ambiguous": False,
                }
                batch_ordinals = None
                restriction_order = None
                cycle_route = unban_route = record_route = False
            elif (
                explicit_order_attempt(message.text)
                or is_replay
                or cycle_order is not None
                or unban_order is not None
                or record_order is not None
                or is_cycle_list
            ):
                self.kill_history.clear(scope)
            if restriction_order is not None:
                return await self.offline_batch.handle(
                    self,
                    message,
                    role,
                    oid,
                    restriction_order,
                    restriction_snapshot,
                    tell,
                    role_check,
                )
            if record_route:
                return await self.records.handle(
                    self, message, role, oid, record_order, record_snapshot, tell
                )
            if batch_ordinals is not None:
                return await self.batch.handle(
                    self,
                    message,
                    role,
                    oid,
                    batch_ordinals,
                    replay_snapshot,
                    selection_snapshot,
                    tell,
                    role_check,
                )
            if is_cycle_list:
                return await self.cycle_list.handle(self, message, role, oid, tell)
            if unban_route:
                return await self.unban.handle(
                    self, message, role, oid, unban_order, unban_snapshot, tell
                )
            if cycle_route:
                return await self.cycle.handle(
                    self, message, role, oid, cycle_order, cycle_snapshot, tell
                )
            if is_replay:
                self.offline_batch.clear(scope)
                self.records.clear(scope)
                self.unban.clear(scope)
                self.cycle.clear(scope)
                if self.replay.seen(scope, message.id):
                    await tell("此回溯请求已处理；请按原列表确认，或重新发送“回溯”。")
                    return True
                with self.db:
                    self.db.execute(
                        "DELETE FROM admin_kick_drafts WHERE platform=? AND grp=? AND sender=?",
                        scope,
                    )
                self.history.clear(scope)
                await self.replay.offer(message, role, tell)
                return True
            if (
                request is not None
                and "selection" in request
                and replay_snapshot is not None
            ):
                target, error = self.replay.select(
                    scope, replay_snapshot, request["selection"]
                )
                if error:
                    await tell(error)
                    return True
                request = {
                    "confirm": False,
                    "query": target,
                    "ids": [target],
                    "ambiguous": False,
                }
            if model is not None and request is None:
                new_attempt = explicit_order_attempt(message.text)
                if new_attempt:
                    self.offline_batch.clear(scope)
                    self.records.clear(scope)
                    self.unban.clear(scope)
                    self.cycle.clear(scope)
                    # Revoke before model I/O, including none/error/clarify paths.
                    # A subsequent ordinal must never select the previous player.
                    with self.db:
                        self.db.execute(
                            "DELETE FROM admin_kick_drafts WHERE platform=? AND grp=? AND sender=?",
                            scope,
                        )
                    self.history.clear(scope)
                    self.replay.clear(scope)
                draft = self.db.execute(
                    "SELECT candidates,expires FROM admin_kick_drafts WHERE platform=? AND grp=? AND sender=?",
                    scope,
                ).fetchone()
                candidates = (
                    self.history.candidate_ids(draft[0])
                    if draft and draft[1] >= time.time()
                    else []
                )
                try:
                    if image_sources:
                        request = await interpret_image(
                            model, message.text, image_sources, media
                        )
                    else:
                        request = await interpret(model, message.text, candidates)
                except Exception:
                    await tell(
                        "未能可靠理解本次人工踢出请求，未执行；请稍后重新明确目标和操作。"
                    )
                    return True
                if request is None:
                    if new_attempt:
                        await tell(
                            "本次踢出请求未能可靠识别，未执行；旧候选已作废，请重新发送玩家昵称或精确SteamID64。"
                        )
                        return True
                    return False
                if request.get("clarify"):
                    await tell(
                        "目标或操作不够明确，未执行；请提供清晰截图并指明目标，或提供玩家昵称、精确SteamID64。"
                    )
                    return True
            if "selection" in request:
                draft = self.db.execute(
                    "SELECT candidates,expires FROM admin_kick_drafts WHERE platform=? AND grp=? AND sender=?",
                    scope,
                ).fetchone()
                if (
                    selection_snapshot is None
                    or not draft
                    or tuple(draft) != selection_snapshot
                ):
                    await tell(
                        "候选列表尚未就绪或已更新，未执行；请等待最新列表后重新确认序号。"
                    )
                    return True
                candidates = (
                    self.history.candidate_ids(draft[0])
                    if draft and draft[1] >= time.time()
                    else []
                )
                ordinal = request["selection"]
                if not 1 <= ordinal <= len(candidates):
                    await tell(
                        "候选序号不存在或请求已过期；请重新发起踢出请求，并按最新列表确认。"
                    )
                    return True
                request["ids"] = [candidates[ordinal - 1]]
            if not request["confirm"]:
                self.offline_batch.clear(scope)
                self.records.clear(scope)
                self.unban.clear(scope)
                self.cycle.clear(scope)
                # A positively identified new order supersedes the old proposal,
                # even when its later directory request fails.
                with self.db:
                    self.db.execute(
                        "DELETE FROM admin_kick_drafts WHERE platform=? AND grp=? AND sender=?",
                        scope,
                    )
                self.history.clear(scope)
                self.replay.clear(scope)
            try:
                players = fresh_players(await self.client.players())
            except Exception:
                await tell("在线名单不可核验，暂不执行踢出。")
                return True
            ids = request["ids"]
            queries = ids or request.get("queries", [request["query"]])
            directory, history_unavailable = await self.history.lookup(
                queries, who, role, players
            )
            players = [p for p in directory if p.get("online", True)]
            mode = "kick"
            if len(ids) > 1 or request["ambiguous"]:
                await tell("一次只处理一个玩家，请提供一个精确的17位SteamID64。")
                return True
            if request["confirm"]:
                row = self.db.execute(
                    "SELECT candidates,expires FROM admin_kick_drafts WHERE platform=? AND grp=? AND sender=?",
                    scope,
                ).fetchone()
                if (
                    not row
                    or row[1] < time.time()
                    or len(ids) != 1
                    or ids[0] not in self.history.candidate_ids(row[0])
                ):
                    await tell(
                        "待确认请求不存在、已过期或ID不在候选中；请重新发起踢出请求，并输入候选的精确SteamID64。"
                    )
                    return True
                mode = (
                    "restriction"
                    if "offline:" + ids[0] in json.loads(row[0])
                    else self.history.action(scope, row[1], ids[0])
                )
            if ids and mode == "restriction":
                exact = [p for p in directory if p["steamId"] == ids[0]]
                if (
                    len(exact) != 1
                    or exact[0].get("online", True)
                    or history_unavailable
                ):
                    await tell(
                        "离线目标状态或历史身份发生变化，未补封；请重新发起请求并确认当前处置方式。"
                    )
                    with self.db:
                        self.db.execute(
                            "DELETE FROM admin_kick_drafts WHERE platform=? AND grp=? AND sender=?",
                            scope,
                        )
                    self.history.clear(scope)
                    return True
            elif ids and not any(p["steamId"] == ids[0] for p in players):
                exact = [
                    p
                    for p in directory
                    if p["steamId"] == ids[0] and not p.get("online", True)
                ]
                if len(exact) == 1 and not history_unavailable:
                    await self.offer(scope, exact, tell)
                    return True
                if history_unavailable:
                    await tell("历史玩家检索暂不可核验，未执行；请稍后重试。")
                    return True
            if not ids:
                queries = [
                    q.casefold() for q in request.get("queries", [request["query"]])
                ]
                if request["confirm"] or not all(queries):
                    await tell("确认必须包含精确的17位SteamID64。")
                    return True
                matches = [
                    p
                    for p in players
                    if any(
                        query in str(p.get("name", "")).casefold()
                        or query in p["steamId"]
                        or SequenceMatcher(
                            None, query, str(p.get("name", "")).casefold()
                        ).ratio()
                        >= 0.6
                        for query in queries
                    )
                ][:5]
                if callable(getattr(self.client, "admin_player_search", None)):
                    matches = self.history.matches(directory, queries)
                if not matches and history_unavailable:
                    await tell("历史玩家检索暂不可核验，未执行；请稍后重试。")
                    return True
                await self.offer(scope, matches, tell)
                return True
            target = ids[0]
            if (
                mode != "restriction"
                and len([p for p in players if p["steamId"] == target]) != 1
            ):
                await tell("精确ID未在新鲜在线名单中唯一匹配，暂不执行。")
                return True
            held = self.db.execute(
                "SELECT id FROM admin_kick_operations WHERE target=? AND status IN ('dispatching','outcome_unknown','accepted') LIMIT 1",
                (target,),
            ).fetchone()
            if (
                held
                or self.bans.held(target)
                or self.history.held(target)
                or self.cycle.held(target)
                or self.unban.held(target)
            ):
                await tell("该玩家存在待核实的处置记录，请核实原操作，勿重复提交。")
                return True
            if mode == "restriction":
                await self.history.submit(message, role, target, oid, tell)
                return True
            return await self.submit_kick(message, role, target, oid, tell)

    def pending_target(self, target):
        return bool(
            self.bans.held(target)
            or self.db.execute(
                "SELECT id FROM admin_kick_operations WHERE target=? AND status IN ('dispatching','outcome_unknown','accepted') LIMIT 1",
                (target,),
            ).fetchone()
            or self.history.held(target)
            or self.cycle.held(target)
            or self.unban.held(target)
        )

    async def submit_kick(self, message, role, target, oid, tell):
        if role not in ("owner", "admin"):
            await tell("此操作需要可核验的群主、管理员或面板授权身份。")
            return True
        who = message.who
        scope = (who.platform, who.group, who.sender)
        platform, bot = who.platform.rsplit(":", 1)
        payload = {
            "requestId": oid,
            "steamId": target,
            "actor": {
                "platform": platform,
                "botId": bot,
                "groupId": who.group,
                "memberId": who.sender,
                "role": role,
            },
            "requestedAt": time.time(),
        }
        with self.db:
            self.db.execute(
                "INSERT INTO admin_kick_operations VALUES(?,?,?,?,?,'dispatching',?,?,?,NULL)",
                (oid, *scope, target, time.time(), json.dumps(payload), "{}"),
            )
            self.db.execute(
                "DELETE FROM admin_kick_drafts WHERE platform=? AND grp=? AND sender=?",
                scope,
            )
        try:
            result = await self.client.admin_kick(payload, "wd-admin-" + oid)
            operation = result.get("operationId")
            if not isinstance(operation, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,128}", operation
            ):
                raise ValueError("invalid_operation")
            with self.db:
                self.db.execute(
                    "UPDATE admin_kick_operations SET status='accepted',result=?,operation_id=? WHERE id=?",
                    (json.dumps(result), operation, oid),
                )
        except BaseException as error:
            with self.db:
                self.db.execute(
                    "UPDATE admin_kick_operations SET status='outcome_unknown' WHERE id=?",
                    (oid,),
                )
            if isinstance(error, asyncio.CancelledError):
                raise
            await tell("请求结果待核实，勿重复提交。操作记录：" + oid[:16])
            return True
        try:
            await tell("已受理")
        finally:
            # Register after the acknowledgement so polling cannot reverse replies.
            # The callback keeps the original adapter's passive-reply context.
            from .sanction_summary import receipt_notice

            self.receipts[oid] = (
                receipt_notice(self.client, message.who, role, target, tell),
                time.monotonic() + 300,
                0,
            )
        return True

    def close(self):
        self.bans.close()
        self.receipts.clear()
        self.history.close()
        self.cycle.close()
        self.unban.close()
        self.batch.close()

    async def poll(self):
        async with self.poll_lock:
            await self._poll()
            await self._notify_receipts()
            await self.batch.poll()

    async def _poll(self):
        await self.bans.poll()
        await self.history.poll()
        await self.cycle.poll()
        await self.unban.poll()
        rows = self.db.execute(
            "SELECT id,operation_id,target FROM admin_kick_operations WHERE status='accepted' ORDER BY created LIMIT 10"
        ).fetchall()
        for oid, operation, target in rows:
            try:
                result = await self.client.admin_operation(operation)
                if (
                    result.get("operationId") != operation
                    or result.get("steamId") != target
                ):
                    continue
                status = {
                    "confirmed": "confirmed",
                    "target_offline": "target_offline",
                    "failed": "failed",
                    "unknown": "outcome_unknown",
                    "cancelled": "cancelled",
                }.get(result.get("kickStatus"))
                if status is None and result.get("status") in (
                    "offline",
                    "rejected",
                    "superseded",
                    "cancelled",
                ):
                    status = result["status"]
                if status:
                    with self.db:
                        self.db.execute(
                            "UPDATE admin_kick_operations SET status=?,result=? WHERE id=? AND status='accepted'",
                            (status, json.dumps(result), oid),
                        )
            except Exception:
                continue

    async def _notify_receipts(self):
        for oid, (notify, expires, failures) in list(self.receipts.items()):
            row = self.db.execute(
                "SELECT target,status FROM admin_kick_operations WHERE id=?", (oid,)
            ).fetchone()
            if not row:
                self.receipts.pop(oid, None)
                continue
            target, status = row
            text = {
                "confirmed": "已踢出：",
                "target_offline": "玩家已离线，未执行踢出：",
                "offline": "玩家已离线，未执行踢出：",
                "failed": "踢出失败：",
                "outcome_unknown": "踢出结果待核实，请勿重复提交：",
                "cancelled": "踢出已取消：",
                "rejected": "踢出请求被拒绝：",
                "superseded": "踢出请求已被替代，请核实原操作：",
            }.get(status)
            if text is None:
                if time.monotonic() < expires:
                    continue
                text = "踢出结果仍待核实，请勿重复提交："
            try:
                await asyncio.wait_for(notify(text + target), timeout=15)
            except Exception:
                logging.getLogger(__name__).warning(
                    "WARDOGS administrator result reply unavailable; preserving audit."
                )
                if failures < 2 and time.monotonic() < expires:
                    self.receipts[oid] = (notify, expires, failures + 1)
                    continue
            self.receipts.pop(oid, None)
