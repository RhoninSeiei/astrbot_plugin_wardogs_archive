"""Opt-in contract test against the companion service source, using fake RCON."""

import copy
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from test_domain import ID, combat_fixture, decision
from wardogs.domain import Identity, Message, Store
from wardogs.flow import Reports
from wardogs.io import ServiceClient, ServiceError

SERVICE_SOURCE = os.environ.get(
    "WARDOGS_SERVICE_SOURCE",
    str(Path(__file__).resolve().parents[1] / "integration" / "companion"),
)


@unittest.skipUnless(
    Path(SERVICE_SOURCE, "service", "app.py").exists(),
    "companion service snapshot not supplied",
)
class ServiceIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_report_to_real_service_coordinator_and_three_jobs(self):
        sys.path.insert(0, SERVICE_SOURCE)
        from service.app import DEFAULT_CONFIG, Engine, api_handler

        class Rcon:
            def __init__(self):
                self.calls = []

            def request(self, method, path, body=None):
                self.calls.append((method, path, body))
                if method == "GET":
                    return {
                        "players": [
                            {"steamId": ID, "name": "Example", "kills": 0, "deaths": 0}
                        ]
                    }
                return {"ok": True, "pending": False}

        class Model:
            async def assess(self, case, images, players, rules):
                return decision(
                    combat_evidence=combat_fixture(next(iter(images))),
                    observed_at=time.time(),
                    evidence_refs=list(images),
                    identity_evidence=list(images),
                )

        class Media:
            async def load(self, url):
                return "data:image/png;base64,example"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rcon = Rcon()
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            cfg["rejoin_guard_enabled"] = True
            engine = Engine(root / "backend.db", rcon, cfg)
            engine.players = [{"steamId": ID, "name": "Example"}]
            engine.last_poll = time.time()
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), api_handler(engine, "fake-query", "fake-execute")
            )
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            (root / "query").write_text("fake-query")
            (root / "execute").write_text("fake-execute")
            client = ServiceClient(
                f"http://127.0.0.1:{server.server_port}",
                root / "query",
                root / "execute",
            )
            store = Store(root / "plugin.db")
            reports = Reports(
                store,
                Model(),
                client,
                Media(),
                rules={"outside_combat": {"version": "1", "text": "No outside combat"}},
                auto_execute=True,
            )

            async def notify(text):
                pass

            try:
                m = Message(
                    Identity("FixtureBot", "g", "u"),
                    "m",
                    time.time(),
                    "举报",
                    ("https://cdn.example/a",),
                )
                cid = await reports.ingest(m, True, notify)
                await reports.finish(cid)
                c = store.get(cid)
                self.assertEqual(c["status"], "submitted")
                self.assertFalse(any(x[0] == "POST" for x in rcon.calls))
                await __import__("asyncio").to_thread(engine.moderation.process_one)
                result = await client.operation(c["operation_id"])
                self.assertEqual(result["kickStatus"], "confirmed")
                self.assertEqual(len(result["announcementJobIds"]), 3)
                history = engine.kick_store.snapshot()
                self.assertEqual(history["confirmedKickCount"], 1)
                self.assertEqual(history["mode"], "permanent")
                self.assertEqual(history["restrictions"][0]["steamId"], ID)
                self.assertEqual(
                    sum(x[0] == "POST" and x[1].endswith("/kick") for x in rcon.calls),
                    1,
                )
                duplicate = await client.kick(
                    store.get(cid), store.get(cid)["decision"]
                )
                self.assertEqual(duplicate["operationId"], c["operation_id"])
                await __import__("asyncio").to_thread(engine.moderation.process_one)
                self.assertEqual(
                    sum(x[0] == "POST" and x[1].endswith("/kick") for x in rcon.calls),
                    1,
                )
                client.kick_key_file = root / "query"
                with self.assertRaises(ServiceError) as ctx:
                    await client.operation(c["operation_id"])
                self.assertEqual(ctx.exception.status, 401)
            finally:
                await reports.close()
                store.close()
                await client.close()
                await __import__("asyncio").to_thread(server.shutdown)
                server.server_close()
                worker.join()
                engine.close()
