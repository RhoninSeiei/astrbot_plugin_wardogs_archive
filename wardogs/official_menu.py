"""Official QQ command keyboards; buttons never grant execution authority."""

import asyncio
import logging

from .admin_auth import management_role, official_scope
from .joins import JoinAPI, JoinError

PUBLIC = [
    ("服况", "服况", True),
    ("服ID", "服id", True),
    ("在线人数", "人数", True),
    ("比分", "比分", True),
    ("封禁统计", "封禁统计", True),
    ("24小时封禁", "过去24小时封禁人数", True),
]
ADMIN = [
    ("服况", "服况", True),
    ("服ID", "服id", True),
    ("回溯1分钟", "回溯", True),
    ("回溯5分钟", "回溯5分钟", True),
    ("哈哈名单", "哈哈名单", True),
    ("封禁原因", "封禁原因 ", False),
    ("踢出玩家", "踢 ", False),
    ("循环惩罚", "循环 ", False),
    ("解封玩家", "解封 ", False),
    ("查询菜单", "查询菜单", True),
    ("击杀记录", "击杀记录 ", False),
]


def command(text):
    value = text.strip().lstrip("/").strip()
    if value in ("菜单", "帮助菜单", "命令菜单", "查询菜单"):
        return "public" if value == "查询菜单" else "auto"
    if value in ("管理菜单", "管理员菜单"):
        return "admin"
    return None


def keyboard(admin):
    buttons = ADMIN if admin else PUBLIC
    rows = []
    for offset in range(0, len(buttons), 2):
        row = []
        for index, (label, data, enter) in enumerate(buttons[offset : offset + 2]):
            restricted = admin and data not in ("服况", "服id", "查询菜单")
            row.append(
                {
                    "id": f"wd_{offset + index}",
                    "render_data": {"label": label, "visited_label": label, "style": 1},
                    "action": {
                        "type": 2,
                        "permission": {"type": 1 if restricted else 2},
                        "data": data,
                        "enter": enter,
                        "reply": False,
                        "unsupport_tips": "请@机器人输入对应命令",
                    },
                }
            )
        if len(rows) == 5:
            rows[-1]["buttons"].extend(row)
        else:
            rows.append({"buttons": row})
    return {"content": {"rows": rows}}


def text_menu(admin):
    if admin:
        return "WARDOGS 管理菜单\n查询：服况、服id。\n记录：回溯、回溯5分钟、哈哈名单、封禁原因 <昵称或ID>、击杀记录 <昵称或ID> [条数，最多50]。\n操作：踢 <昵称或ID>、循环 <昵称或ID>、解封 <昵称或ID>。\n处罚按钮只填入输入框；补充对象并发送后，仍执行原权限与候选确认。"
    return "WARDOGS 查询菜单\n服况、服id、人数、比分、封禁统计、过去24小时封禁人数。\n请@机器人发送命令；服ID可用于游戏左下角的ID加入。"


async def handle(event, context, config, queries):
    text = " ".join(getattr(c, "text", "") for c in event.get_messages()).strip()
    kind = command(text)
    if kind is None or event.get_platform_name() != "qq_official":
        return False
    who = official_scope(event, config, context, scope_key="official_query_scopes")
    if who is None:
        return False
    admin = official_scope(
        event, config, context
    ) is not None and await management_role(event, context, config) in (
        "owner",
        "admin",
    )
    if kind == "admin" and not admin:
        await event.send(
            event.plain_result("管理菜单仅限本群可核验的群主、管理员或面板授权成员。")
        )
        return True
    admin = admin and kind != "public"
    body = text_menu(admin)
    key = queries.history.fingerprint([str(event.message_obj.message_id), text])
    async with queries.lock:
        if queries.history.cached(who, "official_menu", key, ttl=86400) is not None:
            return True
        # Claim before awaiting the write; an uncertain response is not replayed.
        queries.history.remember(who, "official_menu", key, {"status": "dispatching"})
        if not config.get("official_menu_buttons_enabled", True):
            await event.send(event.plain_result(body))
            queries.history.remember(who, "official_menu", key, {"status": "text"})
            return True
        api = JoinAPI(context.get_platform_inst(event.get_platform_id()), who.group)
        try:
            result = await asyncio.wait_for(
                api.request(
                    "POST",
                    "/messages",
                    json={
                        "msg_type": 2,
                        "markdown": {"content": body},
                        "keyboard": keyboard(admin),
                        "msg_id": str(event.message_obj.message_id),
                        "msg_seq": 1,
                    },
                ),
                15,
            )
            if not isinstance(result, dict) or not result.get("id"):
                raise JoinError(0, "unknown_menu_send_result")
        except JoinError as error:
            if error.status in (400, 401, 403, 404, 405, 422):
                # Explicit rejection: it is safe to send the text alternative.
                await event.send(event.plain_result(body))
                queries.history.remember(who, "official_menu", key, {"status": "text"})
                return True
            queries.history.remember(who, "official_menu", key, {"status": "unknown"})
            logging.getLogger(__name__).warning(
                "WARDOGS official menu result unknown; no retry."
            )
            return True
        except BaseException as error:
            queries.history.remember(who, "official_menu", key, {"status": "unknown"})
            if isinstance(error, asyncio.CancelledError):
                raise
            logging.getLogger(__name__).warning(
                "WARDOGS official menu result unknown; no retry."
            )
            return True
        finally:
            await api.close()
        queries.history.remember(who, "official_menu", key, {"status": "sent"})
        # Direct native sends bypass the adapter's send bookkeeping.
        event._has_send_oper = True
        return True


async def send_restriction_offer(event, context, config, text):
    """One-use offer from the controller; no action occurs until its own confirm."""
    import re

    code = getattr(text, "restriction_code", None)
    if (
        event.get_platform_name() != "qq_official"
        or not isinstance(code, str)
        or not re.fullmatch(r"[a-f0-9]{12}", code)
    ):
        return False
    who = official_scope(event, config, context)
    if who is None or await management_role(event, context, config) not in (
        "owner",
        "admin",
    ):
        raise PermissionError("restriction_offer_scope_unverified")
    buttons = []
    for i, (label, data) in enumerate(
        [("确认全部补封", "确认补封全部 "), ("暂不补封", "取消批量补封 ")]
    ):
        buttons.append(
            {
                "id": "wd_bulk_" + str(i),
                "render_data": {
                    "label": label,
                    "visited_label": label,
                    "style": 1 if i == 0 else 0,
                },
                "action": {
                    "type": 2,
                    "permission": {"type": 1},
                    "data": data + code,
                    "enter": True,
                    "reply": False,
                    "unsupport_tips": "请@机器人按文字说明确认或取消",
                },
            }
        )
    api = JoinAPI(context.get_platform_inst(event.get_platform_id()), who.group)

    async def post(payload):
        result = await asyncio.wait_for(
            api.request("POST", "/messages", json=payload), 15
        )
        if not isinstance(result, dict) or not result.get("id"):
            raise JoinError(0, "unknown_offer_result")

    text_payload = {
        "msg_type": 0,
        "content": str(text),
        "msg_id": str(event.message_obj.message_id),
        "msg_seq": 1,
    }
    try:
        if config.get("official_menu_buttons_enabled", True):
            try:
                await post(
                    {
                        "msg_type": 2,
                        "markdown": {"content": str(text)},
                        "keyboard": {"content": {"rows": [{"buttons": buttons}]}},
                        "msg_id": str(event.message_obj.message_id),
                        "msg_seq": 1,
                    }
                )
            except JoinError as error:
                if error.status not in (400, 401, 403, 404, 405, 422):
                    raise
                # The confirmation window requires a positively acknowledged
                # text fallback too; a swallowed SDK timeout cannot publish it.
                await post(dict(text_payload, msg_seq=2))
        else:
            await post(text_payload)
    finally:
        await api.close()
    event._has_send_oper = True
    return True
