"""Low-population encouragement and at-most-once natural-day rally attempts."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import astrbot.api.message_components as Comp

from .joins import JoinAPI

BEIJING = timezone(timedelta(hours=8))


class PopulationReply(str):
    def __new__(cls, text, population=None):
        value = super().__new__(cls, text)
        value.population = population
        return value


def population_reply(current, capacity=""):
    text = f"服务器在线人数：{current}{capacity}人。"
    if current < 20:
        text += "\n服务器人数低于20，需要更多玩家进入才能开启。\n加油啊打灰的大哥哥"
    return PopulationReply(text, current)


class DailyRally:
    def __init__(self, db, clock=None):
        self.db = db
        self.clock = clock or time.time
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS wd_population_rallies(day TEXT PRIMARY KEY,claimed REAL,status TEXT,platform TEXT,grp TEXT)"
            )

    async def send_once(self, who, reply, notify_all):
        current = getattr(reply, "population", None)
        if type(current) is not int or not 15 <= current <= 19 or notify_all is None:
            return False
        now = self.clock()
        day = datetime.fromtimestamp(now, BEIJING).date().isoformat()
        with self.db:
            claimed = self.db.execute(
                "INSERT OR IGNORE INTO wd_population_rallies VALUES(?,?,'reserved',?,?)",
                (day, now, who.platform, who.group),
            ).rowcount
        if not claimed:
            return False
        try:
            await asyncio.wait_for(
                notify_all(
                    str(reply)
                    + f"\n还差{20 - current}人就能开启，大家进入服务器一起打灰！"
                ),
                20,
            )
        except BaseException as error:
            with self.db:
                self.db.execute(
                    "UPDATE wd_population_rallies SET status='unknown' WHERE day=?",
                    (day,),
                )
            if isinstance(error, asyncio.CancelledError):
                raise
            return False
        with self.db:
            self.db.execute(
                "UPDATE wd_population_rallies SET status='sent' WHERE day=?", (day,)
            )
        return True


async def send_all(event, context, text):
    adapter = event.get_platform_name()
    group = str(event.get_group_id())
    platform = context.get_platform_inst(event.get_platform_id())
    if not group or platform is None or platform.meta().name != adapter:
        raise PermissionError("unverified_rally_platform")
    if adapter == "qq_official":
        api = JoinAPI(platform, group)
        try:
            state = await api.state()
        finally:
            await api.close()
        if state.get("member_role") not in ("owner", "admin"):
            raise PermissionError("rally_bot_not_admin")
        # The official adapter drops generic AtAll components; retain native markup.
        await event.send(event.plain_result("<@everyone> " + text))
    elif adapter == "aiocqhttp":
        account = str(event.message_obj.self_id)
        state = await asyncio.wait_for(
            platform.get_client().call_action(
                action="get_group_member_info",
                self_id=int(account),
                group_id=int(group),
                user_id=int(account),
                no_cache=True,
            ),
            10,
        )
        if (
            str(state.get("user_id")) != account
            or str(state.get("group_id")) != group
            or state.get("role") not in ("owner", "admin")
        ):
            raise PermissionError("rally_bot_not_admin")
        await event.send(event.chain_result([Comp.AtAll(), Comp.Plain(text)]))
    else:
        raise PermissionError("unsupported_rally_platform")
