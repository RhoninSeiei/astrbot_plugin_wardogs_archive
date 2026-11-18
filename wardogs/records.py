"""Administrator-only, read-only punishment history with scoped page bindings."""

import json
import math
import re
import time
from datetime import datetime, timedelta, timezone

from .admin_history import actor
from .sanction_summary import describe as describe_sanctions
from .sanction_summary import query as query_sanctions

IDENTITY = re.compile(r"[0-9]{17}", re.ASCII)
BEIJING = timezone(timedelta(hours=8))
NEXT = {"下一页", "记录下一页", "处罚记录下一页"}
TYPES = {
    "kick_confirmation": "踢出确认记录",
    "rejoin_restriction": "重入限制登记",
    "kick_request": "踢出处置请求",
    "restriction_request": "离线限制请求",
    "cycle_registration": "循环名单登记",
    "unban_verification": "解封核验",
    "restriction_release": "本地限制释放",
    "team_switch": "循环换队",
    "console_kill": "控制台击杀",
    "burst_detection": "连杀检测",
}
STATES = {
    "confirmed": "已确认",
    "applied": "已生效",
    "pending": "待处理",
    "failed": "失败",
    "unknown": "结果未知",
    "not_executed": "未执行",
    "registered": "已登记",
    "already_registered": "此前已登记",
    "verified_clear": "已核验解除",
    "partial": "部分完成",
}
BASIS = {
    "kick_confirmed_at": "踢出确认时间",
    "ledger_recorded_at_effective_unknown": "台账时间，生效时间未知",
    "restriction_created_at": "限制登记时间",
    "operation_created_at": "操作创建时间",
    "release_recorded_at": "解除登记时间",
    "behavior_action_created_at": "动作创建时间",
    "detection_created_at": "检测时间",
}
SOURCES = {
    "automatic": "智能策略",
    "manual": "人工管理",
    "report": "举报处理",
    "unknown": "未知",
}


def safe(value, limit=256):
    return re.sub(r"[@\x00-\x1f\x7f]", " ", str(value))[:limit].strip()


def fresh(value):
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and 0 <= time.time() - value <= 30
    )


def record_request(text):
    value = text.strip().rstrip("。！!")
    if value in NEXT:
        return None
    match = re.fullmatch(
        r"(?:请\s*|麻烦\s*)?(?:查一下|查看|查询|查)?(?:封禁原因|处罚记录|封禁记录)\s*(.*)",
        value,
    )
    if not match:
        return None
    query = match[1].strip()
    ids = re.findall(r"[0-9]{17}", query)
    return {
        "query": query,
        "clarify": not query
        or len(query) > 128
        or bool(ids and not IDENTITY.fullmatch(query)),
    }


def record_selection(text):
    m = re.fullmatch(r"(?:查看(?:记录)?\s*)?([1-9][0-9]{0,3}|[0-9]{17})", text.strip())
    return m[1] if m else None


def validate_page(data, target):
    if (
        not isinstance(data, dict)
        or not fresh(data.get("observedAt"))
        or data.get("player", {}).get("steamId") != target
    ):
        raise ValueError("invalid_history_target")
    if (
        type(data.get("historyComplete")) is not bool
        or data.get("coverage") != "local_management_ledger_only"
        or data.get("snapshotMode") != "membership_fenced_live_status"
        or type(data.get("snapshotAt")) not in (int, float)
        or not 0 <= data["snapshotAt"] <= data["observedAt"]
    ):
        raise ValueError("invalid_history_coverage")
    records = data.get("records")
    more = data.get("hasMore")
    cursor = data.get("nextCursor")
    if (
        not isinstance(records, list)
        or len(records) > 5
        or type(more) is not bool
        or type(data.get("responseLimited")) is not bool
    ):
        raise ValueError("invalid_history_page")
    if more:
        if not records or not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
            raise ValueError("invalid_history_cursor")
    elif cursor is not None:
        raise ValueError("unexpected_history_cursor")
    seen = set()
    for row in records:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("recordId"), str)
            or not 1 <= len(row["recordId"]) <= 128
            or row["recordId"] in seen
        ):
            raise ValueError("invalid_record_id")
        seen.add(row["recordId"])
        if (
            row.get("type") not in TYPES
            or row.get("executionStatus") not in STATES
            or row.get("timeBasis") not in BASIS
            or not isinstance(row.get("source"), dict)
            or row["source"].get("category") not in SOURCES
        ):
            raise ValueError("unsupported_record")
        if (
            type(row.get("reasonKnown")) is not bool
            or type(row.get("reasonTruncated")) is not bool
        ):
            raise ValueError("invalid_reason")
        if row["reasonKnown"] and (
            not isinstance(row.get("reason"), str) or not 1 <= len(row["reason"]) <= 256
        ):
            raise ValueError("invalid_reason")
        at = row.get("timestamp")
        if (
            type(at) not in (int, float)
            or not math.isfinite(at)
            or not 0 <= at <= data["observedAt"]
        ):
            raise ValueError("invalid_record_time")
        if row.get("kickStatus") not in (
            "confirmed",
            "pending",
            "failed",
            "unknown",
            "not_executed",
            "not_applicable",
        ):
            raise ValueError("invalid_kick_state")
        if row["kickStatus"] == "confirmed" and (
            row["type"] not in ("kick_confirmation", "kick_request")
            or row["executionStatus"] != "confirmed"
        ):
            raise ValueError("inconsistent_confirmed_kick")
    return data


def render_page(data):
    player = data["player"]
    lines = [f"处罚记录：{safe(player.get('name', ''), 128)} {player['steamId']}"]
    if not data["records"]:
        lines.append("本地台账未找到该玩家的处罚记录。")
    for i, row in enumerate(data["records"], 1):
        date = datetime.fromtimestamp(row["timestamp"], BEIJING).strftime(
            "%m-%d %H:%M:%S"
        )
        state = (
            "已踢出"
            if row["type"] in ("kick_confirmation", "kick_request")
            and row["kickStatus"] == "confirmed"
            else STATES[row["executionStatus"]]
        )
        reason = safe(row["reason"]) if row["reasonKnown"] else "原因未知"
        if row["reasonTruncated"]:
            reason += "（已截短）"
        lines.append(
            f"{i}. {date}（北京时间，{BASIS[row['timeBasis']]}）\n{TYPES[row['type']]}；{state}；来源：{SOURCES[row['source']['category']]}。\n原因：{reason}"
        )
    lines.append("本地管理台账可能不完整；同一处置可能有多条记录，条数不代表踢出次数。")
    if data["hasMore"]:
        lines.append("120秒内由本次管理员@同一机器人发送“下一页”继续查看。")
    chunks = []
    chunk = ""
    for line in lines:
        if chunk and len(chunk) + len(line) + 1 > 1700:
            chunks.append(chunk)
            chunk = ""
        chunk += ("\n" if chunk else "") + line
    if chunk:
        chunks.append(chunk)
    return chunks


class RecordQueries:
    def __init__(self, store, client):
        self.db, self.client = store.db, client
        with self.db:
            self.db.executescript(
                """CREATE TABLE IF NOT EXISTS admin_record_drafts(platform TEXT,grp TEXT,sender TEXT,state TEXT,expires REAL,PRIMARY KEY(platform,grp,sender));CREATE TABLE IF NOT EXISTS admin_record_receipts(id TEXT PRIMARY KEY,status TEXT,created REAL);"""
            )

    def existing(self, oid):
        return self.db.execute(
            "SELECT status FROM admin_record_receipts WHERE id=?", (oid,)
        ).fetchone()

    def clear(self, scope):
        with self.db:
            self.db.execute(
                "DELETE FROM admin_record_drafts WHERE platform=? AND grp=? AND sender=?",
                scope,
            )

    def snapshot(self, scope, at):
        row = self.db.execute(
            "SELECT state,expires FROM admin_record_drafts WHERE platform=? AND grp=? AND sender=?",
            scope,
        ).fetchone()
        return tuple(row) if row and at >= row[1] - 120 else None

    async def handle(self, controller, message, role, oid, request, snapshot, tell):
        if role not in ("owner", "admin"):
            await tell("查询处罚记录需要可核验的群主或管理员身份。")
            return True
        scope = (message.who.platform, message.who.group, message.who.sender)
        if self.db.execute(
            "SELECT id FROM admin_record_receipts WHERE id=?", (oid,)
        ).fetchone():
            return True
        with self.db:
            self.db.execute(
                "DELETE FROM admin_record_receipts WHERE created<?",
                (time.time() - 86400,),
            )
            self.db.execute(
                "INSERT INTO admin_record_receipts VALUES(?,'handled',?)",
                (oid, time.time()),
            )
        body = {"actor": actor(message.who, role), "requestedAt": time.time()}
        target = None
        cursor = None
        if request is not None:
            controller.clear_drafts(scope)
            if request["clarify"]:
                await tell("请提供“封禁原因 玩家昵称或精确SteamID64”，本次只查询记录。")
                return True
            query = request["query"]
            if IDENTITY.fullmatch(query):
                target = query
            else:
                try:
                    data = await self.client.punishment_search(dict(body, query=query))
                    players = data.get("players")
                    if (
                        data.get("complete") is not True
                        or not fresh(data.get("observedAt"))
                        or type(data.get("truncated")) is not bool
                        or not isinstance(players, list)
                        or len(players) > 20
                    ):
                        raise ValueError("invalid_search")
                    if data["truncated"]:
                        await tell("匹配结果较多，请提供更完整昵称或精确SteamID64。")
                        return True
                    ids = []
                    for p in players:
                        if (
                            not isinstance(p, dict)
                            or not isinstance(p.get("steamId"), str)
                            or not IDENTITY.fullmatch(p["steamId"])
                            or p["steamId"] in ids
                            or not isinstance(p.get("name"), str)
                        ):
                            raise ValueError("invalid_search_identity")
                        ids.append(p["steamId"])
                    if not ids:
                        await tell(
                            "本地台账未匹配到玩家，请提供历史昵称或精确SteamID64。"
                        )
                        return True
                    lines = ["选择玩家查看处罚记录，不执行处罚："] + [
                        f"{i}. {safe(p['name'], 80)} {p['steamId']}"
                        for i, p in enumerate(players, 1)
                    ]
                    lines.append("120秒内由本次管理员@同一机器人回复序号，或“查看 1”。")
                    chunk = ""
                    for line in lines:
                        if chunk and len(chunk) + len(line) + 1 > 1700:
                            await tell(chunk)
                            chunk = ""
                        chunk += ("\n" if chunk else "") + line
                    if chunk:
                        await tell(chunk)
                    self.save(scope, {"kind": "candidates", "ids": ids})
                except Exception:
                    await tell("处罚记录候选不可核验，请稍后重试。")
                return True
        else:
            current = self.snapshot(scope, message.at)
            if snapshot is None or current != snapshot or snapshot[1] < time.time():
                await tell("记录查询列表已更新、过期或尚未就绪，请重新查询。")
                return True
            try:
                state = json.loads(snapshot[0])
                if message.text.strip() in NEXT:
                    if state["kind"] != "page":
                        raise ValueError("not_page")
                    target = state["target"]
                    cursor = state["cursor"]
                else:
                    if state["kind"] != "candidates":
                        raise ValueError("not_candidates")
                    value = record_selection(message.text)
                    if IDENTITY.fullmatch(value or ""):
                        if value not in state["ids"]:
                            raise ValueError("unknown_target")
                        target = value
                    else:
                        target = state["ids"][int(value) - 1]
            except (TypeError, ValueError, KeyError, IndexError):
                await tell("查看记录的序号或分页无效，请重新查询。")
                return True
        self.clear(scope)
        try:
            body = dict(body, steamId=target, limit=5, requestedAt=time.time())
            if cursor is not None:
                body["cursor"] = cursor
            data = validate_page(await self.client.punishment_history(body), target)
            summary_text = describe_sanctions(
                await query_sanctions(self.client, message.who, role, target)
            )
            if summary_text:
                await tell(summary_text)
            for text in render_page(data):
                await tell(text)
            if data["hasMore"]:
                self.save(
                    scope,
                    {"kind": "page", "target": target, "cursor": data["nextCursor"]},
                )
        except Exception:
            await tell("处罚记录暂不可核验，请稍后重试；未执行任何处罚或解封。")
        return True

    def save(self, scope, state):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO admin_record_drafts VALUES(?,?,?,?,?)",
                (*scope, json.dumps(state), time.time() + 120),
            )
