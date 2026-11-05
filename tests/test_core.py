import hashlib
import hmac
import json
import os
import time
import unittest
from urllib.parse import urlencode

os.environ.setdefault("BOT_TOKEN", "h1-test-token")
os.environ.setdefault("BOT_OWNER", "1")
os.environ.setdefault("WEBAPP_URL", "https://example.invalid")
os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@127.0.0.1/unused")

import h1


def signed_init_data(user_id=12345, auth_date=None):
    values = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAE-test",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", os.environ["BOT_TOKEN"].encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class WebAppUrlTests(unittest.TestCase):
    def test_adds_https_to_bare_domain(self):
        self.assertEqual(h1.normalize_webapp_url("h1radar.relaxdev.ru"), "https://h1radar.relaxdev.ru")

    def test_upgrades_http_to_https(self):
        self.assertEqual(h1.normalize_webapp_url("http://h1radar.relaxdev.ru"), "https://h1radar.relaxdev.ru")

    def test_keeps_https_url(self):
        self.assertEqual(h1.normalize_webapp_url("https://h1radar.relaxdev.ru/"), "https://h1radar.relaxdev.ru")


class PhoneValidationTests(unittest.TestCase):
    def test_accepts_russian_phone(self):
        self.assertEqual(h1.ru_phone("+7 (999) 123-45-67"), "+79991234567")

    def test_normalizes_russian_trunk_prefix(self):
        self.assertEqual(h1.ru_phone("8 999 123 45 67"), "+79991234567")

    def test_rejects_non_russian_phone(self):
        self.assertIsNone(h1.ru_phone("+49 151 12345678"))


class TelegramInitDataTests(unittest.TestCase):
    def test_valid_signature(self):
        result = h1.verify_init_data(signed_init_data())
        self.assertEqual(result["id"], 12345)

    def test_rejects_tampered_data(self):
        raw = signed_init_data().replace("hash=", "hash=f")
        with self.assertRaises(ValueError):
            h1.verify_init_data(raw)

    def test_rejects_expired_data(self):
        raw = signed_init_data(auth_date=int(time.time()) - 90000)
        with self.assertRaises(ValueError):
            h1.verify_init_data(raw)


if __name__ == "__main__":
    unittest.main()
