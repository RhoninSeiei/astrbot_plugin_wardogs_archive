"""Aggregate-only ban statistics and explicit read-only request matching."""

import math
import re
import time
from datetime import datetime, timedelta, timezone

KINDS = ("ban_stats", "ban_stats_12h", "ban_stats_24h")
RULE = "unique_first_effective_ban_v1"
TRIM = "，,。.!！?？~～、:：；;"
PREFIX = re.compile(
    r"^(?:请问|麻烦|帮忙|帮我|请|查询|查一下|查下|查查|查看|看看|看一下|看下|统计一下|统计下|报一下|报下|"
    r"服务器|伺服器|服里|本服|现在|目前|累计|总共|一共|总计|截至目前|截止目前|截至现在|截止现在|截止至今|截至至今|到现在)"
)
BODY = (
    r"(?:(?:封禁|封号)(?:人数统计|统计|人数|数量|总数|总人数)(?:是多少|多少)?|"
    r"(?:封禁|封号|封)(?:了)?(?:多少|几(?:个)?)人|(?:有)?(?:多少|几(?:个)?)人被封|封进统计|禁封统计)"
)
WINDOW = (
    r"(?:过去|最近|近)?(?P<hours>12|24|十二|二十四)(?:个)?(?:小时|h)(?:内|以来)?(?:的)?"
)
PATTERNS = (re.compile(WINDOW + BODY), re.compile(BODY + WINDOW))
BEIJING = timezone(timedelta(hours=8))


def match_query(value):
    """Only a whole statistics question may bypass the punishment-word guard."""
    for _ in range(2):
        value = re.sub(r"(?:谢谢啦|谢谢|谢了)$", "", value).strip(TRIM)
    for _ in range(3):
        value = re.sub(r"(?:呢|吗|呀|啊|呗|了)$", "", value)
    for _ in range(8):
        value = value.strip(TRIM)
        updated = PREFIX.sub("", value, count=1)
        if updated == value:
            break
        value = updated
    if re.fullmatch(BODY, value):
        return "ban_stats"
    for pattern in PATTERNS:
        match = pattern.fullmatch(value)
        if match:
            hours = {"十二": "12", "二十四": "24"}.get(match["hours"], match["hours"])
            return "ban_stats_" + hours + "h"
    return None


def validate(data):
    if data.get("countingRule") != RULE:
        raise ValueError("unsupported_ban_counting_rule")
    observed = data.get("observedAt")
    if (
        type(observed) not in (int, float)
        or not math.isfinite(observed)
        or not 0 <= time.time() - observed <= 90
    ):
        raise ValueError("stale_ban_statistics")
    fields = (
        "totalPlayers",
        "last24HoursPlayers",
        "last12HoursPlayers",
        "unknownTimePlayers",
    )
    values = [data.get(key) for key in fields]
    if any(type(value) is not int or not 0 <= value <= 1000000000 for value in values):
        raise ValueError("invalid_ban_count")
    total, day, half, unknown = values
    if not total >= day >= half or unknown > total - day:
        raise ValueError("inconsistent_ban_counts")
    if type(data.get("historyComplete")) is not bool:
        raise ValueError("invalid_ban_coverage")
    if "recordsSince" not in data:
        raise ValueError("missing_ban_coverage")
    since = data["recordsSince"]
    if since is not None and (
        type(since) not in (int, float)
        or not math.isfinite(since)
        or not 0 <= since <= observed
    ):
        raise ValueError("invalid_ban_records_start")
    return data


async def render(client, kind):
    try:
        data = validate(await client.ban_statistics())
        cutoff = datetime.fromtimestamp(data["observedAt"], BEIJING).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        lines = ["封禁人数统计（按玩家去重）", "截止：" + cutoff + "（北京时间）。"]
        if kind == "ban_stats":
            lines.append(f"累计封禁：{data['totalPlayers']}人。")
        if kind in ("ban_stats", "ban_stats_24h"):
            lines.append(f"过去24小时：{data['last24HoursPlayers']}人。")
        if kind in ("ban_stats", "ban_stats_12h"):
            lines.append(f"过去12小时：{data['last12HoursPlayers']}人。")
        lines.append("近期按首次生效时间统计，重复踢出不重复计人数。")
        if data["unknownTimePlayers"]:
            lines.append(
                f"{data['unknownTimePlayers']}人历史时间不详，未计入近12、24小时。"
            )
        if not data["historyComplete"]:
            lines.append("统计基于现存记录，可能未覆盖开服以来全部历史。")
        if data["recordsSince"] is not None:
            since = datetime.fromtimestamp(data["recordsSince"], BEIJING).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            lines.append("现存记录起始：" + since + "（北京时间）。")
        return "\n".join(lines)
    except Exception:
        return "封禁人数统计暂不可用，请稍后重试。"
