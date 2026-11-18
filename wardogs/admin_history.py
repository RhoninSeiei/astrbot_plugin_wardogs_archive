"""Historical directory and explicitly confirmed permanent rejoin restrictions."""

import asyncio
import inspect
import json
import logging
import math
import re
import time
from difflib import SequenceMatcher


def actor(who, role):
    platform, bot = who.platform.rsplit(":", 1)
    return {
        "platform": platform,
        "botId": bot,
        "groupId": who.group,
        "memberId": who.sender,
        "role": role,
    }


def valid_time(value):
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and 0 <= value <= time.time() + 30
    )


class HistorySupport:
    def __init__(self, store, client):
        self.db, self.client = store.db, client
        self.receipts = {}
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS admin_draft_actions(
                    platform TEXT, grp TEXT, sender TEXT, expires REAL, actions TEXT,
                    PRIMARY KEY(platform,grp,sender));
                CREATE TABLE IF NOT EXISTS admin_restriction_operations(
                    id TEXT PRIMARY KEY,target TEXT,status TEXT,created REAL,
                    payload TEXT,result TEXT,operation_id TEXT);
            """)
            self.db.execute(
                "UPDATE admin_restriction_operations SET status='outcome_unknown' WHERE status='dispatching'"
            )

    @staticmethod
    def candidate_ids(raw):
        try:
            values = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(values, list) or len(values) > 5:
            return []
        result = []
        for value in values:
            match = (
                re.fullmatch(r"(?:offline:)?([0-9]{17})", value)
                if isinstance(value, str)
                else None
            )
            if match is None:
                return []
            result.append(match[1])
        return result

    @staticmethod
    def stored_ids(players):
        # Legacy plugins cannot mistake an offline proposal for an online ID.
        return [
            ("" if p.get("online", True) else "offline:") + p["steamId"]
            for p in players
        ]

    async def lookup(self, queries, who, role, online, *, search=None):
        found = {
            p["steamId"]: dict(
                p,
                online=True,
                aliases=[],
                lastSeen=time.time(),
                source="online_snapshot",
            )
            for p in online
        }
        unavailable = False
        search = search or getattr(self.client, "admin_player_search", None)
        if not callable(search):
            return list(found.values()), unavailable
        for query in list(dict.fromkeys(queries))[:5]:
            try:
                data = await search(
                    {
                        "query": query,
                        "actor": actor(who, role),
                        "requestedAt": time.time(),
                    }
                )
                observed = data.get("observedAt")
                rows = data.get("players")
                if (
                    not valid_time(observed)
                    or not 0 <= time.time() - observed <= 15
                    or not isinstance(rows, list)
                    or len(rows) > 2000
                    or type(data.get("truncated")) is not bool
                    or type(data.get("historyComplete")) is not bool
                ):
                    raise ValueError("history_not_fresh")
                if not rows and (data["truncated"] or not data["historyComplete"]):
                    unavailable = True
                checked = []
                for p in rows:
                    if (
                        not isinstance(p, dict)
                        or not isinstance(p.get("steamId"), str)
                        or not re.fullmatch(r"[0-9]{17}", p["steamId"])
                        or type(p.get("online")) is not bool
                        or not isinstance(p.get("name"), str)
                        or not p["name"].strip()
                        or (
                            p.get("lastSeen") is not None
                            and not valid_time(p["lastSeen"])
                        )
                        or not isinstance(p.get("source"), str)
                        or not p["source"]
                        or not isinstance(p.get("aliases"), list)
                        or len(p["aliases"]) > 100
                        or any(
                            not isinstance(a, str)
                            or not a.isprintable()
                            or len(a) > 128
                            for a in p["aliases"]
                        )
                    ):
                        raise ValueError("invalid_history_identity")
                    checked.append(p)
                for p in checked:
                    sid = p["steamId"]
                    aliases = found[sid]["aliases"] if sid in found else []
                    found[sid] = dict(
                        p, aliases=list(dict.fromkeys(aliases + p["aliases"]))
                    )
            except Exception:
                unavailable = True
        return list(found.values()), unavailable

    @staticmethod
    def matches(players, queries):
        def score(p):
            best = 0
            for query in queries:
                q = query.casefold()
                if q == p["steamId"]:
                    best = max(best, 5)
                elif q in p["steamId"]:
                    best = max(best, 2)
                for name in [p.get("name", "")] + p.get("aliases", []):
                    n = name.casefold()
                    if q == n:
                        best = max(best, 4)
                    elif q in n:
                        best = max(best, 3)
                    elif SequenceMatcher(None, q, n).ratio() >= 0.6:
                        best = max(best, 1)
            return best

        scored = [(score(p), p) for p in players]
        return [
            p
            for s, p in sorted(
                scored, key=lambda v: (-v[0], not v[1].get("online", True))
            )
            if s
        ][:5]

    def remember(self, scope, expires, players):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO admin_draft_actions VALUES(?,?,?,?,?)",
                (
                    *scope,
                    expires,
                    json.dumps(
                        {
                            p["steamId"]: "kick"
                            if p.get("online", True)
                            else "restriction"
                            for p in players
                        }
                    ),
                ),
            )

    def action(self, scope, expires, target):
        row = self.db.execute(
            "SELECT expires,actions FROM admin_draft_actions WHERE platform=? AND grp=? AND sender=?",
            scope,
        ).fetchone()
        return (
            json.loads(row[1]).get(target, "kick")
            if row and row[0] == expires
            else "kick"
        )

    def clear(self, scope):
        with self.db:
            self.db.execute(
                "DELETE FROM admin_draft_actions WHERE (platform=? AND grp=? AND sender=?) OR expires<?",
                (*scope, time.time()),
            )

    def existing(self, oid):
        return self.db.execute(
            "SELECT status FROM admin_restriction_operations WHERE id=?", (oid,)
        ).fetchone()

    def held(self, target):
        return self.db.execute(
            "SELECT id FROM admin_restriction_operations WHERE target=? AND status IN ('dispatching','accepted','outcome_unknown')",
            (target,),
        ).fetchone()

    async def submit(self, message, role, target, oid, notify):
        payload = {
            "requestId": oid,
            "steamId": target,
            "actor": actor(message.who, role),
            "requestedAt": time.time(),
        }
        with self.db:
            self.db.execute(
                "INSERT INTO admin_restriction_operations VALUES(?,?,'dispatching',?,?,?,NULL)",
                (oid, target, time.time(), json.dumps(payload), "{}"),
            )
            scope = (message.who.platform, message.who.group, message.who.sender)
            self.db.execute(
                "DELETE FROM admin_kick_drafts WHERE platform=? AND grp=? AND sender=?",
                scope,
            )
        self.clear(scope)
        try:
            result = await self.client.admin_restrict(
                payload, "wd-admin-restrict-" + oid
            )
            operation = result.get("operationId")
            if not isinstance(operation, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,128}", operation
            ):
                raise ValueError("invalid_operation")
            with self.db:
                self.db.execute(
                    "UPDATE admin_restriction_operations SET status='accepted',result=?,operation_id=? WHERE id=?",
                    (json.dumps(result), operation, oid),
                )
        except BaseException as error:
            with self.db:
                self.db.execute(
                    "UPDATE admin_restriction_operations SET status='outcome_unknown' WHERE id=?",
                    (oid,),
                )
            if isinstance(error, asyncio.CancelledError):
                raise
            value = notify(
                "离线补封请求结果待核实，请勿重复提交。操作记录：" + oid[:16]
            )
        else:
            try:
                value = notify("已受理，正在追加离线封禁记录。")
                if inspect.isawaitable(value):
                    await value
            finally:
                from .sanction_summary import receipt_notice

                self.receipts[oid] = (
                    receipt_notice(self.client, message.who, role, target, notify),
                    time.monotonic() + 300,
                    0,
                )
            return
        if inspect.isawaitable(value):
            await value

    def close(self):
        self.receipts.clear()

    @staticmethod
    def prior_automatic_kick(result, target):
        prior = result.get("priorAutomaticKick")
        created = result.get("createdAt")
        if not isinstance(prior, dict) or not valid_time(created):
            return False
        confirmed = prior.get("confirmedAt")
        return (
            prior.get("steamId") == target
            and prior.get("source") == "auto_kd"
            and prior.get("kickStatus") == "confirmed"
            and isinstance(prior.get("operationId"), str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", prior["operationId"]) is not None
            and valid_time(confirmed)
            and confirmed <= min(created, time.time())
        )

    async def notify_receipts(self):
        for oid, (notify, expires, failures) in list(self.receipts.items()):
            row = self.db.execute(
                "SELECT target,status,result FROM admin_restriction_operations WHERE id=?",
                (oid,),
            ).fetchone()
            if not row:
                self.receipts.pop(oid, None)
                continue
            target, status, raw = row
            if status == "restricted":
                result = json.loads(raw)
                if self.prior_automatic_kick(result, target):
                    text = f"{target} 此前已被智能检测自动踢出。已追加封禁记录（永久重入限制）。"
                else:
                    text = f"{target} 处理时已离线。已追加封禁记录（永久重入限制），本次未执行踢出。"
            else:
                text = {
                    "failed": "离线封禁记录登记失败：",
                    "outcome_unknown": "离线封禁记录登记结果待核实，请勿重复提交：",
                    "cancelled": "离线补封已取消，请重新核验玩家状态：",
                    "rejected": "离线补封请求被拒绝：",
                    "superseded": "该玩家已有其他处置，请核实原操作：",
                }.get(status)
                if text is None:
                    if time.monotonic() < expires:
                        continue
                    text = "离线封禁记录登记结果仍待核实，请勿重复提交："
                text += target
            try:
                value = notify(text)
                if inspect.isawaitable(value):
                    await asyncio.wait_for(value, timeout=15)
            except Exception:
                logging.getLogger(__name__).warning(
                    "WARDOGS administrator restriction reply unavailable; preserving audit."
                )
                if failures < 2 and time.monotonic() < expires:
                    self.receipts[oid] = (notify, expires, failures + 1)
                    continue
            self.receipts.pop(oid, None)

    async def poll(self):
        for oid, operation, target in self.db.execute(
            "SELECT id,operation_id,target FROM admin_restriction_operations WHERE status='accepted' ORDER BY created LIMIT 10"
        ).fetchall():
            try:
                result = await self.client.admin_operation(operation)
                if (
                    result.get("operationId") != operation
                    or result.get("steamId") != target
                ):
                    continue
                status = {
                    "restricted": "restricted",
                    "confirmed": "restricted",
                    "unknown": "outcome_unknown",
                    "failed": "failed",
                    "cancelled": "cancelled",
                }.get(result.get("restrictionStatus"))
                if status is None and result.get("status") in (
                    "rejected",
                    "cancelled",
                    "superseded",
                ):
                    status = result["status"]
                if status:
                    with self.db:
                        self.db.execute(
                            "UPDATE admin_restriction_operations SET status=?,result=? WHERE id=? AND status='accepted'",
                            (status, json.dumps(result), oid),
                        )
            except Exception:
                pass
        await self.notify_receipts()
