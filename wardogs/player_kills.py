"""Bounded per-player archive queries and actor-bound numeric navigation."""

import json
import math
import re
import time
from datetime import datetime

from .admin_history import actor
from .replay import BEIJING, identity, label
from .sanction_summary import describe as describe_sanctions
from .sanction_summary import query as query_sanctions

ACTIONS = {
    1: ("kick", "踢出"),
    2: ("ban", "封禁"),
    3: ("unban", "解封"),
    4: ("cycle", "加入哈哈名单"),
}
RELATIONS = {
    "friendly": "友军",
    "enemy": "敌军",
    "self": "自杀",
    "unknown": "敌友未知",
}


def history_request(text):
    value = text.strip().lstrip("/").rstrip("。！!")
    value = re.sub(r"^wd\s+", "", value, flags=re.I)
    match = re.fullmatch(r"(?:击杀记录|击杀历史|查击杀)(?:\s+(.*))?", value)
    if match:
        query = (match[1] or "").strip()
    else:
        match = re.fullmatch(r"查\s+(.+?)\s*的击杀(?:记录|历史)(?:\s+([0-9]+))?", value)
        if match:
            query = match[1] + ((" " + match[2]) if match[2] else "")
        else:
            match = re.fullmatch(r"查\s*(.*)", value)
            if not match:
                return None
            # Existing public aliases retain priority over this broad shorthand.
            from .query_aliases import local_query
            from .records import record_request
            from .server_identity import command as identity_command

            if (
                value.startswith("查询")
                or record_request(value) is not None
                or local_query(value) is not None
                or identity_command(value) is not None
                or identity_command((match[1] or "").strip()) is not None
            ):
                return None
            query = (match[1] or "").strip()
    tail = re.fullmatch(r"(.+?)\s+([0-9]+)(?:条)?", query)
    limit = 20
    if tail:
        query, limit = tail[1].strip(), int(tail[2])
    return {
        "query": query,
        "limit": limit,
        "clarify": not query
        or len(query) > 128
        or not query.isprintable()
        or not 1 <= limit <= 50,
    }


def navigation(text):
    value = text.strip().rstrip("。！!")
    if value in ("取消", "取消操作", "退出查询"):
        return "cancel"
    if re.fullmatch(r"[1-9][0-9]{0,2}", value):
        return int(value)
    return None


def validate(data, target, limit):
    if (
        not isinstance(data, dict)
        or data.get("steamId") != target
        or type(data.get("limit")) is not int
        or data.get("limit") != limit
        or type(data.get("observedAt")) not in (int, float)
        or not math.isfinite(data["observedAt"])
        or not 0 <= time.time() - data["observedAt"] <= 15
        or data.get("complete") is not True
        or type(data.get("hasMore")) is not bool
        or data.get("timeBasis") != "archive_received_at"
    ):
        raise ValueError("invalid_player_history")
    coverage = data.get("coverage")
    if (
        not isinstance(coverage, dict)
        or coverage.get("scope") != "retained_local_archive"
        or coverage.get("historyComplete") is not False
        or coverage.get("caughtUp") is not True
        or type(coverage.get("sourceObservedAt")) not in (int, float)
        or not math.isfinite(coverage["sourceObservedAt"])
        or not 0 <= time.time() - coverage["sourceObservedAt"] <= 15
        or coverage["sourceObservedAt"] > data["observedAt"]
    ):
        raise ValueError("invalid_history_coverage")
    since = coverage.get("recordsSince")
    if since is not None and (
        type(since) not in (int, float)
        or not math.isfinite(since)
        or not 0 <= since <= data["observedAt"]
    ):
        raise ValueError("invalid_history_coverage_time")
    events = data.get("events")
    if not isinstance(events, list) or len(events) > limit:
        raise ValueError("invalid_history_events")
    seen = set()
    last = float("inf")
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("invalid_history_event")
        for key in ("serverId", "archiveId"):
            if not isinstance(event.get(key), str) or not 1 <= len(event[key]) <= 128:
                raise ValueError("invalid_history_key")
        key = event["serverId"], event.get("eventId") or event["archiveId"]
        if not isinstance(key[1], str) or len(key[1]) > 128 or key in seen:
            raise ValueError("duplicate_history_event")
        seen.add(key)
        at = event.get("receivedAt")
        if (
            type(at) not in (int, float)
            or not math.isfinite(at)
            or not 0 <= at <= min(data["observedAt"], last)
        ):
            raise ValueError("invalid_history_time")
        last = at
        for side in ("killer", "victim"):
            p = event.get(side)
            if not isinstance(p, dict) or (
                p.get("steamId") is not None and not identity(p["steamId"])
            ):
                raise ValueError("invalid_history_identity")
            if p.get("name") is not None and (
                not isinstance(p["name"], str) or len(p["name"]) > 128
            ):
                raise ValueError("invalid_history_name")
        if (
            event["killer"].get("steamId") != target
            or event.get("relationship") not in RELATIONS
        ):
            raise ValueError("wrong_history_subject")
        game_time = event.get("gameTimeSeconds")
        if game_time is not None and (
            type(game_time) not in (int, float)
            or not math.isfinite(game_time)
            or game_time < 0
        ):
            raise ValueError("invalid_game_time")
        if (event.get("relationship") == "self") != (
            event["victim"].get("steamId") == target
        ):
            raise ValueError("inconsistent_self_kill")
        if event.get("weapon") is not None and (
            not isinstance(event["weapon"], str) or len(event["weapon"]) > 256
        ):
            raise ValueError("invalid_history_weapon")
    return data


def render(data, name, aliases=(), *, current_name_verified=False, sanctions=None):
    seen_names = []
    for value in list(aliases) + [
        e["killer"].get("name")
        for e in data["events"]
        if e["killer"].get("steamId") == data["steamId"]
    ]:
        if (
            isinstance(value, str)
            and value.strip()
            and value.strip() != name.strip()
            and value.strip() not in seen_names
        ):
            seen_names.append(value.strip())
    shown_names = "、".join(label(n, "未知昵称") for n in seen_names[:10])
    if len(seen_names) > 10:
        shown_names += f"（另有{len(seen_names) - 10}个未展开）"
    latest = label(name, "玩家") if current_name_verified else "暂不可核验"
    nickname_line = (
        "曾用名：" if current_name_verified else "同SteamID记录中出现的昵称："
    ) + (shown_names or "当前目录及本次记录中未发现其他昵称")
    explanation = (
        "记录昵称与当前或最近昵称不同时，标注为同一SteamID的历史昵称。"
        if current_name_verified and seen_names
        else ""
    )
    lines = [
        f"当前或最近昵称：{latest}\n{nickname_line}\n{explanation}".rstrip(),
        f"{label(name, '玩家')} {data['steamId']}\n本地留存归档最近{len(data['events'])}条击杀记录，时间为北京时间的归档接收时间：",
    ]
    if sanctions:
        lines.append(sanctions)
    for i, e in enumerate(data["events"], 1):
        at = datetime.fromtimestamp(e["receivedAt"], BEIJING).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        weapon = label(e.get("weapon"), "武器未知").removeprefix("Id.Item.")
        victim = e["victim"]
        game_time = (
            f" 游戏内计时{e['gameTimeSeconds']:g}秒"
            if e.get("gameTimeSeconds") is not None
            else ""
        )
        killer_name = e["killer"].get("name")
        historical = (
            "（同一SteamID的历史昵称）"
            if current_name_verified
            and isinstance(killer_name, str)
            and killer_name.strip()
            and killer_name.strip() != name.strip()
            and e["killer"].get("steamId") == data["steamId"]
            else ""
        )
        lines.append(
            f"┌─ 记录{i:02d} ─\n"
            f"│ {at}{game_time}\n"
            f"│ [{RELATIONS[e['relationship']]}] {label(e['killer'].get('name'), '击杀者')}{historical} → {label(victim.get('name'), '未知受害者')} {victim.get('steamId') or 'ID未知'}\n"
            f"│ 武器：{weapon}\n"
            "└────────"
        )
    if not data["events"]:
        lines.append("本地已同步归档中没有匹配记录；不能据此认定从未击杀。")
    lines.append("仅覆盖本地留存并已同步的归档，不代表该玩家全部历史。")
    if data["hasMore"]:
        lines.append("还有更早记录。本次最多展示指定条数，可发送“击杀记录 <ID> 50”。")
    chunks, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > 1700:
            chunks.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        chunks.append(current)
    # Keep the operation menu outside record chunks; bracket labels are literal
    # text, so QQ Markdown cannot renumber them as one ordered list.
    chunks.append(
        f"【玩家操作】\n操作对象：{label(name, '玩家')}\nSteamID：{data['steamId']}\n"
        "【1】踢出\n【2】封禁\n【3】解封\n【4】加入哈哈名单\n"
        "尚未处罚。记录框仅供查看，不用于选择其他玩家。\n"
        "120秒内由本次管理员@同一机器人回复1、2、3或4；回复“取消”退出。"
    )
    return chunks


class PlayerKillHistory:
    def __init__(self, store, client):
        self.db, self.client = store.db, client
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS admin_player_kill_navigation(
                    platform TEXT,grp TEXT,sender TEXT,source TEXT,created REAL,
                    expires REAL,state TEXT,payload TEXT,
                    PRIMARY KEY(platform,grp,sender));
                CREATE TABLE IF NOT EXISTS admin_player_kill_inputs(
                    platform TEXT,grp TEXT,sender TEXT,source TEXT,created REAL,
                    PRIMARY KEY(platform,grp,sender,source));
            """)

    def clear(self, scope):
        with self.db:
            self.db.execute(
                "DELETE FROM admin_player_kill_navigation WHERE platform=? AND grp=? AND sender=?",
                scope,
            )

    def snapshot(self, scope, at):
        row = self.db.execute(
            "SELECT source,created,expires,state,payload FROM admin_player_kill_navigation WHERE platform=? AND grp=? AND sender=?",
            scope,
        ).fetchone()
        return tuple(row) if row and row[1] <= at else None

    def seen(self, scope, source):
        return (
            self.db.execute(
                "SELECT 1 FROM admin_player_kill_inputs WHERE platform=? AND grp=? AND sender=? AND source=?",
                (*scope, source),
            ).fetchone()
            is not None
        )

    def remember(self, scope, message, stage, payload):
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO admin_player_kill_navigation VALUES(?,?,?,?,?,?,?,?)",
                (*scope, message.id, now, now + 120, stage, json.dumps(payload)),
            )

    async def fetch(self, message, role, target, name, limit, tell, *, history=None):
        scope = message.who.platform, message.who.group, message.who.sender
        try:
            data = validate(
                await self.client.player_kills(
                    {
                        "steamId": target,
                        "limit": limit,
                        "actor": actor(message.who, role),
                        "requestedAt": time.time(),
                    }
                ),
                target,
                limit,
            )
        except Exception:
            await tell(
                "个人击杀历史暂不可用或记录未追平，本次没有生成操作菜单；请稍后重查。"
            )
            return
        profile = None
        if history is not None:
            try:
                players, unavailable = await history.lookup(
                    [target], message.who, role, []
                )
                exact = [p for p in players if p["steamId"] == target]
                if not unavailable and len(exact) == 1:
                    profile = exact[0]
            except Exception:
                pass
        if profile is not None:
            name = profile["name"]
        sanctions = describe_sanctions(
            await query_sanctions(self.client, message.who, role, target)
        )
        for chunk in render(
            data,
            name,
            profile.get("aliases", []) if profile else (),
            current_name_verified=profile is not None,
            sanctions=sanctions,
        ):
            await tell(chunk)
        self.remember(scope, message, "actions", {"target": target, "name": name})

    async def handle(
        self, controller, message, role, request, snapshot, tell, role_check
    ):
        from .admin import fresh_players

        scope = message.who.platform, message.who.group, message.who.sender
        if self.seen(scope, message.id):
            return None
        with self.db:
            self.db.execute(
                "DELETE FROM admin_player_kill_inputs WHERE created<?",
                (time.time() - 86400,),
            )
            self.db.execute(
                "INSERT INTO admin_player_kill_inputs VALUES(?,?,?,?,?)",
                (*scope, message.id, time.time()),
            )
        if request is not None:
            controller.clear_drafts(scope)
            if request["clarify"]:
                await tell(
                    "请发送“查 <游戏昵称或SteamID>”或“击杀记录 <游戏昵称或SteamID>”，默认20条；可追加1至50的条数，例如“击杀记录 Example 50”。"
                )
                return None
            query, limit = request["query"], request["limit"]
            if identity(query):
                await self.fetch(
                    message,
                    role,
                    query,
                    "玩家",
                    limit,
                    tell,
                    history=controller.history,
                )
                return None
            try:
                online = fresh_players(await self.client.players())
                directory, unavailable = await controller.history.lookup(
                    [query], message.who, role, online
                )
            except Exception:
                await tell("玩家身份检索暂不可用，未生成查询候选。")
                return None
            if unavailable:
                await tell("玩家历史身份暂不可核验，请提供精确SteamID后重查。")
                return None
            matches = controller.history.matches(directory, [query.casefold()])
            if not matches:
                await tell("没有匹配到玩家，请提供游戏昵称或精确SteamID。")
                return None
            lines = ["选择玩家后查看击杀历史，尚未执行处罚："]
            lines += [
                f"【{i}】{label(p.get('name'), '玩家')} {p['steamId']}"
                for i, p in enumerate(matches, 1)
            ]
            lines.append("120秒内由本次管理员@同一机器人回复玩家序号，或回复“取消”。")
            await tell("\n".join(lines))
            self.remember(
                scope, message, "players", {"players": matches, "limit": limit}
            )
            return None
        current = self.snapshot(scope, message.at)
        if snapshot is None or snapshot != current or current[2] < time.time():
            await tell("击杀历史菜单已更新、尚未就绪或已过期，请重新查询。")
            return None
        value = navigation(message.text)
        if value == "cancel":
            self.clear(scope)
            await tell("已取消本次操作。")
            return None
        state, payload = current[3], json.loads(current[4])
        if state == "players":
            if not isinstance(value, int) or not 1 <= value <= len(payload["players"]):
                await tell("玩家序号不存在，请按当前候选列表选择。")
                return None
            p = payload["players"][value - 1]
            self.clear(scope)
            await self.fetch(
                message,
                role,
                p["steamId"],
                p.get("name", ""),
                payload["limit"],
                tell,
                history=controller.history,
            )
            return None
        if state == "actions":
            if value not in ACTIONS:
                await tell("操作序号不存在，请选择1至4，或回复“取消”。")
                return None
            action, title = ACTIONS[value]
            details = {
                "kick": "仅请求踢出；已离线将另行提示补封确认。",
                "ban": "登记永久重入限制并请求游戏原生封禁，按实际收据报告结果。",
                "unban": "解除游戏原生封禁及本地重入限制。",
                "cycle": "按服务器现有规则循环惩罚；哈哈名单为持续循环惩罚名单。",
            }[action]
            sanctions = describe_sanctions(
                await query_sanctions(self.client, message.who, role, payload["target"])
            )
            self.clear(scope)
            await tell(
                f"确认{title}：{label(payload.get('name'), '玩家')} {payload['target']}\n{details}\n{sanctions}\n【1】确认{title}\n【2】取消\n120秒内由本次管理员@同一机器人回复序号。"
            )
            self.remember(scope, message, "confirm", dict(payload, action=action))
            return None
        if state != "confirm" or value not in (1, 2):
            await tell("请回复1确认本次操作，或回复2取消。")
            return None
        self.clear(scope)
        if value == 2:
            await tell("已取消本次操作。")
            return None
        if role_check is not None:
            try:
                role = await role_check()
            except Exception:
                role = ""
        if role not in ("owner", "admin"):
            await tell("管理员身份已变化或不可核验，未执行。")
            return None
        if controller.pending_target(payload["target"]):
            await tell("该玩家存在待核实的处置记录，未重复执行。")
            return None
        return dict(payload, role=role)
