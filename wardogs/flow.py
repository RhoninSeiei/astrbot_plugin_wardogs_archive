"""Async report collection and evidence-gated execution orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import deque
from dataclasses import dataclass, field

from .combat import ERRORS as COMBAT_ERRORS
from .combat import bind_combat_evidence
from .domain import KICK_REASONS, Buffer, Identity, Message, validate_decision
from .history import LocalHistory
from .model import FALLBACK_PROVIDER

REVIEW_ERRORS = COMBAT_ERRORS | {
    "invalid_decision",
    "model_insufficient",
    "duplicate_field",
    "invalid_model_output",
    "nonfinite_number",
    "object_required",
    "not_substantiated",
    "rule_not_enabled",
    "identity_unresolved",
    "confidence_insufficient",
    "evidence_unresolved",
    "identity_evidence_unresolved",
    "facts_incomplete",
    "evidence_expired",
}


@dataclass
class Collection:
    who: object
    notify: object
    messages: list = field(default_factory=list)
    urls: dict = field(default_factory=dict)
    deadline: float = 0
    ambiguous: bool = False
    unresolved_quote: bool = False
    assessor: object = None
    reporter_nickname: str = ""
    own_urls: set = field(default_factory=set)
    archived: list = field(default_factory=list)
    retrieved: list = field(default_factory=list)
    retrieved_metadata: list = field(default_factory=list)
    own_archived: set = field(default_factory=set)


class Reports:
    def __init__(
        self,
        store,
        model,
        service,
        media,
        *,
        rules,
        wait=8,
        auto_execute=False,
        identity_threshold=0.99,
        violation_threshold=0.95,
        assessment_timeout=180,
    ):
        if not 5 <= wait <= 10:
            raise ValueError("collection_window_must_be_5_to_10")
        if not 30 <= assessment_timeout <= 300:
            raise ValueError("assessment_timeout_must_be_30_to_300")
        self.assessment_timeout = assessment_timeout
        self.store, self.model, self.service, self.media = store, model, service, media
        self.rules = rules
        self.wait = wait
        self.auto_execute = auto_execute
        self.identity_threshold = identity_threshold
        self.violation_threshold = violation_threshold
        self.buffer = Buffer()
        self.history = LocalHistory(store)
        self.review_locks = [asyncio.Lock() for _ in range(32)]
        self.pending = {}
        self.tasks = {}
        self.rates = {}
        self.closed = False
        self.used = set()
        self.notifiers = {}
        self.assessment_gate = asyncio.Semaphore(4)

    def observe(self, m):
        if self.closed or not self.history.record(m):
            return False
        for prior in self.history.recent(m):
            self.buffer.add(prior)
        quote = self.history.quote(m)
        if quote:
            self.buffer.add(quote)
        self.buffer.add(m)
        return True

    @staticmethod
    def _text(b, text):
        text = text.strip()[:500]
        remaining = 2000 - sum(map(len, b.messages))
        if text and text not in b.messages and remaining > 0 and len(b.messages) < 8:
            b.messages.append(text[:remaining])

    def _attach(self, b, m, quoted=False, label=""):
        m = self.history.filtered(m)
        self._text(b, (label + m.text) if m.text else "")
        if m.reply_text:
            self._text(b, label + "引用：" + m.reply_text if label else m.reply_text)
        self.used.add(self.buffer.key(m))
        self.history.mark(m, "consumed")
        urls = m.images
        quote = (self.history.quote(m) or self.buffer.quoted(m)) if m.reply_to else None
        if quote:
            quote = self.history.filtered(quote)
        if m.reply_images:
            urls += m.reply_images
        elif m.reply_to:
            if quote:
                urls += quote.images
                self._text(b, quote.text)
            else:
                b.unresolved_quote = True
        own_urls = set(m.images) if m.who == b.who else set()
        if quote and quote.who == b.who:
            own_urls.update(url for url in quote.images if url in urls)
        for url in urls:
            key = hashlib.sha256(url.encode()).hexdigest()[:24]
            if len(b.urls) < 4:
                b.urls.setdefault(key, url)
                if url in own_urls:
                    b.own_urls.add(key)

    def _rate(self, who, now, kind="report"):
        for key, q in list(self.rates.items()):
            while q and q[0] < now - 60:
                q.popleft()
            if not q:
                self.rates.pop(key, None)
        keys = [
            (kind, "user", who),
            (kind, "group", who.platform, who.group),
            (kind, "global"),
        ]
        limits = [3, 20, 100]
        if any(len(self.rates.get(k, ())) >= n for k, n in zip(keys, limits)):
            return False
        for k in keys:
            self.rates.setdefault(k, deque()).append(now)
        return True

    async def ingest(
        self, m, is_report, notify, assessor=None, *, observed=False, retrieved=None
    ):
        if retrieved is not None and (
            not isinstance(retrieved, list)
            or len(retrieved) > 4
            or any(
                not isinstance(x, Message)
                or x.who.platform != m.who.platform
                or x.who.group != m.who.group
                for x in retrieved
            )
        ):
            raise ValueError("foreign_retrieval_scope")
        if self.closed or (not observed and not self.observe(m)):
            return None
        same = [
            (cid, b)
            for cid, b in self.pending.items()
            if b.who == m.who and time.monotonic() <= b.deadline
        ]
        if not is_report:
            if len(same) == 1:
                self._attach(same[0][1], m)
            elif len(same) > 1 and (m.images or m.reply_images):
                # A reply to the report's source selects one collection explicitly.
                selected = [
                    (cid, b)
                    for cid, b in same
                    if self.store.get(cid)["source"] == m.reply_to
                ]
                if len(selected) == 1:
                    selected[0][1].ambiguous = False
                    self._attach(selected[0][1], m)
                else:
                    for _, b in same:
                        b.ambiguous = True
                    await notify(
                        "存在多个举报，请引用对应举报消息发送图片，避免关联错误。"
                    )
            return None
        if len(self.pending) >= 100 or not self._rate(m.who, m.at):
            await notify("举报较频繁，请一分钟后重试。")
            return None
        cid = self.store.create(m, m.text[:4000])
        if self.store.get(cid)["status"] != "collecting":
            return cid
        b = Collection(
            m.who,
            notify,
            deadline=time.monotonic() + self.wait,
            assessor=assessor or self.model,
            reporter_nickname=m.sender_nickname,
        )
        # Current evidence and explicit citations take precedence over history.
        self._attach(b, m)
        if retrieved is not None:
            b.retrieved = list(retrieved)
            for source in retrieved:
                label = (
                    "[检索来源:"
                    + (
                        "reporter"
                        if source.who == b.who
                        else "other_" + self.history.scope(source.who)[:12]
                    )
                    + "] "
                )
                self._attach(b, source, label=label)
        if not m.reply_images and not m.reply_to:
            for prior in reversed(list(self.buffer.messages.values())):
                if (
                    prior.who == m.who
                    and prior.id != m.id
                    and -30 <= prior.at - m.at <= max(self.wait, time.time() - m.at)
                    and (retrieved is None or prior.at > m.at)
                    and self.buffer.key(prior) not in self.used
                    and self.history.state(prior) not in ("consumed", "command")
                ):
                    self._attach(b, prior)
        self.pending[cid] = b
        self.used.add(self.buffer.key(m))
        # Mark pre-context as consumed so another target does not inherit it.
        for prior in self.buffer.previous(m):
            if prior.images:
                self.used.add(self.buffer.key(prior))
        if len(self.used) > 4000:
            self.used = {
                self.buffer.key(x)
                for x in self.buffer.messages.values()
                if self.buffer.key(x) in self.used
            }
        self.tasks[cid] = asyncio.create_task(
            self._wait(cid), name="wardogs-collect-" + cid
        )
        await notify(
            f"举报 {cid} 已受理，正在收集图片，请在{self.wait}秒内继续发送；补图时可同时 @机器人。"
        )
        return cid

    async def supplement(self, cid, m, notify, assessor=None):
        c = self.store.visible(cid, m.who)
        if not c or c["status"] not in (
            "needs_evidence",
            "not_substantiated",
            "technical_failure",
            "eligible",
        ):
            return False
        if self.closed or len(self.pending) >= 100 or not self._rate(m.who, m.at):
            return False
        self.buffer.add(m)
        self.store.update(
            cid,
            status="collecting",
            target="",
            decision=None,
            evidence_version=c.get("evidence_version", 1) + 1,
            description=(c["description"] + "\n补充：" + m.text[:2000])[-4000:],
        )
        b = Collection(
            m.who,
            notify,
            deadline=time.monotonic() + self.wait,
            assessor=assessor or self.model,
            reporter_nickname=m.sender_nickname,
        )
        self._attach(b, m)
        for text in c.get("last_context", []):
            self._text(b, text)
        b.archived = c.get("last_evidence_ids", c.get("evidence_ids"))
        if b.archived is None:
            b.archived = [
                p.stem
                for p in sorted((self.store.evidence_root / cid).glob("*.json"))[-4:]
            ]
        b.retrieved_metadata = c.get("retrieved_sources", [])
        b.own_archived = set(
            c.get("reporter_context", {}).get("own_evidence_refs", [])
        ) & set(b.archived)
        self.pending[cid] = b
        self.tasks[cid] = asyncio.create_task(
            self._wait(cid), name="wardogs-supplement-" + cid
        )
        await notify(f"举报 {cid} 正在收集补充图片。")
        return True

    async def _wait(self, cid):
        try:
            await asyncio.sleep(max(0, self.pending[cid].deadline - time.monotonic()))
            await self.finish(cid)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never log arbitrary provider/HTTP errors that could contain secrets.
            if self.store.get(cid)["status"] == "executing":
                self.store.update(cid, status="outcome_unknown")
            elif self.store.get(cid)["status"] in ("collecting", "assessing"):
                self.store.update(cid, status="technical_failure")
        finally:
            self.tasks.pop(cid, None)

    def _validate_review(self, d, case, images, players):
        if not isinstance(d, dict):
            raise ValueError("invalid_decision")
        if d.get("verdict") not in {
            "substantiated",
            "insufficient",
            "not_substantiated",
        }:
            raise ValueError("invalid_model_output")
        if d.get("verdict") == "not_substantiated":
            return d
        if d.get("verdict") == "insufficient":
            raise ValueError("model_insufficient")
        reasons = KICK_REASONS.get(d.get("rule_id"))
        if reasons:
            d = dict(d, **dict(zip(("reason", "english", "japanese"), reasons)))
        d = bind_combat_evidence(d, set(images), players, case["reporter_context"])
        return validate_decision(
            d,
            set(images),
            {str(p.get("steamId", "")) for p in players},
            {k: v["version"] for k, v in self.rules.items()},
            time.time(),
            self.identity_threshold,
            self.violation_threshold,
        )

    async def _review(self, cid, assessor, case, images, players):
        primary_provider = getattr(assessor, "provider_id", "")
        reviewers = [(primary_provider, assessor.assess)]
        fallback = getattr(assessor, "fallback_assess", None)
        fallback_provider = getattr(assessor, "fallback_provider_id", FALLBACK_PROVIDER)
        if (
            callable(fallback)
            and fallback_provider
            and primary_provider != fallback_provider
        ):
            reviewers.append((fallback_provider, fallback))
        attempts = []
        for index, (provider, review) in enumerate(reviewers):
            started = time.monotonic()
            entry = {
                "provider": provider,
                "stage": "primary" if index == 0 else "fallback",
                "status": "assessing",
            }
            attempts.append(entry)
            self.store.update(
                cid, assessment_attempts=attempts, assessment_provider=provider
            )
            try:
                d = await asyncio.wait_for(
                    review(case, images, players, self.rules),
                    timeout=self.assessment_timeout,
                )
                if isinstance(d, dict) and d.get("verdict") in (
                    "substantiated",
                    "insufficient",
                    "not_substantiated",
                ):
                    entry["verdict"] = d["verdict"]
                d = self._validate_review(d, case, images, players)
            except Exception as error:
                entry.update(
                    status="failed",
                    error_type=type(error).__name__,
                    duration_seconds=round(time.monotonic() - started, 3),
                )
                if isinstance(error, ValueError) and str(error) in REVIEW_ERRORS:
                    entry["error_code"] = str(error)
                self.store.update(cid, assessment_attempts=attempts)
                if index == len(reviewers) - 1:
                    raise
            else:
                entry.update(
                    status="accepted",
                    duration_seconds=round(time.monotonic() - started, 3),
                )
                self.store.update(cid, assessment_attempts=attempts)
                return d

    async def finish(self, cid):
        b = self.pending.pop(cid, None)
        if not b:
            return
        task = self.tasks.get(cid)
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.tasks.pop(cid, None)

        async def tell(text):
            try:
                await b.notify(f"举报 {cid}：{text}")
            except Exception:
                pass

        if b.ambiguous or (not b.urls and not b.archived) or b.unresolved_quote:
            self.store.update(cid, status="needs_evidence")
            await tell("图片缺失、引用不可读或关联不明确，请引用举报消息重新补充图片。")
            return
        self.store.update(cid, status="assessing")
        try:
            images = {}
            url_digests = {}
            own_evidence = set()
            for ref, url in b.urls.items():
                content = await self.media.load(url)
                digest = self.store.archive(cid, ref, content)
                images[digest] = content
                url_digests[url] = digest
                if ref in b.own_urls:
                    own_evidence.add(digest)
            archived = await asyncio.to_thread(
                self.store.read_evidence, cid, b.archived
            )
            for digest, content in archived.items():
                if len(images) < 4:
                    images.setdefault(digest, content)
                    if digest in b.own_archived:
                        own_evidence.add(digest)
            if not images:
                self.store.update(cid, status="needs_evidence")
                await tell("本地图片已过期或不可读，请重新补充原图。")
                return
            self.store.update(
                cid, last_evidence_ids=list(images), last_context=b.messages
            )
            data = await self.service.players()
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("observedAt"), (int, float))
                or not 0 <= time.time() - data["observedAt"] <= 90
            ):
                raise ValueError("players_expired")
            players = data.get("players")
            if not isinstance(players, list) or not all(
                isinstance(p, dict) for p in players
            ):
                raise ValueError("invalid_players")
            # Display name is an untrusted hint; no QQ member/group IDs leave this process.
            case = {
                "description": self.store.get(cid)["description"][-1000:],
                "context": b.messages,
                "evidenceVersion": str(self.store.get(cid).get("evidence_version", 1)),
                "reporter_context": {
                    "display_name": b.reporter_nickname,
                    "source": "qq_event_display_name",
                    "own_evidence_refs": sorted(own_evidence),
                },
            }
            sources = []
            for source in b.retrieved:
                quote = self.history.quote(source)
                quote_urls = source.reply_images or (quote.images if quote else ())
                sources.append(
                    {
                        "author": "reporter"
                        if source.who == b.who
                        else "other_" + self.history.scope(source.who)[:12],
                        "from_reporter": source.who == b.who,
                        "evidence_refs": sorted(
                            {
                                url_digests[url]
                                for url in source.images
                                if url in url_digests
                            }
                        ),
                        "quoted_evidence_refs": sorted(
                            {
                                url_digests[url]
                                for url in quote_urls
                                if url in url_digests
                            }
                        ),
                        "quote_from_reporter": quote.who == b.who if quote else None,
                    }
                )
            for old_source in b.retrieved_metadata:
                entry = dict(old_source)
                for refs in ["evidence_refs", "quoted_evidence_refs"]:
                    entry[refs] = [ref for ref in entry.get(refs, []) if ref in images]
                if entry["evidence_refs"] or entry["quoted_evidence_refs"]:
                    sources.append(entry)
            if sources:
                case["retrieved_sources"] = sources[:4]
                self.store.update(cid, retrieved_sources=sources[:4])
            if b.retrieved:
                self.store.update(
                    cid, retrieved_message_ids=[source.id for source in b.retrieved]
                )
            self.store.update(cid, reporter_context=case["reporter_context"])
            case_key = dict(case)
            case_key.pop("evidenceVersion", None)
            fingerprint = self.history.fingerprint(
                {
                    "policy": self.history.POLICY,
                    "case": case_key,
                    "images": sorted(images),
                    "rules": self.rules,
                    "players": sorted(
                        (str(p.get("steamId", "")), str(p.get("name", "")))
                        for p in players
                    ),
                    "provider": getattr(b.assessor, "provider_id", ""),
                    "thresholds": [self.identity_threshold, self.violation_threshold],
                }
            )
            async with (
                self.assessment_gate,
                self.review_locks[int(fingerprint[:4], 16) % 32],
            ):
                previous = self.history.cached(b.who, "review", fingerprint, ttl=1800)
                if previous and previous != cid:
                    self.store.update(
                        cid, status="duplicate_evidence", related_case_id=previous
                    )
                    await tell(
                        "相同证据及上下文已处理，不重复送审。原案件："
                        + previous
                        + "；新增图片或事件信息后可补充。"
                    )
                    return
                try:
                    d = await self._review(cid, b.assessor, case, images, players)
                except ValueError as error:
                    if str(error) in REVIEW_ERRORS - {
                        "invalid_decision",
                        "duplicate_field",
                        "invalid_model_output",
                        "nonfinite_number",
                        "object_required",
                    }:
                        self.history.remember(b.who, "review", fingerprint, cid)
                    raise
                else:
                    self.history.remember(b.who, "review", fingerprint, cid)
                finally:
                    for digest in images:
                        self.history.remember(b.who, "image", digest, {"case_id": cid})
            if d.get("verdict") == "not_substantiated":
                self.store.update(cid, status="not_substantiated")
                await tell("现有证据未能证实举报。")
                return
            self.store.update(
                cid,
                status="eligible",
                target=d["target_steam_id"],
                decision=d,
                evidence_ids=list(images),
                decided_at=time.time(),
            )
        except ValueError as error:
            code = (
                str(error)
                if str(error) in COMBAT_ERRORS
                else "evidence_validation_failed"
            )
            self.store.update(cid, status="needs_evidence", evidence_error=code)
            if code == "victim_link_unproven":
                await tell(
                    "尚不能确认位置属于同一被击杀玩家，请补充第一视角死亡画面、可用昵称及同一事件关联证据。"
                )
            elif code in ("full_death_view_unproven", "damage_log_conflict"):
                await tell(
                    "完整死亡画面、小地图清晰度或伤害来源尚未核实，请补充包含完整小地图、击杀条和伤害日志的截图。"
                )
            elif code in ("killer_link_unproven", "combat_event_unproven"):
                await tell(
                    "尚不能确认圈外标记属于击杀者或两图属于同一交战事件，请补充带玩家名、位置及事件关联的完整截图。"
                )
            elif code in ("killer_identity_ambiguous", "killer_target_conflict"):
                await tell(
                    "击杀者昵称或在线ID无法唯一核实，请补充清晰击杀记录及玩家ID关联证据。"
                )
            else:
                await tell(
                    "证据、身份或服规核验未通过，请补充能对应玩家ID、行为和时间的图片。"
                )
            return
        except Exception:
            self.store.update(cid, status="technical_failure")
            await tell("图片读取、玩家查询或模型审核暂不可用，未执行处罚。")
            return
        if not self.auto_execute:
            await tell("证据审核通过；当前自动处置未启用，尚未踢出。")
            return
        if not self.store.claim(cid):
            self.store.update(cid, status="duplicate_blocked")
            await tell("相同目标存在进行中或待核实处置，本次未重复执行。")
            return
        try:
            result = await self.service.kick(self.store.get(cid), d)
            oid = result.get("operationId")
            if not isinstance(oid, str) or not oid:
                raise ValueError("invalid_operation")
            self.store.update(cid, status="submitted", operation_id=oid, result=result)
            self.notifiers[cid] = (b.notify, None)
            await tell(
                "处置任务已提交；可用 /wd 举报进度 " + cid + " 查询，尚未确认踢出。"
            )
        except Exception:
            self.store.update(cid, status="outcome_unknown")
            await tell("处置提交结果待核实，不会自动重试踢出。")

    async def progress(self, cid, who, admin=False):
        c = self.store.visible(cid, who, admin)
        if not c:
            return "案件不存在或没有查看权限。"
        if c.get("operation_id"):
            try:
                result = await self.service.operation(c["operation_id"])
                self.store.update(cid, result=result)
                ks = result.get("kickStatus")
                state_map = {
                    "offline": "target_offline",
                    "failed": "failed",
                    "cancelled": "cancelled",
                    "unknown": "outcome_unknown",
                }
                if ks in state_map:
                    self.store.update(cid, status=state_map[ks])
                if ks == "confirmed":
                    self.store.update(cid, status="kicked")
                    return "踢出已确认；三语公告状态：" + self.announcement_label(
                        result.get("announcementStatus")
                    )
                labels = {
                    "queued": "等待执行",
                    "pending": "等待执行",
                    "resolving": "复核玩家",
                    "dispatching": "执行中",
                    "offline": "目标离线，未执行",
                    "failed": "执行失败",
                    "cancelled": "已取消",
                    "unknown": "结果待核实",
                }
                return (
                    "处置状态："
                    + labels.get(ks, "待核实")
                    + "；公告状态："
                    + self.announcement_label(result.get("announcementStatus"))
                )
            except Exception:
                return "任务查询暂不可用，保留原处置记录，不会重新踢出。"
        if c["status"] == "duplicate_evidence":
            return "相同证据已处理，原案件：" + c.get("related_case_id", "")
        return "案件状态：" + c["status"]

    async def poll(self):
        for cid in self.store.active():
            c = self.store.get(cid)
            if not c.get("operation_id"):
                continue
            text = await self.progress(
                cid, Identity(c["platform"], c["grp"], c["sender"])
            )
            c = self.store.get(cid)
            signature = (c["status"], c.get("result", {}).get("announcementStatus"))
            watcher = self.notifiers.get(cid)
            if (
                watcher
                and signature != watcher[1]
                and c["status"]
                in (
                    "kicked",
                    "target_offline",
                    "failed",
                    "cancelled",
                    "outcome_unknown",
                )
            ):
                try:
                    await watcher[0](f"举报 {cid}：" + text)
                except Exception:
                    pass
                self.notifiers[cid] = (watcher[0], signature)
            if c["status"] in (
                "target_offline",
                "failed",
                "cancelled",
                "outcome_unknown",
            ) or signature[1] in ("sent", "partial", "failed", "unknown"):
                self.notifiers.pop(cid, None)

    @staticmethod
    def announcement_label(status):
        return {
            "not_queued": "未入队",
            "queued": "已入队",
            "pending": "投递中",
            "partial": "部分成功",
            "sent": "已获RCON接受",
            "failed": "失败",
            "unknown": "结果不明",
        }.get(status, "待核实")

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for cid in list(self.pending):
            if self.store.get(cid)["status"] == "collecting":
                self.store.update(cid, status="needs_evidence")
        self.tasks.clear()
        self.pending.clear()
        self.buffer.messages.clear()
        self.notifiers.clear()
