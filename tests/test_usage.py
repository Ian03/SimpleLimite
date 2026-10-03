import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import codex_usage
import main
from app_paths import user_data_dir, codex_home
from codex_usage import CodexApiPoller, normalize_usage, normalize_limits

NOW = datetime.now(timezone.utc)
PAYLOAD = {
    "plan_type": "plus",
    "rate_limit": {"allowed": True, "limit_reached": False,
                   "primary_window": {"used_percent": 1, "limit_window_seconds": 18000,
                                      "reset_after_seconds": 600},
                   "secondary_window": {"used_percent": 37, "limit_window_seconds": 604800,
                                        "reset_at": int((NOW + timedelta(days=3)).timestamp())}},
    "rate_limit_reset_credits": {"available_count": 2},
    "additional_rate_limits": None,
    "model_usage": None,
}


def response(data=None, status=200):
    r = Mock(status_code=status, ok=status == 200, headers={})
    r.json.return_value = copy.deepcopy(data)
    return r


class UsageTests(unittest.TestCase):
    def test_both_windows_and_one_percent(self):
        limits, notes = normalize_usage(PAYLOAD, NOW)
        self.assertEqual([l["pct"] for l in limits], [1, 37])
        self.assertIn("Semanal", limits[1]["label"])
        self.assertEqual(limits[0]["reset_at"], NOW + timedelta(seconds=600))
        self.assertIn("Resets disponíveis: 2", notes)
        self.assertEqual(main._normalize({"five_hour": {"utilization": 1}})[0]["pct"], 1)
        extra = main._normalize({"extra_usage": {"used_credits": 200, "monthly_limit": 1000}})[0]
        self.assertEqual(extra["pct"], 20)
        self.assertEqual(extra["used_usd"], 2)

    def test_local_and_weekly_primary_formats(self):
        limits = normalize_limits({"primary": {"used_percent": 22, "window_minutes": 10080,
                                               "resets_at": 1900000000},
                                   "secondary": {"used_percent": 4, "window_minutes": 300}}, NOW)
        self.assertIn("Semanal", limits[0]["label"])
        self.assertIn("5h", limits[1]["label"])
        self.assertEqual(limits[0]["reset_at"].timestamp(), 1900000000)

    def test_extras_and_missing_resets(self):
        data = copy.deepcopy(PAYLOAD)
        data.pop("rate_limit_reset_credits")
        data["code_review_rate_limit"] = data["rate_limit"]
        data["additional_rate_limits"] = [{"limit_name": "Modelo", "rate_limit": data["rate_limit"]}]
        data["model_usage"] = {"modelo": {"available": False}}
        data["rate_limit"]["limit_reached"] = True
        limits, notes = normalize_usage(data, NOW)
        self.assertEqual(len(limits), 6)
        self.assertIn("Resets disponíveis: não informados", notes)
        self.assertIn("Limite de uso atingido", notes)
        self.assertIn("modelo: indisponível", notes)

    def test_invalid_payload_is_not_zero_usage(self):
        with self.assertRaises(ValueError):
            normalize_usage({}, NOW)
        data = copy.deepcopy(PAYLOAD)
        data["rate_limit"]["primary_window"]["used_percent"] = "nan"
        with self.assertRaises(ValueError):
            normalize_usage(data, NOW)

    def test_paths_independent_of_working_directory(self):
        with patch.dict("os.environ", {"LOCALAPPDATA": "C:/user/data", "CODEX_HOME": "C:/custom/codex"}):
            self.assertEqual(user_data_dir(), Path("C:/user/data/SimpleLimite"))
            self.assertEqual(codex_home(), Path("C:/custom/codex").resolve())

    def test_cache_reset_details_account_switch_and_rate_limit(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            auth = path / "auth.json"
            auth.write_text(json.dumps({"tokens": {"access_token": "secret", "account_id": "a"}}))
            details = {"credits": [{"id": "private-id", "status": "available",
                                     "expires_at": "2030-01-01T00:00:00Z"},
                                    {"status": "redeemed", "expires_at": "2020-01-01T00:00:00Z"}]}
            with patch.object(codex_usage, "codex_home", return_value=path), \
                 patch.object(codex_usage, "DATA_DIR", path / "cache"), \
                 patch.object(codex_usage.requests, "get") as get:
                p = CodexApiPoller()
                get.side_effect = [response(PAYLOAD), response(details)]
                p.fetch_now()
                self.assertFalse(p.stale)
                self.assertTrue(any("01/01" in n or "31/12" in n for n in p.notes))
                saved = next((path / "cache").glob("*.json")).read_text()
                self.assertNotIn("secret", saved)
                self.assertNotIn("private-id", saved)
                restarted = CodexApiPoller()
                get.side_effect = requests.ConnectionError()
                restarted.fetch_now()
                self.assertTrue(restarted.stale)
                self.assertEqual(restarted.limits, p.limits)
                get.side_effect = None
                get.return_value = response(status=429)
                p.fetch_now()
                count = get.call_count
                p.fetch_now()
                self.assertEqual(get.call_count, count)
                self.assertTrue(p.stale)
                self.assertIn("rate limit", p.error)
                auth.write_text(json.dumps({"tokens": {"access_token": "other", "account_id": "b"}}))
                get.return_value = response(status=401)
                p.fetch_now()
                self.assertEqual(p.limits, [])
                self.assertIn("Autenticação", p.error)

    def test_today_uses_deltas_in_sessions_spanning_days(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            yesterday = NOW - timedelta(days=1)
            events = [{"type": "session_meta", "payload": {"cwd": "C:/projects/test"}}]
            for ts, total in ((yesterday, 100), (NOW, 150), (NOW, 150), (NOW, 180)):
                events.append({"timestamp": ts.isoformat(), "type": "event_msg", "payload": {
                    "type": "token_count", "info": {"total_token_usage": {"input_tokens": total}},
                    "rate_limits": {"primary": {"used_percent": 1, "window_minutes": 300},
                                    "secondary": {"used_percent": 37, "window_minutes": 10080}}}})
            (path / "session.jsonl").write_text("\n".join(json.dumps(e) for e in events))
            with patch.object(main, "CODEX_SESSIONS_DIR", path):
                loader = main.CodexLoader()
                loader.reload()
                self.assertEqual(loader.today.total, 80)
                self.assertEqual(loader.alltime.total, 180)
                self.assertEqual(len(loader.limits), 2)
                self.assertEqual(loader.fetched_at, NOW)


if __name__ == "__main__":
    unittest.main()
