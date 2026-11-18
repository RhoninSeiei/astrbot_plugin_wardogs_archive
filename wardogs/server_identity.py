"""Scoped read-only server UUID queries and strictly fixed safe rendering."""

import asyncio
import math
import re
import time
from datetime import datetime, timedelta, timezone

UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
BEIJING = timezone(timedelta(hours=8))


def command(text):
    value = (
        re.sub(r"\s+", "", text.strip().removeprefix("/")).rstrip("。！!？?").lower()
    )
    if value in {
        "服务器id",
        "服id",
        "服地址",
        "服务器地址",
        "查询服务器id",
        "查看服务器id",
        "当前服务器id",
        "服务器id是多少",
    }:
        return "query", None
    return None


def validate(data, now=None):
    now = time.time() if now is None else now
    if not isinstance(data, dict):
        raise ValueError("invalid_server_identity")
    sid, observed = data.get("serverId"), data.get("observedAt")
    if (
        not isinstance(sid, str)
        or not UUID.fullmatch(sid)
        or type(observed) not in (int, float)
        or not math.isfinite(observed)
        or not 0 <= now - observed <= 90
    ):
        raise ValueError("invalid_server_identity")
    return {"serverId": sid.lower(), "observedAt": observed}


def content(data):
    data = validate(data)
    date = datetime.fromtimestamp(data["observedAt"], BEIJING).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return (
        "阿纳海姆 WARDOGS\n服务器ID："
        + data["serverId"]
        + "\n核验时间："
        + date
        + "（北京时间）"
        + "\n请在游戏左下角选择“ID加入”，输入上方服务器ID加入。"
    )


async def query(client):
    try:
        data = await asyncio.wait_for(client.server_identity(), 5)
        return content(data)
    except Exception:
        return "服务器ID暂不可用，请稍后重试。"
