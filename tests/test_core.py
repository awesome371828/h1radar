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


class RussiaGeofenceTests(unittest.TestCase):
    def test_accepts_moscow(self):
        self.assertTrue(h1.point_in_russia(37.6173, 55.7558))

    def test_accepts_bataysk(self):
        self.assertTrue(h1.point_in_russia(39.75, 47.14))

    def test_rejects_valencia(self):
        self.assertFalse(h1.point_in_russia(-0.3763, 39.4699))


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


class ModerationExpansionTests(unittest.TestCase):
    def test_schema_contains_persistent_restrictions_and_city_requests(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS user_restrictions", h1.SCHEMA)
        self.assertIn("CREATE TABLE IF NOT EXISTS city_requests", h1.SCHEMA)

    def test_owner_city_management_routes_exist(self):
        paths = {route.path for route in h1.app.routes}
        self.assertIn("/api/moderation/cities", paths)
        self.assertIn("/api/moderation/cities/{city_id}", paths)
        self.assertIn("/api/moderation/city-requests", paths)

    def test_account_restriction_and_chat_review_routes_exist(self):
        paths = {route.path for route in h1.app.routes}
        self.assertIn("/api/moderation/users/{telegram_id}/restriction", paths)
        self.assertIn("/api/moderation/chat", paths)

    def test_moderation_user_directory_exposes_restriction_state(self):
        self.assertIn("restriction_expires_at", h1.app.openapi_json() if hasattr(h1.app, "openapi_json") else h1.SCHEMA + open("h1.py", encoding="utf-8").read())

    def test_city_edit_restriction_and_chat_moderation_ui_are_present(self):
        from pathlib import Path
        html = (Path(__file__).resolve().parents[1] / "static" / "moderation.html").read_text(encoding="utf-8")
        self.assertIn('data-city-edit', html)
        self.assertIn('data-restrict', html)
        self.assertIn('data-unrestrict', html)
        self.assertIn('id="chatSearch"', html)
        self.assertIn('"/moderation/chat?city="', html)

    def test_profile_activity_stats_and_report_flow(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        backend = (root / "h1.py").read_text(encoding="utf-8")
        app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
        index = (root / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('"reports_total"', backend)
        self.assertIn('"confirmations_given"', backend)
        self.assertIn('submitNearby();', app_js)
        self.assertNotIn('id="submitNearby"', index)
        self.assertIn('new Notification(', app_js)

    def test_moscow_clock_and_collapsible_map_panel(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
        index = (root / "static" / "index.html").read_text(encoding="utf-8")
        home = (root / "static" / "home.html").read_text(encoding="utf-8")
        self.assertIn('timeZone:"Europe/Moscow"', app_js)
        self.assertIn('id="moscowClock"', index)
        self.assertIn('id="sideToggle"', index)
        self.assertIn('id="cityListToggle"', home)



class MandatorySignInTests(unittest.TestCase):
    def test_map_data_routes_require_auth_and_public_city_directory_is_separate(self):
        from pathlib import Path
        backend = (Path(__file__).resolve().parents[1] / "h1.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/public/cities")', backend)
        self.assertIn('async def cities(authorization:str|None=Header(default=None)):', backend)
        self.assertIn('async def feed(city:str|None=None,limit:int=50,hours:int=24,authorization:str|None=Header(default=None)):', backend)
        self.assertIn('async def city_chat(city:str,after:int=0,authorization:str|None=Header(default=None)):', backend)

    def test_map_is_hidden_until_telegram_login(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        index = (root / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn('class="auth-locked"', index)
        self.assertIn('id="authGate"', index)
        self.assertIn('id="gateLogin"', index)
        self.assertIn('async function startMapApp()', app_js)
        self.assertIn('.auth-locked .app', css)
        self.assertIn('API+"/auth/poll?', app_js)

    def test_h1_brand_assets_exist(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / "static" / "favicon.svg").exists())
        self.assertTrue((root / "static" / "h1-avatar.svg").exists())


if __name__ == "__main__":
    unittest.main()
