"""Administrator-only bounded view of currently enabled cyclic punishments."""

import re
import time

from .admin_history import actor, valid_time

MEANING = (
    "哈哈名单即循环惩罚名单：名单中的玩家在线时，服务器按既有规则随机换队、通过控制台击杀，"
    "成功动作后发送包含“哈哈”的公告。首次识别到在线时可能立即触发，之后按随机间隔循环，"
    "并非每秒执行。它与原生封禁、本地重入限制分别管理，解封不会自动移出哈哈名单。"
)


def list_command(text):
    value = re.sub(r"\s+", "", text.strip()).rstrip("。！!？?")
    return value in {
        "循环名单",
        "哈哈名单",
        "查看循环名单",
        "查看哈哈名单",
        "列出循环名单",
        "列出哈哈名单",
        "当前循环名单",
        "当前哈哈名单",
        "看看循环名单",
        "看看哈哈名单",
        "循环名单有哪些人",
        "哈哈名单有哪些人",
        "循环名单里有谁",
        "哈哈名单里有谁",
        "哈哈名单什么意思",
        "循环名单什么意思",
        "什么是哈哈名单",
        "什么是循环名单",
        "哈哈名单是什么",
        "循环名单是什么",
    }


def render_list(data):
    if (
        not isinstance(data, dict)
        or data.get("complete") is not True
        or not valid_time(data.get("observedAt"))
        or not 0 <= time.time() - data["observedAt"] <= 15
    ):
        raise ValueError("cycle_list_not_fresh_or_complete")
    rows = data.get("players")
    if not isinstance(rows, list) or len(rows) > 2000:
        raise ValueError("invalid_cycle_list")
    checked = []
    seen = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("steamId"), str)
            or not re.fullmatch(r"[0-9]{17}", row["steamId"])
            or row["steamId"] in seen
        ):
            raise ValueError("invalid_cycle_identity")
        seen.add(row["steamId"])
        if "enabled" in row and type(row["enabled"]) is not bool:
            raise ValueError("invalid_cycle_enabled")
        if row.get("online") is not None and type(row["online"]) is not bool:
            raise ValueError("invalid_cycle_online")
        if not isinstance(row.get("name"), str) or not row["name"].strip():
            raise ValueError("invalid_cycle_name")
        if row.get("enabled", True):
            checked.append(row)
    lines = ["当前哈哈名单：" + str(len(checked)) + "人。", MEANING]
    for row in checked:
        name = "".join(c for c in row["name"] if c.isprintable() and c not in "@<>")[
            :60
        ]
        online = (
            "在线"
            if row.get("online") is True
            else "离线"
            if row.get("online") is False
            else "在线状态未知"
        )
        lines.append("• " + name + " " + row["steamId"] + " [" + online + "]")
    chunks = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > 1700:
            chunks.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        chunks.append(current)
    return chunks


class CycleListSupport:
    def __init__(self, store, client):
        self.db, self.client = store.db, client
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS admin_cycle_list_receipts(id TEXT PRIMARY KEY,created REAL)"
            )

    async def handle(self, controller, message, role, oid, tell):
        if self.db.execute(
            "SELECT id FROM admin_cycle_list_receipts WHERE id=?", (oid,)
        ).fetchone():
            await tell("此名单查询已处理；需要刷新时请重新发送“哈哈名单”。")
            return True
        scope = (message.who.platform, message.who.group, message.who.sender)
        controller.clear_drafts(scope)
        with self.db:
            self.db.execute(
                "DELETE FROM admin_cycle_list_receipts WHERE created<?",
                (time.time() - 86400,),
            )
            self.db.execute(
                "INSERT INTO admin_cycle_list_receipts VALUES(?,?)", (oid, time.time())
            )
        try:
            data = await self.client.cycle_list(
                {"actor": actor(message.who, role), "requestedAt": time.time()}
            )
            chunks = render_list(data)
        except Exception:
            await tell("当前哈哈名单暂不可用，请稍后重试。")
            return True
        for chunk in chunks:
            await tell(chunk)
        return True
