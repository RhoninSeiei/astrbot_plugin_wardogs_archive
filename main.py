"""Scoped OneBot reports and platform-verified administrator kick orders."""

import asyncio
import json
import logging
import os
import re
import shlex
import time
from pathlib import Path

import astrbot.api.message_components as Comp
from aiocqhttp.exceptions import ApiNotAvailable, NetworkError
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter import HandlerFilter
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_temp_path,
)

from .wardogs.actions import Announcements
from .wardogs.admin import AdminKicks
from .wardogs.admin_auth import (
    group_role,
    management_authorization,
    management_role,
    official_scope,
)
from .wardogs.domain import Identity, Message, Store
from .wardogs.flow import Reports
from .wardogs.identity_monitor import IdentityMonitor, notice_target, prepare_notice
from .wardogs.io import Media, ServiceClient
from .wardogs.joins import JoinApprovals
from .wardogs.model import DEFAULT_PROVIDER, FALLBACK_PROVIDER, ContextModel
from .wardogs.official_menu import handle as handle_official_menu
from .wardogs.official_menu import send_restriction_offer
from .wardogs.queries import PublicQueries
from .wardogs.rally import send_all
from .wardogs.readiness import (
    verify_admin_deployment,
    verify_backend_deployment,
    verify_onebot_deployment,
    verify_query_deployment,
)
from .wardogs.retrieval import TRIGGERS
from .wardogs.server_identity import command as identity_command

PLATFORM_START_RETRY_SECONDS = 30


def account_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    value = str(value)
    return value if value.isascii() and value.isdigit() and len(value) <= 20 else ""


class ScopedFilter(HandlerFilter):
    def __init__(self, raise_error=True):
        self.raise_error = raise_error

    def filter(self, event, cfg):
        return (
            event.get_platform_name() == "aiocqhttp"
            and bool(account_id(getattr(event.message_obj, "self_id", None)))
            and bool(event.get_group_id())
        )


class AdminFilter(HandlerFilter):
    def __init__(self, raise_error=True):
        self.raise_error = raise_error

    def filter(self, event, cfg):
        return event.get_platform_name() in ("aiocqhttp", "qq_official") and bool(
            event.get_group_id()
        )


class WardogsPlugin(Star):
    name = "astrbot_plugin_wardogs"

    def __init__(self, context: Context, config: dict):
        super().__init__(context, config)
        self.config = config
        self.ready = False
        self.data_root = Path(get_astrbot_plugin_data_path()) / self.name
        self.gate = asyncio.Semaphore(4)
        self._closing = False
        self.intake_tasks = set()
        self.join_task = None
        self.identity_task = None
        self.join_approvals = None
        self.startup_task = None
        self.onebot_ready = False
        self._lifecycle_lock = asyncio.Lock()

    async def initialize(self):
        async with self._lifecycle_lock:
            if self.ready or self._closing:
                return
            await self._initialize()

    def startup_state(
        self, reason, ready_check=None, filename="deployment-readiness.json"
    ):
        path = self.data_root / filename
        temporary = path.with_suffix(".json.tmp")
        state = dict(ready_check or {})
        state.update(ready=bool(ready_check), checked_at=time.time())
        if not ready_check:
            state["reason"] = reason
        try:
            self.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with os.fdopen(
                os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                    0o600,
                ),
                "w",
            ) as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(state, handle)
            temporary.replace(path)
        except OSError:
            if ready_check:
                raise
            # A diagnostic failure must not prevent shutdown or mask its cause.
            logging.getLogger(__name__).error(
                "WARDOGS blocked readiness state could not be saved."
            )

    async def start_after_platform(self):
        last_reason = None
        while not self._closing and not self.onebot_ready:
            try:
                check = await verify_onebot_deployment(self.context, self.config)
                if check.get("onebot_membership") is not True:
                    raise ValueError("onebot_membership_unproven")
                if self._closing or not self.ready:
                    return
                self.onebot_ready = True
                self.startup_state("ready", check, filename="onebot-readiness.json")
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.onebot_ready = False
                reason = "onebot_identity_unproven"
                if isinstance(error, ApiNotAvailable):
                    reason = "onebot_api_unavailable"
                elif isinstance(error, (TimeoutError, NetworkError)):
                    reason = "onebot_api_timeout"
                self.startup_state(reason, filename="onebot-readiness.json")
                if reason != last_reason:
                    logging.getLogger(__name__).warning(
                        "WARDOGS OneBot verification unavailable; OneBot intake remains disabled; official scopes retain independent checks."
                    )
                    last_reason = reason
                await asyncio.sleep(PLATFORM_START_RETRY_SECONDS)

    async def _initialize(self):
        if self.ready or self._closing:
            return
        self.startup_state("verifying_deployment")
        self.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = Store(self.data_root / "cases.db")
        try:
            self.client = ServiceClient(
                self.config.get("service_base", "http://127.0.0.1:18761"),
                self.config.get("service_token_file", ""),
                self.config.get("execution_token_file", ""),
                self.config.get("admin_execution_token_file", ""),
            )
            self.media = Media(
                self.config.get(
                    "media_hosts",
                    [
                        "gchat.qpic.cn",
                        "multimedia.nt.qq.com",
                        "multimedia.nt.qq.com.cn",
                        "c2cpicdw.qpic.cn",
                    ],
                ),
                cache_root=get_astrbot_temp_path(),
            )
            rules = {}
            for key in ["outside_combat", "base_camping", "cheating"]:
                text = self.config.get(key + "_rule", "").strip()
                if text:
                    rules[key] = {
                        "version": str(self.config.get("rule_version", "1")),
                        "text": text,
                    }
            self.reports = Reports(
                self.store,
                None,
                self.client,
                self.media,
                rules=rules,
                wait=int(self.config.get("collect_seconds", 8)),
                auto_execute=bool(self.config.get("auto_execute", False)),
                assessment_timeout=int(
                    self.config.get("assessment_timeout_seconds", 180)
                ),
            )
            check = {"ready": True}
            if self.config.get("startup_checks", False):
                check = await verify_backend_deployment(self.client)
            if self.config.get("admin_kicks_enabled", False):
                await verify_admin_deployment(self.client)
            if self.config.get("public_queries_enabled", False):
                query_check = await verify_query_deployment(self.client)
                path = self.data_root / "public-query-readiness.json"
                with os.fdopen(
                    os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w"
                ) as handle:
                    json.dump(query_check, handle)
            if self._closing:
                raise asyncio.CancelledError("WARDOGS was terminated during startup")
            self.public_queries = PublicQueries(self.client, self.reports.history)
            self.actions = Announcements(self.store, self.client)
            self.admin_kicks = AdminKicks(self.store, self.client)
            self.maintenance = asyncio.create_task(
                self.maintain(), name="wardogs-maintenance"
            )
            if self.config.get("auto_approve_joins_enabled", False):
                self.join_approvals = JoinApprovals(
                    self.store,
                    self.context,
                    self.config.get("official_join_scopes", []),
                    self.data_root / "join-approval-state.json",
                )
                self.join_task = asyncio.create_task(
                    self.join_approvals.run(), name="wardogs-join-approvals"
                )
            self.ready = True
            if self.config.get(
                "server_identity_auto_notice_enabled", False
            ) and not self.config.get("official_only", False):
                target = notice_target(self.config)
                self.identity_monitor = IdentityMonitor(
                    self.store.db,
                    self.client,
                    target,
                    lambda: prepare_notice(self.context, target),
                )
                self.identity_task = asyncio.create_task(
                    self.identity_monitor.run(), name="wardogs-server-identity"
                )
            self.startup_state("ready", check)
            self.onebot_ready = not self.config.get(
                "startup_checks", False
            ) and not self.config.get("official_only", False)
            if self.config.get("official_only", False):
                self.startup_state(
                    "disabled_official_only", filename="onebot-readiness.json"
                )
            elif not self.onebot_ready:
                self.startup_state(
                    "awaiting_verification", filename="onebot-readiness.json"
                )
                self.startup_task = asyncio.create_task(
                    self.start_after_platform(), name="wardogs-onebot-verification"
                )
        except BaseException:
            if self.identity_task:
                self.identity_task.cancel()
                await asyncio.gather(self.identity_task, return_exceptions=True)
            if self.join_task:
                self.join_task.cancel()
                await asyncio.gather(self.join_task, return_exceptions=True)
            if self.join_approvals:
                await self.join_approvals.close()
            maintenance = getattr(self, "maintenance", None)
            if maintenance:
                maintenance.cancel()
                await asyncio.gather(maintenance, return_exceptions=True)
            for name in ("media", "client"):
                resource = getattr(self, name, None)
                if resource is not None:
                    await resource.close()
            self.store.close()
            self.ready = False
            self.startup_state("deployment_verification_failed")
            raise

    def allowed(self, event):
        return (
            self.ready
            and not self._closing
            and (not self.config.get("startup_checks", False) or self.onebot_ready)
            and not self.config.get("official_only", False)
            and ScopedFilter().filter(event, {})
            and bool(self.config.get("platform_id"))
            and event.get_platform_id() == self.config.get("platform_id")
            and bool(account_id(self.config.get("bot_account_id")))
            and account_id(getattr(event.message_obj, "self_id", None))
            == account_id(self.config.get("bot_account_id"))
            and str(event.get_sender_id())
            != account_id(self.config.get("bot_account_id"))
            and str(event.get_group_id())
            in {str(g) for g in self.config.get("allowed_groups", [])}
        )

    def query_identity(self, event):
        if self.allowed(event):
            return self.message(event).who
        if (
            not self.ready
            or self._closing
            or not self.config.get("public_queries_enabled", False)
        ):
            return None
        target = str(getattr(event.message_obj, "self_id", ""))
        if not target or not any(
            isinstance(c, Comp.At) and str(c.qq) == target for c in event.get_messages()
        ):
            return None
        who = official_scope(
            event, self.config, self.context, scope_key="official_query_scopes"
        )
        raw = getattr(getattr(event.message_obj, "raw_message", None), "raw_data", None)
        if (
            who is None
            or not isinstance(raw, dict)
            or str(raw.get("group_openid", "")) != who.group
        ):
            return None
        author = raw.get("author")
        if (
            not isinstance(author, dict)
            or author.get("member_openid") != who.sender
            or author.get("bot") is True
        ):
            return None
        return who

    @filter.custom_filter(AdminFilter, priority=250)
    async def on_public_query(self, event: AstrMessageEvent):
        if not self.config.get("public_queries_enabled", False):
            return
        who = self.query_identity(event)
        if who is None:
            return
        target = str(getattr(event.message_obj, "self_id", ""))
        if not target or not any(
            isinstance(c, Comp.At) and str(c.qq) == target for c in event.get_messages()
        ):
            return
        text = " ".join(
            c.text for c in event.get_messages() if isinstance(c, Comp.Plain)
        ).strip()
        if (
            not text
            or len(text) > 1000
            or not getattr(event.message_obj, "message_id", None)
        ):
            return
        task = asyncio.current_task()
        self.intake_tasks.add(task)
        try:

            async def notify(value):
                await event.send(event.plain_result(value))

            message = Message(who, str(event.message_obj.message_id), time.time(), text)
            if (
                text in ("我的管理身份", "我的授权信息")
                and event.get_platform_name() == "qq_official"
            ):
                platform, appid = who.platform.removeprefix("qq_official:").rsplit(
                    ":", 1
                )
                await notify(
                    "QQ官方身份信息（只读，不代表获得管理权限）\n平台实例ID："
                    + platform
                    + "\nAppID："
                    + appid
                    + "\ngroup_openid："
                    + who.group
                    + "\nmember_openid："
                    + who.sender
                    + "\n官方接口不提供真实QQ号。请由管理者核对QQ后，在插件设置中填写授权QQ及对应绑定。"
                )
                event.stop_event()
                return
            if await handle_official_menu(
                event, self.context, self.config, self.public_queries
            ):
                event.stop_event()
                return
            async with self.gate:
                if await self.public_queries.handle(
                    message,
                    None
                    if identity_command(text) is not None
                    else self.assessor(event),
                    notify,
                    notify_all=lambda text: send_all(event, self.context, text),
                ):
                    event.stop_event()
        finally:
            self.intake_tasks.discard(task)

    @staticmethod
    def message(event):
        images = []
        quoted = []
        reply_id = ""
        reply_text = ""
        for c in event.message_obj.message:
            if isinstance(c, Comp.Image):
                url = c.url or c.file
                if isinstance(url, str):
                    images.append(url)
            elif isinstance(c, Comp.Reply):
                reply_id = str(c.id)
                reply_text = str(c.message_str or c.text or "")[:4000]
                for q in c.chain or []:
                    if isinstance(q, Comp.Image):
                        quoted.append(q.url or q.file)
        display_name = event.get_sender_name()
        if (
            not isinstance(display_name, str)
            or display_name == event.get_sender_id()
            or not all(ch.isprintable() for ch in display_name)
        ):
            display_name = ""
        return Message(
            Identity(
                "aiocqhttp:"
                + str(event.get_platform_id())
                + ":"
                + account_id(getattr(event.message_obj, "self_id", None)),
                str(event.get_group_id()),
                str(event.get_sender_id()),
            ),
            str(event.message_obj.message_id),
            time.time(),
            event.get_message_str()[:4000],
            tuple(images[:4]),
            reply_id,
            tuple(quoted[:4]),
            reply_text,
            display_name.strip()[:80],
        )

    @filter.custom_filter(AdminFilter, priority=300)
    async def on_admin_setup(self, event: AstrMessageEvent):
        if (
            not self.ready
            or self._closing
            or event.get_platform_name() != "qq_official"
        ):
            return
        text = (
            " ".join(c.text for c in event.get_messages() if isinstance(c, Comp.Plain))
            .strip()
            .lstrip("/")
        )
        challenge = self.config.get("official_setup_challenge", "")
        if (
            not isinstance(challenge, str)
            or not challenge
            or text not in ("wd 会话核验 " + challenge, "wd sid " + challenge)
        ):
            return
        target = str(getattr(event.message_obj, "self_id", ""))
        if not any(
            isinstance(c, Comp.At) and str(c.qq) == target for c in event.get_messages()
        ):
            return
        inst = self.context.get_platform_inst(event.get_platform_id())
        if inst is None or inst.meta().name != "qq_official":
            return
        appid = str(getattr(inst, "appid", ""))
        pair = str(event.get_platform_id()) + "|" + appid
        if pair not in self.config.get("official_setup_platforms", []):
            return
        raw = getattr(getattr(event.message_obj, "raw_message", None), "raw_data", None)
        if (
            not isinstance(raw, dict)
            or str(raw.get("group_openid", "")) != str(event.get_group_id())
            or not event.get_group_id()
        ):
            return
        record = {
            "platform_id": event.get_platform_id(),
            "appid": appid,
            "group_openid": str(event.get_group_id()),
            "role": await group_role(event, self.context),
            "message_id": str(event.message_obj.message_id),
            "received_at": time.time(),
        }
        path = self.data_root / "official-setup.json"
        with os.fdopen(
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600),
            "w",
        ) as handle:
            json.dump(record, handle)
        event.stop_event()
        await event.send(
            event.plain_result(
                "当前群会话核验已记录；平台角色："
                + (record["role"] or "未核实")
                + "。本次没有执行处罚，人工踢出尚未启用。"
            )
        )

    @filter.custom_filter(AdminFilter, priority=200)
    async def on_admin_order(self, event: AstrMessageEvent):
        if (
            not self.ready
            or self._closing
            or not self.config.get("admin_kicks_enabled", False)
        ):
            return
        command_text = " ".join(
            c.text for c in event.get_messages() if isinstance(c, Comp.Plain)
        ).strip()
        if not command_text or len(command_text) > 1000:
            return
        if not getattr(event.message_obj, "message_id", None):
            return
        target = str(getattr(event.message_obj, "self_id", ""))
        if not any(
            isinstance(c, Comp.At) and str(c.qq) == target for c in event.get_messages()
        ):
            return
        if event.get_platform_name() == "aiocqhttp":
            if not self.allowed(event):
                return
            who = self.message(event).who
        else:
            who = official_scope(event, self.config, self.context)
            if who is None:
                return
        task = asyncio.current_task()
        self.intake_tasks.add(task)
        try:
            attachment_message = self.message(event)
            message = Message(
                who,
                str(event.message_obj.message_id),
                time.time(),
                command_text,
                images=attachment_message.images,
                reply_images=attachment_message.reply_images,
            )
            authorization = await management_authorization(
                event, self.context, self.config
            )
            role = authorization["role"]
            if authorization["source"] == "configured_qq":
                try:
                    await self.audit_management_authorization(
                        who, message.id, authorization
                    )
                except Exception:
                    await event.send(
                        event.plain_result("面板授权审计暂不可用，本次未执行管理操作。")
                    )
                    event.stop_event()
                    return

            async def notify(text):
                if await send_restriction_offer(event, self.context, self.config, text):
                    return
                await event.send(event.plain_result(text))

            if await self.admin_kicks.handle(
                message,
                role,
                notify,
                model=self.assessor(event),
                media=getattr(self, "media", None),
                role_check=lambda: management_role(event, self.context, self.config),
            ):
                event.stop_event()
        finally:
            self.intake_tasks.discard(task)

    async def audit_management_authorization(self, who, message_id, proof):
        db = self.admin_kicks.db
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS configured_qq_authorizations(platform TEXT,grp TEXT,sender TEXT,message_id TEXT,qq TEXT,platform_role TEXT,effective_role TEXT,source TEXT,created REAL,PRIMARY KEY(platform,grp,sender,message_id))"
            )
            db.execute(
                "DELETE FROM configured_qq_authorizations WHERE created<?",
                (time.time() - 90 * 86400,),
            )
            db.execute(
                "INSERT OR IGNORE INTO configured_qq_authorizations VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    who.platform,
                    who.group,
                    who.sender,
                    message_id,
                    proof["qq"],
                    proof["platform_role"],
                    proof["role"],
                    proof["source"],
                    time.time(),
                ),
            )

    def assessor(self, event):
        return ContextModel(
            self.context,
            event.unified_msg_origin,
            vision_provider_id=self.config.get(
                "assessment_provider_id", DEFAULT_PROVIDER
            ),
            default_provider_id=self.config.get(
                "default_provider_id",
                self.config.get("assessment_provider_id", DEFAULT_PROVIDER),
            ),
            fallback_provider_id=self.config.get(
                "fallback_provider_id", FALLBACK_PROVIDER
            ),
        )

    @filter.custom_filter(ScopedFilter, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        if not self.allowed(event):
            return
        task = asyncio.current_task()
        self.intake_tasks.add(task)
        try:
            await self._handle_message(event)
        finally:
            self.intake_tasks.discard(task)

    @staticmethod
    def addressed_elsewhere(event):
        """Route a leading, current-message @ without interpreting its nickname."""
        target = str(getattr(event.message_obj, "self_id", ""))
        segments = []
        for c in event.get_messages():
            if isinstance(c, Comp.At):
                segments.append(("at", str(c.qq)))
            elif isinstance(c, Comp.Plain):
                segments.append(("text", c.text))
        # OneBot may omit an At component if its nickname lookup failed.
        # Only inspect native top-level segments, never quoted message content.
        raw = getattr(event.message_obj, "raw_message", None)
        if (
            isinstance(raw, dict)
            and str(raw.get("self_id", "")) == target
            and isinstance(raw.get("message"), list)
        ):
            native = []
            for c in raw["message"]:
                if not isinstance(c, dict) or not isinstance(c.get("data"), dict):
                    continue
                if c.get("type") == "at":
                    native.append(("at", str(c["data"].get("qq", ""))))
                elif c.get("type") == "text":
                    native.append(("text", str(c["data"].get("text", ""))))
            if any(kind == "at" for kind, _ in native):
                segments = native
        if any(kind == "at" and value == target for kind, value in segments):
            return False
        for kind, value in segments:
            if kind == "text" and value.strip():
                return False
            if kind == "at":
                return bool(account_id(value)) and value != target
        return False

    async def _handle_message(self, event):
        m = self.message(event)
        if self.addressed_elsewhere(event):
            self.reports.history.record(m)
            self.reports.history.mark(m, "command")
            return
        if not self.reports.observe(m):
            return

        async def notify(text):
            await event.send(event.plain_result(text))

        text = m.text.strip().lstrip("/")
        if text.startswith("wd "):
            if await self.command(event, m, text[3:], notify):
                self.reports.history.mark(m, "command")
                event.stop_event()
                return
        explicit = bool(
            re.match(r"^(?:wd\s+)?举报(?:\s|[:：])", text)
        ) and not re.search(
            r"怎么|如何|流程|入口|进度|有用|是什么|吗|取消|撤回|撤销|不是|不要|不想|没打算|？|\?",
            text,
        )
        is_report = explicit
        discovery = None
        retrieved = None
        trigger = bool(TRIGGERS.search(text))
        if (
            text
            and (trigger or event.is_at_or_wake_command)
            and not (explicit and (m.images or m.reply_images or m.reply_to))
        ):
            history = self.reports.history
            try:
                if not self.reports._rate(m.who, m.at, "intent"):
                    await self.reports.ingest(
                        m, False, notify, self.assessor(event), observed=True
                    )
                    return
                async with self.gate:
                    discovery = await asyncio.wait_for(
                        self.assessor(event).discover(m, history), timeout=95
                    )
                is_report = discovery["is_report"]
                retrieved = discovery["messages"]
                previous = discovery.get("prior_case_id")
                previous_case = (
                    self.store.visible(previous, m.who) if previous else None
                )
                if previous_case and previous_case["status"] != "technical_failure":
                    history.mark(m, "consumed")
                    event.stop_event()
                    return
                if not is_report:
                    history.mark(m, "irrelevant_text")
            except Exception:
                history.mark(m, "intent_error")
                if not explicit:
                    if event.is_at_or_wake_command and trigger:
                        await notify(
                            "举报线索检索暂不可用，请使用“举报 玩家ID 原因”并附图。"
                        )
                        event.stop_event()
                        return
                    await self.reports.ingest(
                        m, False, notify, self.assessor(event), observed=True
                    )
                    return
        cid = await self.reports.ingest(
            m,
            is_report,
            notify,
            self.assessor(event),
            observed=True,
            retrieved=retrieved,
        )
        if cid and discovery and discovery.get("cache_key"):
            self.reports.history.remember(
                m.who, "retrieval_case", discovery["cache_key"], cid
            )
        if is_report:
            event.stop_event()

    async def command(self, event, m, text, notify):
        parts = text.split(maxsplit=1)
        cmd = parts[0] if parts else ""
        arg = parts[1] if len(parts) > 1 else ""
        if cmd == "举报":
            return False
        if cmd == "补充":
            cid = arg.split(maxsplit=1)[0] if arg else ""
            if not await self.reports.supplement(cid, m, notify, self.assessor(event)):
                await notify("案件不可补充、无权限或已有处置记录。")
            return True
        if cmd == "举报进度":
            await notify(
                await self.reports.progress(arg.strip(), m.who, event.is_admin())
            )
            return True
        if cmd == "帮助":
            await notify(
                "发送“举报 玩家ID 原因”并携图；可在8秒内补图或引用图片。命令：/wd 状态、玩家、查询 <昵称或ID>、补充 <案件号>、举报进度 <案件号>。"
            )
            return True
        if cmd in ("公告", "确认", "取消", "任务"):
            if not event.is_admin():
                await notify(
                    "此管理操作需要 AstrBot 管理员权限，举报向所有群成员开放。"
                )
                return True
            try:
                if cmd == "公告":
                    texts = shlex.split(arg)
                    code = self.actions.draft(m.who, texts)
                    await notify(
                        "三语公告预览：\n"
                        + "\n".join(texts)
                        + "\n120秒内发送 /wd 确认 "
                        + code
                    )
                elif cmd == "确认":
                    result = await self.actions.confirm(m.who, arg.strip())
                    await notify("公告已入队，尚未确认送达。操作号：" + arg.strip())
                elif cmd == "取消":
                    await notify(
                        "草稿已取消。"
                        if self.actions.cancel(m.who, arg.strip())
                        else "草稿不可取消。"
                    )
                else:
                    draft = self.actions.result(m.who, arg.strip())
                    if not draft:
                        await notify("操作不存在或无权限。")
                        return True
                    statuses = []
                    for job in draft[1].get("jobIds", []):
                        result = await self.client.job(job)
                        statuses.append(str(result.get("status", "unknown")))
                    await notify("公告状态：" + draft[0] + "；" + ", ".join(statuses))
            except ValueError:
                await notify(
                    '草稿不可用或参数不完整。公告格式：/wd 公告 "中文" "English" "日本語"。'
                )
            except Exception:
                await notify("操作结果待核实，请查询原操作，勿重复提交。")
            return True
        if cmd not in ("状态", "玩家", "查询"):
            return False
        try:
            if cmd == "状态":
                data = await self.client.state()
                at = data.get("lastSuccessfulPoll")
                fresh = isinstance(at, (int, float)) and 0 <= time.time() - at <= 90
                await notify(
                    f"在线人数：{data.get('playerCount', '未知')}；队列：{data.get('queueDepth', '未知')}；数据{'有效' if fresh else '过期'}。"
                )
            else:
                data = await self.client.players()
                at = data.get("observedAt")
                if not isinstance(at, (int, float)) or not 0 <= time.time() - at <= 90:
                    await notify("玩家缓存已过期，暂不能提供当前玩家信息。")
                    return True
                players = data.get("players", [])
                if cmd == "查询":
                    players = [
                        p
                        for p in players
                        if arg
                        and (
                            arg.casefold() in str(p.get("name", "")).casefold()
                            or arg == str(p.get("steamId"))
                        )
                    ]
                rows = [
                    re.sub(r"[@\x00-\x1f\x7f]", "", str(p.get("name", "")))[:40]
                    + " "
                    + str(p.get("steamId", ""))
                    for p in players[:15]
                ]
                await notify("\n".join(rows) if rows else "未找到玩家。")
        except Exception:
            await notify("服务器查询暂不可用。")
        return True

    @filter.llm_tool(name="wardogs_report")
    async def report_tool(self, event: AstrMessageEvent, description: str):
        """提交 WARDOGS 玩家违规举报并收集当前消息和同人相邻图片。仅受理材料，不直接处罚。

        Args:
            description(string): 举报目标与具体违规描述。
        """
        if not self.allowed(event):
            return "当前会话未启用举报。"
        original = self.message(event)
        m = Message(
            original.who,
            original.id + ":report",
            original.at,
            description[:4000],
            original.images,
            original.reply_to,
            original.reply_images,
            original.reply_text,
            original.sender_nickname,
        )

        async def notify(text):
            await event.send(event.plain_result(text))

        cid = await self.reports.ingest(m, True, notify, self.assessor(event))
        return f"举报已受理，案件号：{cid}" if cid else "已处理该消息或请求受限。"

    @filter.llm_tool(name="wardogs_status")
    async def status_tool(self, event: AstrMessageEvent):
        """查询 WARDOGS 当前在线人数、采样状态与队列。"""
        if self.query_identity(event) is None:
            return "当前会话未启用服务器查询。"
        if event.get_platform_name() == "qq_official":
            return await self.public_queries.render("population")
        output = []

        async def collect(text):
            output.append(text)

        await self.command(event, self.message(event), "状态", collect)
        return "\n".join(output)

    @filter.llm_tool(name="wardogs_match")
    async def match_tool(self, event: AstrMessageEvent):
        """只读查询WARDOGS当前对局各队比分及服务器人数，向已启用群的普通成员开放。"""
        if (
            not self.config.get("public_queries_enabled", False)
            or self.query_identity(event) is None
        ):
            return "当前会话未启用对局查询。"
        return await self.public_queries.render("both")

    @filter.llm_tool(name="wardogs_players")
    async def players_tool(self, event: AstrMessageEvent, query: str = ""):
        """查询 WARDOGS 玩家。只查询，不执行处罚。

        Args:
            query(string): 玩家昵称或SteamID，留空查询在线名单。
        """
        if not self.allowed(event):
            return "当前会话未启用玩家查询。"
        output = []

        async def collect(text):
            output.append(text)

        await self.command(
            event, self.message(event), "查询 " + query if query else "玩家", collect
        )
        return "\n".join(output)

    @filter.llm_tool(name="wardogs_announcement")
    async def announcement_tool(
        self, event: AstrMessageEvent, message: str, english: str, japanese: str
    ):
        """使用模型生成的中英日文本创建普通全服公告预览，随后由原管理员确认发送。踢人公告使用固定脚本模板。

        Args:
            message(string): 中文公告，单行最多200字符。
            english(string): 英语公告，单行最多200字符。
            japanese(string): 日语公告，单行最多200字符。
        """
        if not self.allowed(event) or not event.is_admin():
            return "普通公告需要本群管理员权限；举报向所有成员开放。"
        texts = [message, english, japanese]
        try:
            code = self.actions.draft(self.message(event).who, texts)
        except ValueError:
            return "三种语言必须完整，各为1至200个单行可打印字符。"
        instruction = "120秒内由原管理员发送 /wd 确认 " + code
        await event.send(
            event.plain_result(
                "普通公告预览：\n" + "\n".join(texts) + "\n" + instruction
            )
        )
        return "公告仅创建草稿，尚未发送。" + instruction

    @filter.llm_tool(name="wardogs_report_status")
    async def report_status_tool(self, event: AstrMessageEvent, case_id: str):
        """查询当前成员自己的举报处理结果，管理员可查询本群案件。

        Args:
            case_id(string): 举报受理时返回的案件号。
        """
        if not self.allowed(event):
            return "当前会话未启用举报。"
        return await self.reports.progress(
            case_id, self.message(event).who, event.is_admin()
        )

    async def maintain(self):
        while True:
            await asyncio.sleep(10)
            try:
                self.store.purge()
                self.reports.history.prune()
                await self.reports.poll()
                await self.admin_kicks.poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.warning(
                    "WARDOGS status check unavailable; preserving operation state."
                )

    async def terminate(self):
        # Set the gate before awaiting so no new intake can race with shutdown.
        was_closing = self._closing
        self._closing = True
        self.onebot_ready = False
        startup = getattr(self, "startup_task", None)
        if startup is not None and startup is not asyncio.current_task():
            if not was_closing:
                startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
        async with self._lifecycle_lock:
            self.startup_state("terminated")
            if not self.ready:
                return
            self.ready = False
            try:
                intake = [
                    task
                    for task in self.intake_tasks
                    if task is not asyncio.current_task()
                ]
                for task in intake:
                    task.cancel()
                await asyncio.gather(*intake, return_exceptions=True)
                if self.identity_task:
                    self.identity_task.cancel()
                    await asyncio.gather(self.identity_task, return_exceptions=True)
                self.maintenance.cancel()
                await asyncio.gather(self.maintenance, return_exceptions=True)
                if self.join_task:
                    self.join_task.cancel()
                    await asyncio.gather(self.join_task, return_exceptions=True)
                if self.join_approvals:
                    await self.join_approvals.close()
                self.admin_kicks.close()
                await self.reports.close()
                await self.media.close()
                await self.client.close()
                self.store.close()
            finally:
                self.ready = False
