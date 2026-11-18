"""Durable, one-use manual broadcast drafts."""

import json
import secrets
import time

from .domain import clean_text


class Announcements:
    def __init__(self, store, client):
        self.db = store.db
        self.client = client
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS drafts(code TEXT PRIMARY KEY, platform TEXT, grp TEXT, sender TEXT, texts TEXT, expires REAL, status TEXT, result TEXT)"
            )
            self.db.execute("UPDATE drafts SET status='unknown' WHERE status='sending'")

    def draft(self, who, texts, now=None):
        if len(texts) != 3:
            raise ValueError("three_languages_required")
        for text in texts:
            clean_text(text)
        code = secrets.token_urlsafe(12)
        with self.db:
            self.db.execute(
                "DELETE FROM drafts WHERE expires<? AND status IN ('draft','cancelled')",
                (time.time() - 86400,),
            )
            self.db.execute(
                "INSERT INTO drafts VALUES(?,?,?,?,?,?,?,?)",
                (
                    code,
                    who.platform,
                    who.group,
                    who.sender,
                    json.dumps(texts, ensure_ascii=False),
                    (now if now is not None else time.time()) + 120,
                    "draft",
                    "{}",
                ),
            )
        return code

    def row(self, who, code):
        row = self.db.execute(
            "SELECT * FROM drafts WHERE code=? AND platform=? AND grp=? AND sender=?",
            (code, who.platform, who.group, who.sender),
        ).fetchone()
        return dict(row) if row else None

    def cancel(self, who, code):
        with self.db:
            c = self.db.execute(
                "UPDATE drafts SET status='cancelled' WHERE code=? AND platform=? AND grp=? AND sender=? AND status='draft'",
                (code, who.platform, who.group, who.sender),
            )
        return c.rowcount == 1

    async def confirm(self, who, code):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.row(who, code)
            if not row or row["status"] != "draft" or time.time() > row["expires"]:
                raise ValueError("draft_not_available")
            self.db.execute("UPDATE drafts SET status='sending' WHERE code=?", (code,))
        finally:
            self.db.commit()
        try:
            result = await self.client.broadcast(
                json.loads(row["texts"]), "wd-draft-" + code
            )
            if not isinstance(result.get("jobIds"), list) or len(result["jobIds"]) != 3:
                raise ValueError("invalid_jobs")
            with self.db:
                self.db.execute(
                    "UPDATE drafts SET status='accepted',result=? WHERE code=?",
                    (json.dumps(result), code),
                )
            return result
        except BaseException:
            with self.db:
                self.db.execute(
                    "UPDATE drafts SET status='unknown' WHERE code=?", (code,)
                )
            raise

    def result(self, who, code):
        row = self.row(who, code)
        if not row:
            return None
        return row["status"], json.loads(row["result"])
