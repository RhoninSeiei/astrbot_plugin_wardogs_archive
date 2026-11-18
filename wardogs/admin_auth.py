"""Platform role proof and exact bot/group scope for explicit manual orders."""

import asyncio

from .domain import Identity


def official_scope(event, config, context, *, scope_key="official_admin_scopes"):
    if event.get_platform_name() != "qq_official" or not event.get_group_id():
        return None
    platform_id = event.get_platform_id()
    inst = context.get_platform_inst(platform_id)
    if inst is None or inst.meta().name != "qq_official":
        return None
    appid = str(getattr(inst, "appid", ""))
    group = str(event.get_group_id())
    allowed = config.get(scope_key, [])
    if (
        not appid.isascii()
        or not appid.isdigit()
        or f"{platform_id}|{appid}|{group}" not in allowed
    ):
        return None
    sender = str(event.get_sender_id())
    if not sender or len(sender) > 128:
        return None
    return Identity("qq_official:" + platform_id + ":" + appid, group, sender)


async def group_role(event, context):
    if event.get_platform_name() == "qq_official":
        raw = getattr(getattr(event.message_obj, "raw_message", None), "raw_data", None)
        if not isinstance(raw, dict) or str(raw.get("group_openid", "")) != str(
            event.get_group_id()
        ):
            return ""
        author = raw.get("author")
        if (
            not isinstance(author, dict)
            or author.get("member_openid") != event.get_sender_id()
            or author.get("bot") is True
        ):
            return ""
        role = author.get("member_role")
        return role if role in ("owner", "admin") else ""
    if event.get_platform_name() != "aiocqhttp":
        return ""
    try:
        inst = context.get_platform_inst(event.get_platform_id())
        result = await asyncio.wait_for(
            inst.get_client().call_action(
                action="get_group_member_info",
                self_id=int(event.message_obj.self_id),
                group_id=int(event.get_group_id()),
                user_id=int(event.get_sender_id()),
                no_cache=True,
            ),
            10,
        )
        if str(result.get("group_id")) != str(event.get_group_id()) or str(
            result.get("user_id")
        ) != str(event.get_sender_id()):
            return ""
        role = result.get("role")
        return role if role in ("owner", "admin") else ""
    except Exception:
        return ""


def qq_number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    value = str(value)
    return (
        value
        if value.isascii()
        and value.isdigit()
        and 5 <= len(value) <= 12
        and value[0] != "0"
        else ""
    )


def _configured_binding(event, config, context):
    values = config.get("authorized_qq", [])
    if not isinstance(values, list) or len(values) > 200:
        return ""
    allowed = {qq_number(v) for v in values} - {""}
    who = official_scope(event, config, context)
    if who is None or not allowed:
        return ""
    raw = getattr(getattr(event.message_obj, "raw_message", None), "raw_data", None)
    if not isinstance(raw, dict) or str(raw.get("group_openid", "")) != who.group:
        return ""
    author = raw.get("author")
    if (
        not isinstance(author, dict)
        or author.get("member_openid") != who.sender
        or author.get("bot") is True
    ):
        return ""
    rows = config.get("official_qq_bindings", [])
    if not isinstance(rows, list) or len(rows) > 500:
        return ""
    platform, app = who.platform.removeprefix("qq_official:").rsplit(":", 1)
    candidates = []
    keys = {"qq", "platform_id", "app_id", "group_id", "member_id"}
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) - keys - {"__template_key"}
            or row.get("__template_key", "qq_member") != "qq_member"
        ):
            continue
        if not all(
            isinstance(row.get(k), str)
            and row[k]
            and row[k] == row[k].strip()
            and len(row[k]) <= 128
            for k in keys
        ):
            continue
        if qq_number(row["qq"]) in allowed and (
            row["platform_id"],
            row["app_id"],
            row["group_id"],
        ) == (platform, app, who.group):
            candidates.append(row)
    matches = [r for r in candidates if r["member_id"] == who.sender]
    if len(matches) != 1:
        return ""
    qq = matches[0]["qq"]
    return qq if sum(r["qq"] == qq for r in candidates) == 1 else ""


async def management_authorization(event, context, config):
    """Return an effective plugin role; never mutate the platform author role."""
    platform_role = await group_role(event, context)
    if platform_role:
        return {
            "role": platform_role,
            "source": "platform_role",
            "qq": "",
            "platform_role": platform_role,
        }
    qq = ""
    reported_role = ""
    if event.get_platform_name() == "qq_official":
        qq = _configured_binding(event, config, context)
        if qq:
            raw = event.message_obj.raw_message.raw_data
            value = raw["author"].get("member_role")
            reported_role = value if value in ("owner", "admin", "member") else ""
    elif event.get_platform_name() == "aiocqhttp" and not config.get(
        "official_only", False
    ):
        values = config.get("authorized_qq", [])
        sender = qq_number(event.get_sender_id())
        scope_ok = (
            event.get_platform_id() == config.get("platform_id")
            and str(getattr(event.message_obj, "self_id", ""))
            == str(config.get("bot_account_id", ""))
            and str(event.get_group_id()) in config.get("allowed_groups", [])
        )
        if (
            scope_ok
            and isinstance(values, list)
            and sender
            and sender in {qq_number(v) for v in values}
        ):
            try:
                result = await asyncio.wait_for(
                    context.get_platform_inst(event.get_platform_id())
                    .get_client()
                    .call_action(
                        action="get_group_member_info",
                        self_id=int(event.message_obj.self_id),
                        group_id=int(event.get_group_id()),
                        user_id=int(sender),
                        no_cache=True,
                    ),
                    10,
                )
                if (
                    str(result.get("group_id")) == str(event.get_group_id())
                    and str(result.get("user_id")) == sender
                    and result.get("role") == "member"
                ):
                    qq = sender
                    reported_role = "member"
            except Exception:
                pass
    return {
        "role": "admin" if qq else "",
        "source": "configured_qq" if qq else "denied",
        "qq": qq,
        "platform_role": reported_role,
    }


async def management_role(event, context, config):
    return (await management_authorization(event, context, config))["role"]
