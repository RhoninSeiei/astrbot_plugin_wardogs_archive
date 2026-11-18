"""Read-only runtime checks for the configured OneBot account and backend."""

import asyncio
import time

from .io import ServiceError


async def verify_deployment(context, config, service):
    backend = await verify_backend_deployment(service)
    onebot = await verify_onebot_deployment(context, config)
    return dict(backend, **onebot)


async def verify_backend_deployment(service):
    query = service.key_file.read_text().strip()
    execution = service.kick_key_file.read_text().strip()
    if len(query) < 32 or len(execution) < 32 or query == execution:
        raise ValueError("independent_tokens_required")
    document = await service.request("GET", "/openapi.json")
    required = {"/v1/moderation/kicks", "/v1/moderation/operations/{operationId}"}
    if not required <= set(document.get("paths", {})):
        raise ValueError("moderation_routes_missing")
    data = await service.players()
    observed = data.get("observedAt")
    if (
        isinstance(observed, bool)
        or not isinstance(observed, (int, float))
        or not 0 <= time.time() - observed <= 90
        or not isinstance(data.get("players"), list)
    ):
        raise ValueError("players_not_fresh")
    try:
        await service.operation("wd-readiness-no-operation")
    except ServiceError as error:
        if error.status != 404:
            raise
    else:
        raise ValueError("unexpected_probe_operation")
    return {
        "ready": True,
        "checked_at": time.time(),
        "backend_routes": True,
        "independent_tokens": True,
    }


async def verify_onebot_deployment(context, config):
    platform = context.get_platform_inst(config["platform_id"])
    if platform is None or platform.meta().name != "aiocqhttp":
        raise ValueError("onebot_platform_missing")
    bot = platform.get_client()
    account = str(config["bot_account_id"])
    groups = config["allowed_groups"]
    if (
        not account.isascii()
        or not account.isdigit()
        or not isinstance(groups, list)
        or not 1 <= len(groups) <= 32
    ):
        raise ValueError("onebot_scope_invalid")

    async def call(action, **kwargs):
        return await asyncio.wait_for(
            bot.call_action(action=action, self_id=int(account), **kwargs), 10
        )

    login = await call("get_login_info")
    if not isinstance(login, dict) or str(login.get("user_id")) != account:
        raise ValueError("onebot_account_mismatch")
    joined_groups = await call("get_group_list", no_cache=True)
    if not isinstance(joined_groups, list):
        raise ValueError("onebot_group_list_invalid")
    joined_ids = set()
    for row in joined_groups:
        group_id = row.get("group_id") if isinstance(row, dict) else None
        if isinstance(group_id, bool) or not isinstance(group_id, (str, int)):
            raise ValueError("onebot_group_list_invalid")
        group_id = str(group_id)
        if (
            not group_id.isascii()
            or not group_id.isdigit()
            or not 1 <= len(group_id) <= 20
            or int(group_id) == 0
            or group_id in joined_ids
        ):
            raise ValueError("onebot_group_list_invalid")
        joined_ids.add(group_id)
    for group in groups:
        if not str(group).isascii() or not str(group).isdigit():
            raise ValueError("onebot_group_invalid")
        if str(group) not in joined_ids:
            raise ValueError("onebot_group_membership_unproven")
        info = await call("get_group_info", group_id=int(group), no_cache=True)
        if not isinstance(info, dict) or str(info.get("group_id")) != str(group):
            raise ValueError("onebot_group_membership_unproven")
    return {
        "ready": True,
        "groups_checked": len(groups),
        "checked_at": time.time(),
        "onebot_membership": True,
    }


async def verify_admin_deployment(service):
    if service.admin_key_file is None:
        raise ValueError("admin_token_required")
    token = service.admin_key_file.read_text().strip()
    others = [
        p.read_text().strip()
        for p in (service.key_file, service.kick_key_file)
        if p is not None
    ]
    if len(token) < 32 or token in others:
        raise ValueError("independent_admin_token_required")
    document = await service.request("GET", "/openapi.json")
    if "/v1/moderation/admin-kicks" not in document.get("paths", {}):
        raise ValueError("admin_moderation_route_missing")
    if callable(getattr(service, "admin_player_search", None)):
        required = {"/v1/moderation/player-search", "/v1/moderation/admin-restrictions"}
        if not required <= set(document.get("paths", {})):
            raise ValueError("admin_history_routes_missing")
    if callable(
        getattr(service, "recent_kills", None)
    ) and "/v1/moderation/recent-kills" not in document.get("paths", {}):
        raise ValueError("admin_replay_route_missing")
    if callable(
        getattr(service, "admin_cycle_add", None)
    ) and "/v1/moderation/admin-cycle-adds" not in document.get("paths", {}):
        raise ValueError("admin_cycle_route_missing")
    if callable(getattr(service, "admin_unban", None)) and not {
        "/v1/moderation/admin-unbans",
        "/v1/moderation/unban-search",
        "/v1/moderation/cycle-list",
    } <= set(document.get("paths", {})):
        raise ValueError("admin_unban_and_list_routes_missing")
    if callable(
        getattr(service, "current_restrictions", None)
    ) and "/v1/moderation/current-restrictions" not in document.get("paths", {}):
        raise ValueError("current_restrictions_route_missing")
    if callable(getattr(service, "player_kills", None)) and not {
        "/v1/moderation/player-kills",
        "/v1/moderation/admin-bans",
    } <= set(document.get("paths", {})):
        raise ValueError("player_kill_history_routes_missing")
    if callable(
        getattr(service, "player_sanction_summary", None)
    ) and "/v1/moderation/player-sanction-summary" not in document.get("paths", {}):
        raise ValueError("sanction_summary_route_missing")
    try:
        await service.admin_operation("wd-admin-readiness-no-operation")
    except ServiceError as error:
        if error.status != 404:
            raise
    else:
        raise ValueError("unexpected_admin_probe_operation")


async def verify_query_deployment(service):
    from .queries import count, fresh

    doc = await service.request("GET", "/openapi.json")
    if "/v1/match" not in doc.get("paths", {}):
        raise ValueError("match_route_missing")
    data = await service.match()
    if (
        not fresh(data.get("observedAt"))
        or type(data.get("scoreAvailable")) is not bool
    ):
        raise ValueError("invalid_match_snapshot")
    count(data.get("playerCount"))
    return {
        "ready": True,
        "checked_at": time.time(),
        "match_route": True,
        "snapshot_fresh": True,
        "score_available": data["scoreAvailable"],
    }
