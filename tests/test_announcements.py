import tempfile
import unittest
from pathlib import Path

from wardogs.actions import Announcements
from wardogs.domain import Identity, Store


class API:
    def __init__(self):
        self.calls = []
        self.error = False

    async def broadcast(self, texts, key):
        self.calls.append((texts, key))
        if self.error:
            raise TimeoutError()
        return {"jobIds": ["zh", "en", "ja"]}


class ActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db")
        self.api = API()
        self.actions = Announcements(self.store, self.api)
        self.who = Identity("FixtureBot", "g", "u")

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_three_language_confirmation_bound_to_owner(self):
        code = self.actions.draft(self.who, ["中文", "English", "日本語"])
        with self.assertRaises(ValueError):
            await self.actions.confirm(Identity("FixtureBot", "g", "v"), code)
        result = await self.actions.confirm(self.who, code)
        self.assertEqual(result["jobIds"], ["zh", "en", "ja"])
        with self.assertRaises(ValueError):
            await self.actions.confirm(self.who, code)
        self.assertEqual(len(self.api.calls), 1)

    async def test_timeout_remains_unknown_and_cannot_replay(self):
        self.api.error = True
        code = self.actions.draft(self.who, ["中文", "English", "日本語"])
        with self.assertRaises(TimeoutError):
            await self.actions.confirm(self.who, code)
        with self.assertRaises(ValueError):
            await self.actions.confirm(self.who, code)
        self.assertEqual(len(self.api.calls), 1)

    async def test_expiry_cancel_and_cross_group(self):
        code = self.actions.draft(self.who, ["中文", "English", "日本語"], now=1)
        with self.assertRaises(ValueError):
            await self.actions.confirm(self.who, code)
        code = self.actions.draft(self.who, ["中文", "English", "日本語"])
        self.assertFalse(self.actions.cancel(Identity("FixtureBot", "else", "u"), code))
        self.assertTrue(self.actions.cancel(self.who, code))
        with self.assertRaises(ValueError):
            await self.actions.confirm(self.who, code)
