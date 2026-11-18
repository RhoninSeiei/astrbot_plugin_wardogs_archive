"""Message correlation, durable cases and fail-closed decision validation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

# Fixed arguments for the pre-existing kick script and its announcement templates.
KICK_REASONS = {
    "outside_combat": (
        "黄圈外违规交战",
        "Combat outside the marked zone",
        "指定区域外での戦闘",
    ),
    "base_camping": (
        "违反基地保护规则",
        "Violation of base protection rules",
        "拠点保護ルール違反",
    ),
    "cheating": (
        "违反反作弊服规",
        "Violation of anti-cheat server rules",
        "不正行為に関するサーバールール違反",
    ),
}


@dataclass(frozen=True)
class Identity:
    platform: str
    group: str
    sender: str


@dataclass(frozen=True)
class Message:
    who: Identity
    id: str
    at: float
    text: str = ""
    images: tuple[str, ...] = ()
    reply_to: str = ""
    reply_images: tuple[str, ...] = ()
    reply_text: str = ""
    sender_nickname: str = ""


class Buffer:
    def __init__(self, ttl=120, per_sender=20, per_group=200, total=2000):
        self.ttl, self.per_sender, self.per_group, self.total = (
            ttl,
            per_sender,
            per_group,
            total,
        )
        self.messages = OrderedDict()

    @staticmethod
    def key(m):
        return m.who.platform, m.who.group, m.id

    def expire(self, now):
        for key, m in list(self.messages.items()):
            if now - m.at > self.ttl:
                self.messages.pop(key)

    def add(self, m):
        self.expire(m.at)
        key = self.key(m)
        if key in self.messages:
            return False
        self.messages[key] = m
        same = [k for k, v in self.messages.items() if v.who == m.who]
        group = [k for k, v in self.messages.items() if k[:2] == key[:2]]
        for keys, limit in [(same, self.per_sender), (group, self.per_group)]:
            for old in keys[:-limit]:
                self.messages.pop(old, None)
        while len(self.messages) > self.total:
            self.messages.popitem(last=False)
        return True

    def previous(self, m):
        self.expire(m.at)
        return [
            x
            for x in self.messages.values()
            if x.who == m.who and x.id != m.id and 0 <= m.at - x.at <= 30
        ]

    def quoted(self, m):
        self.expire(m.at)
        return self.messages.get((m.who.platform, m.who.group, m.reply_to))


def clean_text(value, maximum=200):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or not all(c.isprintable() for c in value)
    ):
        raise ValueError("invalid_text")
    return value


def validate_decision(
    d, evidence, targets, rules, now, identity_threshold=0.99, violation_threshold=0.95
):
    if not isinstance(d, dict) or d.get("verdict") != "substantiated":
        raise ValueError("not_substantiated")
    if d.get("rule_id") not in rules or d.get("rule_version") != rules[d["rule_id"]]:
        raise ValueError("rule_not_enabled")
    target = d.get("target_steam_id")
    if (
        not isinstance(target, str)
        or not re.fullmatch(r"[0-9]{17}", target)
        or target not in targets
    ):
        raise ValueError("identity_unresolved")
    for key, minimum in [
        ("identity_confidence", identity_threshold),
        ("violation_confidence", violation_threshold),
    ]:
        n = d.get(key)
        if (
            isinstance(n, bool)
            or not isinstance(n, (float, int))
            or not math.isfinite(n)
            or not minimum <= n <= 1
        ):
            raise ValueError("confidence_insufficient")
    for field in ["evidence_refs", "identity_evidence"]:
        refs = d.get(field)
        if (
            not isinstance(refs, list)
            or not refs
            or not all(isinstance(x, str) and x in evidence for x in refs)
        ):
            raise ValueError("evidence_unresolved")
    if not set(d["identity_evidence"]).issubset(d["evidence_refs"]):
        raise ValueError("identity_evidence_unresolved")
    if d.get("missing_facts") != [] or d.get("contradictions") != []:
        raise ValueError("facts_incomplete")
    facts = d.get("observed_facts")
    if (
        not isinstance(facts, list)
        or not facts
        or not all(isinstance(x, str) and x.strip() for x in facts)
    ):
        raise ValueError("facts_incomplete")
    at = d.get("observed_at")
    if (
        isinstance(at, bool)
        or not isinstance(at, (int, float))
        or not math.isfinite(at)
        or not (at > 0 and 0 <= now - at <= 1800)
    ):
        raise ValueError("evidence_expired")
    for key in ["reason", "english", "japanese"]:
        clean_text(d.get(key), 120)
    return d


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.evidence_root = Path(path).parent / "evidence"
        self.evidence_root.mkdir(exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY, platform TEXT, grp TEXT, sender TEXT,
          source TEXT, status TEXT, target TEXT DEFAULT '', payload TEXT, created REAL, updated REAL,
          UNIQUE(platform,grp,source));
        """)
        with self.db:
            self.db.execute(
                "UPDATE cases SET status='outcome_unknown' WHERE status='executing'"
            )
            self.db.execute(
                "UPDATE cases SET status='needs_evidence' WHERE status IN ('collecting','assessing','eligible')"
            )

    def create(self, m, description):
        now = time.time()
        cid = uuid.uuid4().hex[:16]
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO cases VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    m.who.platform,
                    m.who.group,
                    m.who.sender,
                    m.id,
                    "collecting",
                    "",
                    json.dumps(
                        {"description": description, "origin": {"message_id": m.id}},
                        ensure_ascii=False,
                    ),
                    now,
                    now,
                ),
            )
        return self.db.execute(
            "SELECT id FROM cases WHERE platform=? AND grp=? AND source=?",
            (m.who.platform, m.who.group, m.id),
        ).fetchone()[0]

    def get(self, cid):
        row = self.db.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d.update(json.loads(d.pop("payload")))
        return d

    def update(self, cid, **changes):
        case = self.get(cid)
        status = changes.pop("status", case["status"])
        target = changes.pop("target", case["target"])
        payload = {
            k: v
            for k, v in case.items()
            if k
            not in {
                "id",
                "platform",
                "grp",
                "sender",
                "source",
                "status",
                "target",
                "created",
                "updated",
            }
        }
        # source metadata has its own payload key on initial creation; routing columns are immutable.
        payload.update(changes)
        with self.db:
            self.db.execute(
                "UPDATE cases SET status=?,target=?,payload=?,updated=? WHERE id=?",
                (
                    status,
                    target,
                    json.dumps(payload, ensure_ascii=False),
                    time.time(),
                    cid,
                ),
            )

    def visible(self, cid, who, admin=False):
        c = self.get(cid)
        if (
            c
            and c["platform"] == who.platform
            and c["grp"] == who.group
            and (admin or c["sender"] == who.sender)
        ):
            return c
        return None

    def claim(self, cid):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            c = self.get(cid)
            if c["status"] != "eligible":
                return False
            other = self.db.execute(
                "SELECT 1 FROM cases WHERE id!=? AND target=? AND (status IN ('executing','outcome_unknown','submitted') OR (status='kicked' AND updated>?))",
                (cid, c["target"], time.time() - 600),
            ).fetchone()
            if other:
                return False
            self.db.execute(
                "UPDATE cases SET status='executing',updated=? WHERE id=?",
                (time.time(), cid),
            )
            return True
        finally:
            self.db.commit()

    def active(self):
        ids = [
            r[0]
            for r in self.db.execute(
                "SELECT id FROM cases WHERE status IN ('submitted','executing','outcome_unknown','kicked')"
            )
        ]
        return [
            cid
            for cid in ids
            if self.get(cid)["status"] != "kicked"
            or self.get(cid).get("result", {}).get("announcementStatus")
            in ("queued", "pending")
        ]

    def archive(self, cid, source_ref, content):
        digest = hashlib.sha256(content.encode()).hexdigest()
        root = self.evidence_root / cid
        root.mkdir(exist_ok=True, mode=0o700)
        dest = root / (digest + ".json")
        payload = json.dumps(
            {"sha256": digest, "source_ref": source_ref, "data_uri": content},
            ensure_ascii=False,
        )
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        return digest

    def read_evidence(self, cid, ids=None):
        if not re.fullmatch(r"[0-9a-f]{16}", cid):
            raise ValueError("invalid_case_id")
        root = self.evidence_root / cid
        refs = (
            ids
            if ids is not None
            else [p.stem for p in sorted(root.glob("*.json"))[-4:]]
        )
        result = {}
        for digest in refs[:4]:
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                continue
            path = root / (digest + ".json")
            try:
                if path.stat().st_size > 12 * 1024 * 1024:
                    continue
                payload = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            content = payload.get("data_uri")
            if (
                isinstance(content, str)
                and hashlib.sha256(content.encode()).hexdigest() == digest
            ):
                result[digest] = content
        return result

    def purge(self, now=None):
        cutoff = (now or time.time()) - 7 * 86400
        for directory in self.evidence_root.iterdir():
            if directory.is_dir() and directory.stat().st_mtime < cutoff:
                shutil.rmtree(directory)
        with self.db:
            self.db.execute(
                "DELETE FROM cases WHERE updated<? AND status NOT IN ('executing','outcome_unknown','submitted')",
                ((now or time.time()) - 30 * 86400,),
            )

    def close(self):
        self.db.close()
