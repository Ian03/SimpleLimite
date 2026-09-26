#!/usr/bin/env python3
"""
Claude Token Monitor
Minimal pill + expanded detail view.
Session limits via Anthropic/Cursor OAuth APIs; project costs via local JSONL.
"""

import json
import os
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests as _requests

from PIL import Image, ImageDraw, ImageFont
import pystray
from pystray import MenuItem as item
from PySide6.QtWidgets import QApplication
from ui import MonitorWindow

# ── Paths ─────────────────────────────────────────────────────────────────────
CLAUDE_DIR   = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
CREDS_FILE   = CLAUDE_DIR / ".credentials.json"
CONFIG_FILE  = Path(__file__).parent / "config.json"
LOG_FILE     = Path(__file__).parent / "simplelimite.log"
CODEX_DIR    = Path.home() / ".codex"
CODEX_SESSIONS_DIR = CODEX_DIR / "sessions"
CURSOR_DIR   = Path.home() / ".cursor"
CURSOR_PROJECTS_DIR = CURSOR_DIR / "projects"
CURSOR_AUTH_FILE = Path(os.environ.get("APPDATA", "")) / "Cursor" / "auth.json"
CURSOR_STATE_DB  = Path(os.environ.get("APPDATA", "")) / "Cursor" / "User" / "globalStorage" / "state.vscdb"

# ── Intervals ─────────────────────────────────────────────────────────────────
POLL_API_SEC  = 60
POLL_JSONL_SEC = 30
AUTO_COLLAPSE_ON_FOCUS_OUT = False

# ── UI sizes ──────────────────────────────────────────────────────────────────
PILL_W, PILL_H = 260, 62
WIN_W,  WIN_H  = 410, 610

# ── Pricing USD / 1M tokens ───────────────────────────────────────────────────
PRICING = {
    "claude-opus-4-8":   {"i": 15.00, "o": 75.00, "cc": 18.75, "cr": 1.50},
    "claude-sonnet-4-6": {"i":  3.00, "o": 15.00, "cc":  3.75, "cr": 0.30},
    "claude-haiku-4-5":  {"i":  0.80, "o":  4.00, "cc":  1.00, "cr": 0.08},
    "default":           {"i":  3.00, "o": 15.00, "cc":  3.75, "cr": 0.30},
}

# ── Palette ───────────────────────────────────────────────────────────────────
BG     = "#090b10"
CARD   = "#11151d"
BORDER = "#242b38"
TXT    = "#f1f5fb"
MUTED  = "#8a94a6"
BLUE   = "#70a7ff"
GREEN  = "#5bd6a2"
ORANGE = "#ffb86b"
RED    = "#ff7185"

ALERT_PCT = 0.70


def _log_error(context: str, exc: BaseException) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] {context}\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    except Exception:
        pass


def _thread(target, name: str):
    def _runner():
        try:
            target()
        except Exception as exc:
            _log_error(f"Thread crashed: {name}", exc)

    threading.Thread(target=_runner, name=name, daemon=True).start()


def _install_exception_logging():
    def _excepthook(exc_type, exc, tb):
        _log_error("Unhandled exception", exc)
        try:
            if sys.__excepthook__:
                sys.__excepthook__(exc_type, exc, tb)
        except Exception:
            pass

    def _threading_excepthook(args):
        _log_error(f"Unhandled thread exception: {args.thread.name}", args.exc_value)

    sys.excepthook = _excepthook
    threading.excepthook = _threading_excepthook


# ════════════════════════════════════════════════════════════════════════════════
#  Formatters
# ════════════════════════════════════════════════════════════════════════════════
def fmt_tok(n: int) -> str:
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}K"
    return str(n)

def fmt_cost(usd: float) -> str:
    if usd == 0:    return "$0.00"
    if usd < 0.001: return "< $0.001"
    if usd < 1:     return f"${usd:.4f}"
    return f"${usd:.2f}"

def fmt_dur(mins: int) -> str:
    if mins <= 0: return "agora"
    if mins < 60: return f"{mins}min"
    h, m = divmod(mins, 60)
    return f"{h}h {m:02d}m" if m else f"{h}h"

def bar_color(pct: float) -> str:
    if pct >= 90:             return RED
    if pct >= ALERT_PCT*100:  return ORANGE
    return BLUE

# ════════════════════════════════════════════════════════════════════════════════
#  API layer
# ════════════════════════════════════════════════════════════════════════════════
_API_URL = "https://api.anthropic.com/api/oauth/usage"

# Exact top-level keys returned by the API
_LABELS: dict[str, str] = {
    "five_hour":        "Sessão atual",
    "seven_day":        "Semanal (todos)",
    "seven_day_sonnet": "Semanal Sonnet",
    "seven_day_opus":   "Semanal Opus",
    "seven_day_haiku":  "Semanal Haiku",
    "seven_day_cowork": "Semanal Cowork",
    "extra_usage":      "Créditos Extra",
    # fallback aliases
    "session":          "Sessão atual",
    "5h":               "Sessão atual",
    "weekly":           "Semanal (todos)",
    "weekly_sonnet":    "Semanal Sonnet",
    "weekly_opus":      "Semanal Opus",
    "weekly_haiku":     "Semanal Haiku",
    "extra":            "Créditos Extra",
    "cowork":           "Semanal Cowork",
}

# Keys that represent the session limit — always shown first in the pill
_SESSION_KEYS: set[str] = {"five_hour", "session", "5h", "rate_limit"}

def _pretty(key: str) -> str:
    if key in _LABELS:
        return _LABELS[key]
    low = key.lower()
    for k, v in _LABELS.items():
        if k in low:
            return v
    return key.replace("_", " ").title()

def _parse_dt(v) -> "datetime | None":
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None

def _read_token() -> "str | None":
    for attempt in range(3):
        try:
            data = json.loads(CREDS_FILE.read_text(encoding="utf-8"))
            # Actual structure: {"claudeAiOauth": {"accessToken": "sk-ant-oat01-..."}}
            if oauth := data.get("claudeAiOauth"):
                if tok := oauth.get("accessToken"):
                    return tok
            # Legacy flat keys
            for key in ("claudeAiOauthAccessToken", "access_token", "token", "oauthToken"):
                if tok := data.get(key):
                    return tok
            # Last resort: first long string in any nested dict
            for v in data.values():
                if isinstance(v, dict):
                    for sv in v.values():
                        if isinstance(sv, str) and len(sv) > 20:
                            return sv
            break
        except (OSError, json.JSONDecodeError):
            if attempt < 2:
                time.sleep(0.2)
                continue
            break
        except Exception:
            break
    return None

def _normalize(data: dict) -> list[dict]:
    """
    Real API response: top-level keys, each a dict with utilization + resets_at.
    Extra usage pool has used_credits / monthly_limit (cents).
    """
    limits: list[dict] = []

    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        pct_raw = val.get("utilization")
        if pct_raw is None:
            continue

        pct = float(pct_raw)
        if 0 < pct <= 1:          # decimal fraction → percent
            pct *= 100
        pct = min(max(pct, 0), 100)

        reset_at = _parse_dt(val.get("resets_at") or val.get("reset_at"))

        # Credits pool (values are in cents)
        used_usd = float(val.get("used_credits") or 0) / 100
        cap_usd  = float(val.get("monthly_limit") or 0) / 100

        limits.append({
            "label":      _pretty(key),
            "pct":        pct,
            "reset_at":   reset_at,
            "kind":       "credits" if cap_usd > 0 else "timed",
            "used_usd":   used_usd,
            "cap_usd":    cap_usd,
            "is_session": key in _SESSION_KEYS,
        })

    # Session first, then by % descending
    limits.sort(key=lambda x: (not x["is_session"], -x["pct"]))
    return limits


def fetch_api() -> "tuple[list[dict], str | None]":
    token = _read_token()
    if not token:
        return [], "Token não encontrado (~/.claude/.credentials.json)"

    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "SimpleLimite/1.0",
    }
    try:
        # requests picks up Windows system proxy automatically
        resp = _requests.get(_API_URL, headers=headers, timeout=10)
        if resp.status_code in (401, 403):
            return [], "Token expirado — reabra o Claude Code"
        if resp.status_code == 429:
            return [], "Rate limited"
        if not resp.ok:
            return [], f"HTTP {resp.status_code}"
        return _normalize(resp.json()), None
    except _requests.exceptions.ConnectionError as e:
        return [], f"Sem conexão: {e}"
    except _requests.exceptions.Timeout:
        return [], "Timeout ao contactar a API"
    except Exception as e:
        return [], str(e)


# ════════════════════════════════════════════════════════════════════════════════
#  JSONL stats (today / all-time / per-project costs)
# ════════════════════════════════════════════════════════════════════════════════
class Stats:
    __slots__ = ("inp", "out", "cc", "cr", "cost")

    def __init__(self):
        self.inp = self.out = self.cc = self.cr = 0
        self.cost = 0.0

    def add(self, usage: dict, model: str):
        p  = PRICING.get(
            next((k for k in PRICING if k != "default" and k in model), "default"),
            PRICING["default"])
        i  = usage.get("input_tokens", 0)
        o  = usage.get("output_tokens", 0)
        cc = usage.get("cache_creation_input_tokens", 0)
        cr = usage.get("cache_read_input_tokens", 0)
        self.inp  += i;  self.out += o
        self.cc   += cc; self.cr  += cr
        self.cost += (i*p["i"] + o*p["o"] + cc*p["cc"] + cr*p["cr"]) / 1_000_000

    @property
    def total(self):
        return self.inp + self.out + self.cc


class CodexStats:
    __slots__ = ("inp", "out", "cached", "reasoning")

    def __init__(self):
        self.inp = self.out = self.cached = self.reasoning = 0

    def add(self, usage: dict):
        self.inp       += int(usage.get("input_tokens") or 0)
        self.out       += int(usage.get("output_tokens") or 0)
        self.cached    += int(usage.get("cached_input_tokens") or 0)
        self.reasoning += int(usage.get("reasoning_output_tokens") or 0)

    @property
    def total(self):
        return self.inp + self.out


def _readable_name(folder: str) -> str:
    parts = folder.replace("--", "\x00").split("\x00")
    return parts[-1].replace("-", " ").strip() if parts else folder


def _readable_path_name(path: str) -> str:
    if not path:
        return "Sem projeto"
    try:
        return Path(path).name or path
    except Exception:
        return path


class Loader:
    def __init__(self):
        self.today    = Stats()
        self.alltime  = Stats()
        self.projects: dict[str, Stats] = {}
        self.updated_at: "datetime | None" = None
        self._lock = threading.Lock()

    def reload(self):
        today_date = datetime.now().date()
        today = Stats(); alltime = Stats(); projects: dict[str, Stats] = {}

        if PROJECTS_DIR.exists():
            for proj_dir in PROJECTS_DIR.iterdir():
                if not proj_dir.is_dir():
                    continue
                name = _readable_name(proj_dir.name)
                proj = Stats()
                for jf in proj_dir.glob("*.jsonl"):
                    try:
                        with open(jf, encoding="utf-8", errors="ignore") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    d = json.loads(line)
                                except json.JSONDecodeError:
                                    continue
                                if d.get("type") != "assistant":
                                    continue
                                msg   = d.get("message", {})
                                usage = msg.get("usage")
                                if not usage:
                                    continue
                                model = msg.get("model", "claude-sonnet-4-6")
                                alltime.add(usage, model)
                                proj.add(usage, model)
                                ts_raw = d.get("timestamp", "")
                                if ts_raw:
                                    try:
                                        ts = datetime.fromisoformat(
                                            ts_raw.replace("Z", "+00:00"))
                                        if ts.astimezone().date() == today_date:
                                            today.add(usage, model)
                                    except Exception:
                                        pass
                    except Exception:
                        continue
                if proj.total > 0:
                    projects[name] = proj

        projects = dict(sorted(projects.items(),
                               key=lambda x: x[1].total, reverse=True))
        with self._lock:
            if alltime.total == 0 and self.alltime.total > 0:
                self.updated_at = datetime.now()
                return
            self.today    = today
            self.alltime  = alltime
            self.projects = projects
            self.updated_at = datetime.now()


class CodexLoader:
    def __init__(self):
        self.today    = CodexStats()
        self.alltime  = CodexStats()
        self.projects: dict[str, CodexStats] = {}
        self.limits: list[dict] = []
        self.error: "str | None" = None
        self.updated_at: "datetime | None" = None
        self._lock = threading.Lock()

    def reload(self):
        today_date = datetime.now().date()
        today = CodexStats(); alltime = CodexStats(); projects: dict[str, CodexStats] = {}
        latest_limit = None
        latest_limit_ts = None

        if not CODEX_SESSIONS_DIR.exists():
            with self._lock:
                self.error = "Codex não encontrado (~/.codex/sessions)"
                self.updated_at = datetime.now()
            return

        try:
            files = list(CODEX_SESSIONS_DIR.rglob("*.jsonl"))
        except Exception as e:
            with self._lock:
                self.error = f"Erro ao ler Codex: {e}"
                self.updated_at = datetime.now()
            return

        for jf in files:
            cwd = ""
            last_usage = None
            last_usage_ts = None

            try:
                with open(jf, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        payload = d.get("payload") or {}
                        ts = _parse_dt(d.get("timestamp"))
                        if d.get("type") in ("session_meta", "turn_context"):
                            cwd = payload.get("cwd") or cwd

                        if d.get("type") != "event_msg" or payload.get("type") != "token_count":
                            continue

                        info = payload.get("info") or {}
                        usage = info.get("total_token_usage")
                        if usage:
                            last_usage = usage
                            last_usage_ts = ts

                        lim = self._parse_limit(payload.get("rate_limits"), ts)
                        if lim and (latest_limit_ts is None or (ts and ts > latest_limit_ts)):
                            latest_limit = lim
                            latest_limit_ts = ts
            except Exception:
                continue

            if not last_usage:
                continue

            s = CodexStats()
            s.add(last_usage)
            alltime.add(last_usage)
            if last_usage_ts and last_usage_ts.astimezone().date() == today_date:
                today.add(last_usage)

            name = _readable_path_name(cwd)
            if name not in projects:
                projects[name] = CodexStats()
            projects[name].add(last_usage)

        projects = dict(sorted(projects.items(), key=lambda x: x[1].total, reverse=True))
        with self._lock:
            if alltime.total == 0 and self.alltime.total > 0:
                self.updated_at = datetime.now()
                return
            self.today    = today
            self.alltime  = alltime
            self.projects = projects
            self.limits   = [latest_limit] if latest_limit else self.limits
            self.error    = None if (alltime.total > 0 or latest_limit) else "Sem dados do Codex"
            self.updated_at = datetime.now()

    def _parse_limit(self, rate_limits, ts):
        if not isinstance(rate_limits, dict):
            return None
        primary = rate_limits.get("primary") or {}
        pct = primary.get("used_percent")
        if pct is None:
            return None
        reset_at = None
        if primary.get("resets_at"):
            try:
                reset_at = datetime.fromtimestamp(float(primary["resets_at"]), tz=timezone.utc)
            except Exception:
                reset_at = None
        return {
            "label": "Codex",
            "pct": min(max(float(pct), 0), 100),
            "reset_at": reset_at,
            "kind": "timed",
            "used_usd": 0,
            "cap_usd": 0,
            "is_session": True,
            "fetched_at": ts,
        }

    def mins_to_reset(self, lim: dict) -> int:
        ra = lim.get("reset_at")
        if not ra:
            return 0
        return max(0, int((ra - datetime.now(tz=timezone.utc)).total_seconds() / 60))


# ════════════════════════════════════════════════════════════════════════════════
#  Cursor API + local stats
# ════════════════════════════════════════════════════════════════════════════════
_CURSOR_API_BASE = "https://api2.cursor.sh/aiserver.v1.DashboardService"
_CURSOR_OAUTH_URL = "https://api2.cursor.sh/oauth/token"
_CURSOR_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"


def _parse_cursor_ms(value) -> "datetime | None":
    if value is None or value == "":
        return None
    try:
        ms = int(float(value))
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except Exception:
        return None


def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    pad = "=" * ((4 - len(payload) % 4) % 4)
    try:
        import base64
        raw = base64.urlsafe_b64decode(payload + pad)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _is_token_expired(token: str, skew_sec: int = 60) -> bool:
    exp = _decode_jwt_payload(token).get("exp")
    if not isinstance(exp, (int, float)):
        return False
    return datetime.now(tz=timezone.utc).timestamp() >= exp - skew_sec


def _read_cursor_token_from_db() -> "tuple[str | None, str | None]":
    if not CURSOR_STATE_DB.exists():
        return None, None
    try:
        con = sqlite3.connect(f"file:{CURSOR_STATE_DB}?mode=ro", uri=True)
        access = refresh = None
        for key, target in (
            ("cursorAuth/accessToken", "access"),
            ("cursorAuth/refreshToken", "refresh"),
        ):
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key = ? LIMIT 1", (key,)
            ).fetchone()
            if not row:
                continue
            val = str(row[0]).strip().strip('"').strip("'")
            if target == "access":
                access = val or None
            else:
                refresh = val or None
        con.close()
        return access, refresh
    except Exception:
        return None, None


def _refresh_cursor_token(refresh_token: str) -> "str | None":
    try:
        resp = _requests.post(
            _CURSOR_OAUTH_URL,
            json={
                "grant_type": "refresh_token",
                "client_id": _CURSOR_CLIENT_ID,
                "refresh_token": refresh_token,
            },
            timeout=10,
        )
        if not resp.ok:
            return None
        data = resp.json()
        if data.get("shouldLogout"):
            return None
        return data.get("access_token")
    except Exception:
        return None


def _read_cursor_token() -> "str | None":
    access = refresh = None
    if CURSOR_AUTH_FILE.exists():
        try:
            data = json.loads(CURSOR_AUTH_FILE.read_text(encoding="utf-8"))
            access = data.get("accessToken") or data.get("access_token")
            refresh = data.get("refreshToken") or data.get("refresh_token")
        except Exception:
            pass
    if not access:
        access, refresh = _read_cursor_token_from_db()
    if access and _is_token_expired(access) and refresh:
        access = _refresh_cursor_token(refresh) or access
    return access


def _cursor_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "User-Agent": "SimpleLimite/1.0",
    }


def _cursor_post(token: str, endpoint: str, body: dict | None = None) -> dict:
    resp = _requests.post(
        f"{_CURSOR_API_BASE}/{endpoint}",
        headers=_cursor_headers(token),
        json=body if body is not None else {},
        timeout=15,
    )
    if resp.status_code in (401, 403):
        raise PermissionError("Token expirado — reabra o Cursor")
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return resp.json()


def _normalize_cursor_limits(usage_raw: dict, plan_name: str) -> list[dict]:
    plan = usage_raw.get("planUsage") or {}
    reset_at = _parse_cursor_ms(
        usage_raw.get("billingCycleEnd") or usage_raw.get("billingCycleStart")
    )
    limits: list[dict] = []

    def _add(label: str, pct_raw, is_session: bool = False):
        if pct_raw is None:
            return
        pct = min(max(float(pct_raw), 0), 100)
        limits.append({
            "label": label,
            "pct": pct,
            "reset_at": reset_at,
            "kind": "timed",
            "used_usd": 0,
            "cap_usd": 0,
            "is_session": is_session,
        })

    _add("Total incluído", plan.get("totalPercentUsed"), is_session=True)
    _add("Auto + Composer", plan.get("autoPercentUsed"))
    _add("API", plan.get("apiPercentUsed"))

    limit_cents = plan.get("limit")
    total_spend = plan.get("totalSpend")
    if isinstance(limit_cents, (int, float)) and limit_cents > 0:
        used = float(total_spend or 0) / 100
        cap = float(limit_cents) / 100
        pct = min(max(used / cap * 100, 0), 100) if cap > 0 else 0
        limits.append({
            "label": f"Plano {plan_name}",
            "pct": pct,
            "reset_at": reset_at,
            "kind": "credits",
            "used_usd": used,
            "cap_usd": cap,
            "is_session": False,
        })

    limits.sort(key=lambda x: (not x["is_session"], -x["pct"]))
    return limits


def fetch_cursor_limits() -> "tuple[list[dict], str | None, str | None]":
    token = _read_cursor_token()
    if not token:
        return [], None, "Token não encontrado (Cursor auth)"
    try:
        usage_raw = _cursor_post(token, "GetCurrentPeriodUsage")
        plan_raw = _cursor_post(token, "GetPlanInfo")
        plan_name = (plan_raw.get("planInfo") or {}).get("planName") or "Cursor"
        if usage_raw.get("enabled") is False or not usage_raw.get("planUsage"):
            return [], plan_name, "Sem assinatura Cursor ativa"
        return _normalize_cursor_limits(usage_raw, plan_name), plan_name, None
    except PermissionError as e:
        return [], None, str(e)
    except _requests.exceptions.ConnectionError as e:
        return [], None, f"Sem conexão: {e}"
    except _requests.exceptions.Timeout:
        return [], None, "Timeout ao contactar Cursor API"
    except Exception as e:
        return [], None, str(e)


def fetch_cursor_usage_events(token: str) -> list[dict]:
    events: list[dict] = []
    page = 1
    while page <= 50:
        try:
            data = _cursor_post(token, "GetFilteredUsageEvents", {"pageSize": 100, "page": page})
        except Exception:
            break
        batch = data.get("usageEventsDisplay") or []
        if not batch:
            break
        events.extend(batch)
        total = data.get("totalUsageEventsCount")
        if isinstance(total, int) and len(events) >= total:
            break
        if len(batch) < 100:
            break
        page += 1
    return events


def _readable_cursor_project(folder: str) -> str:
    parts = folder.split("-")
    if len(parts) > 1 and len(parts[0]) == 1 and parts[0].isalpha():
        return parts[-1].replace("-", " ") or folder
    return folder.replace("-", " ")


class CursorStats:
    __slots__ = ("inp", "out", "cached", "cost_cents", "events")

    def __init__(self):
        self.inp = self.out = self.cached = self.events = 0
        self.cost_cents = 0.0

    def add(self, usage: dict):
        self.inp    += int(usage.get("inputTokens") or 0)
        self.out    += int(usage.get("outputTokens") or 0)
        self.cached += int(usage.get("cacheReadTokens") or 0)
        self.cost_cents += float(usage.get("totalCents") or 0)
        self.events += 1

    @property
    def total(self):
        return self.inp + self.out


class CursorProjectStats:
    __slots__ = ("messages",)

    def __init__(self):
        self.messages = 0


class CursorLoader:
    def __init__(self):
        self.today    = CursorStats()
        self.alltime  = CursorStats()
        self.projects: dict[str, CursorProjectStats] = {}
        self.limits: list[dict] = []
        self.plan_name: "str | None" = None
        self.error: "str | None" = None
        self.updated_at: "datetime | None" = None
        self._lock = threading.Lock()

    def reload(self):
        today_date = datetime.now().date()
        today = CursorStats()
        alltime = CursorStats()
        projects: dict[str, CursorProjectStats] = {}

        token = _read_cursor_token()
        limits: list[dict] = []
        plan_name = None
        err = None

        if not token:
            err = "Cursor não encontrado (auth)"
        else:
            limits, plan_name, err = fetch_cursor_limits()
            if token and not err:
                for ev in fetch_cursor_usage_events(token):
                    usage = ev.get("tokenUsage")
                    if not usage:
                        continue
                    alltime.add(usage)
                    ts = _parse_cursor_ms(ev.get("timestamp"))
                    if ts and ts.astimezone().date() == today_date:
                        today.add(usage)

        if CURSOR_PROJECTS_DIR.exists():
            for proj_dir in CURSOR_PROJECTS_DIR.iterdir():
                if not proj_dir.is_dir():
                    continue
                name = _readable_cursor_project(proj_dir.name)
                count = 0
                transcripts = proj_dir / "agent-transcripts"
                if not transcripts.exists():
                    continue
                for jf in transcripts.rglob("*.jsonl"):
                    try:
                        with open(jf, encoding="utf-8", errors="ignore") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    d = json.loads(line)
                                except json.JSONDecodeError:
                                    continue
                                if d.get("role") == "assistant":
                                    count += 1
                    except Exception:
                        continue
                if count > 0:
                    ps = CursorProjectStats()
                    ps.messages = count
                    projects[name] = ps

        projects = dict(sorted(projects.items(), key=lambda x: x[1].messages, reverse=True))

        with self._lock:
            if alltime.total == 0 and self.alltime.total > 0 and not limits:
                self.updated_at = datetime.now()
                return
            self.today = today
            self.alltime = alltime
            self.projects = projects
            if limits:
                self.limits = limits
            self.plan_name = plan_name
            self.error = err if not limits and alltime.total == 0 else None
            if err and limits:
                self.error = None
            if not limits and not err and alltime.total == 0 and not projects:
                self.error = "Sem dados do Cursor"
            self.updated_at = datetime.now()

    def mins_to_reset(self, lim: dict) -> int:
        ra = lim.get("reset_at")
        if not ra:
            return 0
        return max(0, int((ra - datetime.now(tz=timezone.utc)).total_seconds() / 60))


class CursorApiPoller:
    def __init__(self, loader: CursorLoader):
        self.loader = loader
        self.limits: list[dict] = []
        self.error: "str | None" = None
        self.fetched_at: "datetime | None" = None
        self.plan_name: "str | None" = None
        self._lock = threading.Lock()
        self._cbs: list = []

    def on_update(self, fn):
        self._cbs.append(fn)

    def _fire(self):
        for fn in self._cbs:
            try:
                fn()
            except Exception:
                pass

    def fetch_now(self):
        limits, plan_name, err = fetch_cursor_limits()
        with self._lock:
            if limits:
                self.limits = limits
                self.error = None
                self.plan_name = plan_name
                self.fetched_at = datetime.now()
                with self.loader._lock:
                    self.loader.limits = limits
                    self.loader.plan_name = plan_name
            else:
                self.error = err
                if not self.limits:
                    self.fetched_at = datetime.now()
        self._fire()

    def start(self):
        def _loop():
            while True:
                self.fetch_now()
                delay = POLL_API_SEC
                now = datetime.now(tz=timezone.utc)
                with self._lock:
                    lims = self.limits[:]
                for lim in lims:
                    ra = lim.get("reset_at")
                    if ra:
                        secs = (ra - now).total_seconds()
                        if 0 < secs <= 35:
                            delay = secs + 2
                            break
                time.sleep(delay)
        _thread(_loop, "cursor-api-poller")

    @property
    def top(self) -> "dict | None":
        with self._lock:
            return self.limits[0] if self.limits else None

    def mins_to_reset(self, lim: dict) -> int:
        ra = lim.get("reset_at")
        if not ra:
            return 0
        return max(0, int((ra - datetime.now(tz=timezone.utc)).total_seconds() / 60))


# ════════════════════════════════════════════════════════════════════════════════
#  API Poller
# ════════════════════════════════════════════════════════════════════════════════
class ApiPoller:
    def __init__(self):
        self.limits: list[dict]        = []
        self.error:  "str | None"      = None
        self.fetched_at: "datetime | None" = None
        self._lock = threading.Lock()
        self._cbs: list = []

    def on_update(self, fn):
        self._cbs.append(fn)

    def _fire(self):
        for fn in self._cbs:
            try:
                fn()
            except Exception:
                pass

    def fetch_now(self):
        limits, err = fetch_api()
        with self._lock:
            if limits:
                self.limits     = limits
                self.error      = None
                self.fetched_at = datetime.now()
            else:
                self.error = err
                if not self.limits:
                    self.fetched_at = datetime.now()
        self._fire()

    def start(self):
        def _loop():
            while True:
                self.fetch_now()
                delay = POLL_API_SEC
                # align to reset boundary when close (≤35 s)
                now = datetime.now(tz=timezone.utc)
                with self._lock:
                    lims = self.limits[:]
                for lim in lims:
                    ra = lim.get("reset_at")
                    if ra:
                        secs = (ra - now).total_seconds()
                        if 0 < secs <= 35:
                            delay = secs + 2
                            break
                time.sleep(delay)
        _thread(_loop, "claude-api-poller")

    @property
    def top(self) -> "dict | None":
        with self._lock:
            return self.limits[0] if self.limits else None

    def mins_to_reset(self, lim: dict) -> int:
        ra = lim.get("reset_at")
        if not ra:
            return 0
        return max(0, int((ra - datetime.now(tz=timezone.utc)).total_seconds() / 60))


# ════════════════════════════════════════════════════════════════════════════════
#  Tray icon image
# ════════════════════════════════════════════════════════════════════════════════
def _make_icon() -> Image.Image:
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d    = ImageDraw.Draw(img)
    d.ellipse([2, 2, 62, 62], fill="#70a7ff")
    try:    font = ImageFont.truetype("arialbd.ttf", 22)
    except Exception:
        try: font = ImageFont.truetype("arial.ttf", 22)
        except Exception: font = ImageFont.load_default()
    bb = d.textbbox((0, 0), "CT", font=font)
    tw, th = bb[2]-bb[0], bb[3]-bb[1]
    d.text(((size-tw)//2 - bb[0], (size-th)//2 - bb[1]), "CT", fill="white", font=font)
    return img


# ════════════════════════════════════════════════════════════════════════════════
#  Entry point
# ════════════════════════════════════════════════════════════════════════════════
def main():
    qt_app = QApplication(sys.argv)
    qt_app.setQuitOnLastWindowClosed(False)
    loader = Loader()
    codex_loader = CodexLoader()
    cursor_loader = CursorLoader()
    poller = ApiPoller()
    cursor_poller = CursorApiPoller(cursor_loader)
    window = MonitorWindow(loader, poller, codex_loader, cursor_loader, cursor_poller)

    def open_expanded(icon, _):
        window.request_ui_call(window.show_expanded)

    def quit_app(icon, _):
        icon.stop()
        window.request_ui_call(window.quit)

    tray = pystray.Icon(
        "claude-tokens",
        _make_icon(),
        "Claude Token Monitor",
        menu=pystray.Menu(
            item("Abrir detalhado", open_expanded, default=True),
            item("Sair", quit_app),
        ),
    )
    tray.run_detached()

    # Background: API polling
    poller.start()
    cursor_poller.start()

    # Background: JSONL refresh loop
    def _jsonl_loop():
        while True:
            try:
                loader.reload()
                codex_loader.reload()
                cursor_loader.reload()
                window.request_ui_update()
            except Exception as exc:
                _log_error("JSONL refresh failed", exc)
            time.sleep(POLL_JSONL_SEC)
    _thread(_jsonl_loop, "jsonl-refresh")

    # Initial JSONL load before first render
    _thread(
        lambda: (
            loader.reload(),
            codex_loader.reload(),
            cursor_loader.reload(),
            cursor_poller.fetch_now(),
            window.request_ui_update(),
        ),
        "initial-refresh",
    )

    # Start in minimal pill mode (always visible in corner)
    window.show()
    window._update_ui()
    qt_app.exec()
    tray.stop()


if __name__ == "__main__":
    _install_exception_logging()
    main()
