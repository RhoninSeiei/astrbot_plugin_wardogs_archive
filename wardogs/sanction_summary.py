"""Verified per-player sanction cycles; informational only, never authority."""

import asyncio
import inspect
import math
import re
import time

from .admin_history import actor

RULE = "verified_sanction_cycles_v1"
COVERAGE = "retained_local_sanction_ledger"


def validate(data, target):
    if (
        not isinstance(data, dict)
        or data.get("steamId") != target
        or type(data.get("observedAt")) not in (int, float)
        or not math.isfinite(data["observedAt"])
        or not 0 <= time.time() - data["observedAt"] <= 15
        or data.get("countingRule") != RULE
        or data.get("coverage") != COVERAGE
        or data.get("complete") is not True
        or data.get("historyComplete") is not False
        or data.get("countsAreLowerBounds") is not True
    ):
        raise ValueError("invalid_sanction_summary")
    for key in ("banCount", "unbanCount", "unknownItems"):
        if type(data.get(key)) is not int or not 0 <= data[key] <= 2**63 - 1:
            raise ValueError("invalid_sanction_count")
    since = data.get("recordsSince")
    if since is not None and (
        type(since) not in (int, float)
        or not math.isfinite(since)
        or not 0 <= since <= data["observedAt"]
    ):
        raise ValueError("invalid_sanction_coverage_time")
    alerts = data.get("alerts")
    if not isinstance(alerts, dict):
        raise ValueError("invalid_sanction_alerts")
    for flag, count in (("repeatedBan", "banCount"), ("repeatedUnban", "unbanCount")):
        if type(alerts.get(flag)) is not bool or alerts[flag] != (data[count] > 1):
            raise ValueError("inconsistent_sanction_alert")
    return data


async def query(client, who, role, target):
    if (
        role not in ("owner", "admin")
        or not isinstance(target, str)
        or not re.fullmatch(r"[0-9]{17}", target)
    ):
        return None
    method = getattr(client, "player_sanction_summary", None)
    if not callable(method):
        return None
    try:
        return validate(
            await asyncio.wait_for(
                method(
                    {
                        "steamId": target,
                        "actor": actor(who, role),
                        "requestedAt": time.time(),
                    }
                ),
                5,
            ),
            target,
        )
    except Exception:
        return {"unavailable": True}


def describe(data, *, warning_only=False):
    if data is None:
        return ""
    if data.get("unavailable"):
        return "该玩家封禁/解封次数暂不可核验。"
    repeated = []
    if data["banCount"] > 1:
        repeated.append("多次封禁")
    if data["unbanCount"] > 1:
        repeated.append("多次解封")
    if warning_only and not repeated:
        return ""
    text = f"本地可核验：封禁至少{data['banCount']}次，解封至少{data['unbanCount']}次；不代表全部历史。"
    if data["unknownItems"]:
        text += f"另有{data['unknownItems']}项历史证据无法可靠计次。"
    if repeated:
        text += "\n管理员提醒：该玩家存在" + "、".join(repeated) + "记录，请核实历史。"
    return text


def receipt_notice(client, who, role, target, notify):
    async def send(text):
        extra = describe(await query(client, who, role, target), warning_only=True)
        value = notify(str(text) + ("\n" + extra if extra else ""))
        if inspect.isawaitable(value):
            await value

    return send
