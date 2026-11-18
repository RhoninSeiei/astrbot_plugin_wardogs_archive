"""Administrator-only recent kill snapshots with immutable ordinal bindings."""

import json
import math
import re
import time
from datetime import datetime, timedelta, timezone

from .admin_history import actor

MAX_EVENTS = 1000
BEIJING = timezone(timedelta(hours=8))


def replay_window(text):
    value = text.strip().lstrip("/").rstrip("。.!！?？")
    value = re.sub(r"^wd\s+", "", value, flags=re.I)
    if value == "回溯":
        return 60
    if re.fullmatch(
        r"(?:回溯\s*(?:5分钟|五分钟|5分|5m)|(?:5分钟|五分钟|5分|5m)\s*回溯)",
        value,
        re.I,
    ):
        return 300
    return None


def replay_command(text):
    return replay_window(text) is not None


def stamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def identity(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9]{17}", value) is not None


class ReplayOverflow(ValueError):
    pass


def validate(data, window=60):
    if data.get("complete") is False:
        raise ReplayOverflow("replay_limit_exceeded")
    observed, start, end = (
        data.get(k) for k in ("observedAt", "windowStart", "windowEnd")
    )
    if (
        not all(stamp(v) for v in (observed, start, end))
        or not 0 <= time.time() - observed <= 15
    ):
        raise ValueError("stale_replay")
    if (
        end != observed
        or abs(end - start - window) > 0.001
        or data.get("timeBasis") != "archive_received_at"
        or data.get("complete") is not True
    ):
        raise ValueError("incomplete_replay")
    events = data.get("events")
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValueError("invalid_replay")
    seen = set()
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("invalid_event")
        for key in ("serverId", "archiveId"):
            if not isinstance(event.get(key), str) or not 1 <= len(event[key]) <= 128:
                raise ValueError("missing_event_identity")
        event_id = event.get("eventId")
        if event_id is not None and (
            not isinstance(event_id, str) or not 1 <= len(event_id) <= 128
        ):
            raise ValueError("invalid_event_identity")
        key = event["serverId"], event_id or event["archiveId"]
        if key in seen:
            raise ValueError("duplicate_replay_event")
        seen.add(key)
        at = event.get("receivedAt")
        if not stamp(at) or not start <= at <= end:
            raise ValueError("event_outside_window")
        for side in ("killer", "victim"):
            player = event.get(side)
            if not isinstance(player, dict) or (
                player.get("steamId") is not None and not identity(player["steamId"])
            ):
                raise ValueError("invalid_replay_player")
            if player.get("name") is not None and (
                not isinstance(player["name"], str) or len(player["name"]) > 128
            ):
                raise ValueError("invalid_replay_name")
        if event.get("weapon") is not None and (
            not isinstance(event["weapon"], str) or len(event["weapon"]) > 256
        ):
            raise ValueError("invalid_replay_weapon")
    return data


def label(value, fallback):
    text = re.sub(r"[@\x00-\x1f\x7f]", "", str(value or ""))[:64]
    return text or fallback


def render(data):
    end = datetime.fromtimestamp(data["windowEnd"], BEIJING).strftime("%H:%M:%S")
    lines = [
        f"近{int((data['windowEnd'] - data['windowStart']) / 60)}分钟接收的击杀记录，共{len(data['events'])}条；截止{end}（北京时间）。"
    ]
    for i, event in enumerate(data["events"], 1):
        killer = label(event["killer"].get("name"), "未知击杀者")
        victim = label(event["victim"].get("name"), "未知被击杀者")
        weapon = label(event.get("weapon"), "武器未知")
        if weapon.startswith("Id.Item."):
            weapon = weapon[len("Id.Item.") :]
        suffix = (
            " [缺少击杀者ID，不可选]"
            if not identity(event["killer"].get("steamId"))
            else ""
        )
        lines.append(f"{i}. {killer} — [{weapon}] → {victim}{suffix}")
    lines.append(
        "尚未处罚。120秒内由本次管理员@同一机器人回复序号，或用空格分隔多个序号（如1 3 5，最多20个），踢出对应击杀者；离线目标需另行确认补封。时间为归档接收时间。"
    )
    chunks, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > 1700:
            chunks.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        chunks.append(current)
    return chunks


class ReplayDrafts:
    def __init__(self, store, client):
        self.db, self.client = store.db, client
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS admin_replay_drafts(
                    platform TEXT,grp TEXT,sender TEXT,source TEXT,created REAL,
                    expires REAL,events TEXT,active INTEGER,
                    PRIMARY KEY(platform,grp,sender,source));
                CREATE UNIQUE INDEX IF NOT EXISTS admin_replay_active
                    ON admin_replay_drafts(platform,grp,sender) WHERE active=1;
            """)

    def clear(self, scope):
        with self.db:
            self.db.execute(
                "UPDATE admin_replay_drafts SET active=0 WHERE platform=? AND grp=? AND sender=?",
                scope,
            )

    def snapshot(self, scope):
        row = self.db.execute(
            "SELECT source,expires,events FROM admin_replay_drafts WHERE platform=? AND grp=? AND sender=? AND active=1",
            scope,
        ).fetchone()
        return tuple(row) if row else None

    def seen(self, scope, source):
        return (
            self.db.execute(
                "SELECT 1 FROM admin_replay_drafts WHERE platform=? AND grp=? AND sender=? AND source=?",
                (*scope, source),
            ).fetchone()
            is not None
        )

    async def offer(self, message, role, tell):
        scope = message.who.platform, message.who.group, message.who.sender
        self.clear(scope)
        with self.db:
            self.db.execute(
                "DELETE FROM admin_replay_drafts WHERE created<?",
                (time.time() - 86400,),
            )
            self.db.execute(
                "INSERT INTO admin_replay_drafts VALUES(?,?,?,?,?,0,'[]',0)",
                (*scope, message.id, time.time()),
            )
        window = replay_window(message.text) or 60
        minutes = window // 60
        request = {"actor": actor(message.who, role), "requestedAt": time.time()}
        if window != 60:
            request["windowSeconds"] = window
        try:
            data = validate(await self.client.recent_kills(request), window=window)
        except ReplayOverflow:
            await tell(
                f"完整回溯不可用：近{minutes}分钟记录超过1000条，本次未生成可选序号。"
            )
            return
        except Exception:
            await tell(
                "击杀回溯暂不可用或记录未追平，本次未生成可确认列表；请稍后重新发送“回溯”。"
            )
            return
        if not data["events"]:
            await tell(f"过去{minutes}分钟内没有接收到击杀记录，未生成可确认列表。")
            return
        for chunk in render(data):
            await tell(chunk)
        with self.db:
            self.db.execute(
                "UPDATE admin_replay_drafts SET events=?,expires=?,active=1 WHERE platform=? AND grp=? AND sender=? AND source=?",
                (
                    json.dumps(data["events"], ensure_ascii=False),
                    time.time() + 120,
                    *scope,
                    message.id,
                ),
            )

    def select(self, scope, snapshot, ordinal):
        current = self.snapshot(scope)
        if snapshot is None or current != snapshot:
            return None, "回溯列表尚未就绪或已更新，请等待最新列表后重新确认。"
        if current[1] < time.time():
            return None, "回溯序号已过期，请重新发送“回溯”。"
        events = json.loads(current[2])
        if not 1 <= ordinal <= len(events):
            return None, "回溯序号不存在，请按当前列表选择。"
        target = events[ordinal - 1]["killer"].get("steamId")
        if not identity(target):
            return None, "该记录缺少击杀者精确SteamID64，不能按此序号处罚。"
        return target, None
