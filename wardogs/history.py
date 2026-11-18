"""Bounded private SQLite receipts; raw chat never becomes model history."""

import hashlib
import json
import time
from dataclasses import asdict, replace

from .domain import Identity, Message


class LocalHistory:
    POLICY = "passive-v1"
    SEARCHABLE = "(state NOT IN ('consumed','command') AND (state!='irrelevant_text' OR json_array_length(payload,'$.images')>0 OR json_array_length(payload,'$.reply_images')>0))"

    def __init__(self, store):
        self.db = store.db
        self.next_prune = 0
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS wd_messages(
            platform TEXT, grp TEXT, source TEXT, sender TEXT, at REAL,
            payload TEXT, state TEXT NOT NULL,
            PRIMARY KEY(platform,grp,source));
          CREATE INDEX IF NOT EXISTS wd_messages_recent ON wd_messages(platform,grp,sender,at);
          CREATE INDEX IF NOT EXISTS wd_messages_group_time ON wd_messages(platform,grp,at);
          CREATE TABLE IF NOT EXISTS wd_inputs(
            scope TEXT, kind TEXT, fingerprint TEXT, updated REAL, value TEXT,
            PRIMARY KEY(scope,kind,fingerprint));
        """)

    @staticmethod
    def fingerprint(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    @staticmethod
    def scope(who):
        return LocalHistory.fingerprint([who.platform, who.group, who.sender])

    @staticmethod
    def key(m):
        return m.who.platform, m.who.group, m.id

    def record(self, m):
        self.prune()
        data = asdict(m)
        data.update(
            text=m.text[:4000],
            reply_text=m.reply_text[:4000],
            images=list(m.images[:4]),
            reply_images=list(m.reply_images[:4]),
        )
        with self.db:
            return bool(
                self.db.execute(
                    "INSERT OR IGNORE INTO wd_messages VALUES(?,?,?,?,?,?,?)",
                    (
                        *self.key(m),
                        m.who.sender,
                        m.at,
                        json.dumps(data, ensure_ascii=False),
                        "observed",
                    ),
                ).rowcount
            )

    def mark(self, m, state):
        with self.db:
            self.db.execute(
                "UPDATE wd_messages SET state=? WHERE platform=? AND grp=? AND source=?",
                (state, *self.key(m)),
            )

    def state(self, m):
        row = self.db.execute(
            "SELECT state FROM wd_messages WHERE platform=? AND grp=? AND source=?",
            self.key(m),
        ).fetchone()
        return row[0] if row else ""

    @staticmethod
    def decode(row):
        d = json.loads(row["payload"])
        d["who"] = Identity(**d["who"])
        d["images"] = tuple(d["images"])
        d["reply_images"] = tuple(d["reply_images"])
        m = Message(**d)
        # A non-report text verdict says nothing about whether its image can
        # help a later report. Keep images until an actual evidence review.
        return (
            replace(m, text="", reply_text="")
            if row["state"] == "irrelevant_text"
            else m
        )

    def filtered(self, m):
        return (
            replace(m, text="", reply_text="")
            if self.state(m) == "irrelevant_text"
            else m
        )

    def recent(self, m):
        rows = self.db.execute(
            "SELECT * FROM wd_messages WHERE platform=? AND grp=? AND sender=? AND source!=? AND at>=? AND at<=? AND state NOT IN ('consumed','command') ORDER BY at DESC,rowid DESC LIMIT 20",
            (m.who.platform, m.who.group, m.who.sender, m.id, m.at - 30, m.at + 8),
        ).fetchall()
        return [self.decode(row) for row in reversed(rows)]

    def quote(self, m):
        if not m.reply_to:
            return None
        row = self.db.execute(
            "SELECT * FROM wd_messages WHERE platform=? AND grp=? AND source=? AND at>=? AND at<=? AND state!='command'",
            (m.who.platform, m.who.group, m.reply_to, m.at - 86400, m.at),
        ).fetchone()
        return self.decode(row) if row else None

    def lookup(self, trigger, source, *, ceiling):
        row = self.db.execute(
            "SELECT * FROM wd_messages WHERE platform=? AND grp=? AND source=? AND at>=? AND at<=? AND state!='command'",
            (
                trigger.who.platform,
                trigger.who.group,
                source,
                max(trigger.at - 86400, time.time() - 86400),
                min(ceiling, time.time()),
            ),
        ).fetchone()
        return self.decode(row) if row else None

    def revision(self, trigger):
        row = self.db.execute(
            "SELECT max(rowid) FROM wd_messages WHERE platform=? AND grp=? AND source!=? AND at>=? AND at<=? AND "
            + self.SEARCHABLE,
            (
                trigger.who.platform,
                trigger.who.group,
                trigger.id,
                max(trigger.at - 86400, time.time() - 86400),
                min(trigger.at + 8, time.time()),
            ),
        ).fetchone()
        return row[0] or 0

    def context(self, m):
        recent = self.recent(m)
        previous = [x.text[:200] for x in recent if x.text.strip()][-6:]
        quoted = self.quote(m)
        return {
            "previous_messages": previous,
            "quoted_text": (m.reply_text or (quoted.text if quoted else ""))[:400],
            "image_count": len(m.images) + len(m.reply_images),
            "recent_image_count": min(4, sum(len(x.images) for x in recent)),
        }

    def cached(self, who, kind, fingerprint, ttl=600):
        row = self.db.execute(
            "SELECT value FROM wd_inputs WHERE scope=? AND kind=? AND fingerprint=? AND updated>=?",
            (self.scope(who), kind, fingerprint, time.time() - ttl),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def remember(self, who, kind, fingerprint, value):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO wd_inputs VALUES(?,?,?,?,?)",
                (
                    self.scope(who),
                    kind,
                    fingerprint,
                    time.time(),
                    json.dumps(value, ensure_ascii=False),
                ),
            )

    def prune(self):
        now = time.time()
        if now < self.next_prune:
            return
        self.next_prune = now + 60
        with self.db:
            self.db.execute("DELETE FROM wd_messages WHERE at<?", (now - 86400,))
            self.db.execute("DELETE FROM wd_inputs WHERE updated<?", (now - 7 * 86400,))
            for table, order, limit in [
                ("wd_messages", "at", 10000),
                ("wd_inputs", "updated", 20000),
            ]:
                self.db.execute(
                    f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} ORDER BY {order} DESC,rowid DESC LIMIT -1 OFFSET ?)",
                    (limit,),
                )
