#!/usr/bin/env python3
"""
Claude Token Monitor
Minimal pill + expanded detail view.
Session limits via Anthropic/Cursor OAuth APIs; project costs via local JSONL.
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests as _requests

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont
import pystray
from pystray import MenuItem as item

# ── Paths ─────────────────────────────────────────────────────────────────────
CLAUDE_DIR   = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
CREDS_FILE   = CLAUDE_DIR / ".credentials.json"
CONFIG_FILE  = Path(__file__).parent / "config.json"
CODEX_DIR    = Path.home() / ".codex"
CODEX_SESSIONS_DIR = CODEX_DIR / "sessions"
CURSOR_DIR   = Path.home() / ".cursor"
CURSOR_PROJECTS_DIR = CURSOR_DIR / "projects"
CURSOR_AUTH_FILE = Path(os.environ.get("APPDATA", "")) / "Cursor" / "auth.json"
CURSOR_STATE_DB  = Path(os.environ.get("APPDATA", "")) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
COPILOT_DIR  = Path.home() / ".copilot"
COPILOT_DB = COPILOT_DIR / "session-store.db"
COPILOT_LEGACY_SESSIONS_DIR = Path.home() / ".github-copilot" / "sessions"
_APPDATA = Path(os.environ.get("APPDATA", ""))
# VS Code (+ Insiders) stores GitHub Copilot Chat history per-workspace as
# append-only "*.jsonl" patch logs under workspaceStorage/<hash>/chatSessions
# and, for windows opened without a folder, under globalStorage/emptyWindowChatSessions.
VSCODE_USER_DIRS = [
    _APPDATA / "Code" / "User",
    _APPDATA / "Code - Insiders" / "User",
]

# ── Intervals ─────────────────────────────────────────────────────────────────
POLL_API_SEC  = 60
POLL_JSONL_SEC = 30
AUTO_COLLAPSE_ON_FOCUS_OUT = False

# ── UI sizes ──────────────────────────────────────────────────────────────────
PILL_W, PILL_H = 310, 64
WIN_W,  WIN_H  = 390, 560

# ── Pricing USD / 1M tokens ───────────────────────────────────────────────────
PRICING = {
    "claude-opus-4-8":   {"i": 15.00, "o": 75.00, "cc": 18.75, "cr": 1.50},
    "claude-sonnet-4-6": {"i":  3.00, "o": 15.00, "cc":  3.75, "cr": 0.30},
    "claude-haiku-4-5":  {"i":  0.80, "o":  4.00, "cc":  1.00, "cr": 0.08},
    "default":           {"i":  3.00, "o": 15.00, "cc":  3.75, "cr": 0.30},
}

# ── Palette ───────────────────────────────────────────────────────────────────
BG     = "#0d1117"
CARD   = "#161b22"
BORDER = "#30363d"
TXT    = "#e6edf3"
MUTED  = "#8b949e"
BLUE   = "#58a6ff"
GREEN  = "#3fb950"
ORANGE = "#f0883e"
RED    = "#f85149"

ALERT_PCT = 0.70


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

def _shorten_home_path(path: str) -> str:
    """Replace the user's home directory prefix with '~' so long, combined
    source paths fit better in the compact UI."""
    if not path:
        return path
    home = str(Path.home())
    return path.replace(home, "~")


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


# ════════════════════════════════════════════════════════════════════════════════
#  VS Code "GitHub Copilot Chat" session parsing
#
#  VS Code persists each chat session as an append-only log of patch records
#  (one JSON object per line):
#    {"kind": 0, "v": <full document>}                    -- initial snapshot
#    {"kind": 1, "k": [<path>, ...], "v": <value>}         -- set value at path
#    {"kind": 2, "k": [<path>, ...], "v": [<items>, ...]}  -- append items to
#                                                             the array at path
#  Replaying all records reconstructs the final session document, which
#  contains a "requests" array — one entry per user turn.
# ════════════════════════════════════════════════════════════════════════════════
def _vscode_navigate(doc, path):
    cur = doc
    for key in path:
        if isinstance(cur, list):
            while len(cur) <= key:
                cur.append({})
            cur = cur[key]
        elif isinstance(cur, dict):
            if key not in cur or cur[key] is None:
                cur[key] = {}
            cur = cur[key]
        else:
            return None
    return cur


def _vscode_apply_set(doc, k, v):
    if not k:
        return doc
    parent = _vscode_navigate(doc, k[:-1])
    if parent is None:
        return doc
    last = k[-1]
    if isinstance(parent, list):
        while len(parent) <= last:
            parent.append(None)
        parent[last] = v
    elif isinstance(parent, dict):
        parent[last] = v
    return doc


def _vscode_apply_append(doc, k, v):
    target = _vscode_navigate(doc, k)
    if isinstance(target, list) and isinstance(v, list):
        target.extend(v)
    return doc


def _vscode_merge_chat_session(jf: Path) -> "dict | None":
    doc = None
    with open(jf, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = d.get("kind")
            if kind == 0:
                doc = d.get("v")
            elif doc is None:
                continue
            elif kind == 1:
                _vscode_apply_set(doc, d.get("k"), d.get("v"))
            elif kind == 2:
                _vscode_apply_append(doc, d.get("k"), d.get("v"))
    return doc


def _vscode_project_name_from_uri(uri: str) -> str:
    if not uri:
        return "Sem projeto"
    raw = uri
    for prefix in ("file:///", "file://", "vscode-remote://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    try:
        import urllib.parse
        decoded = urllib.parse.unquote(raw)
    except Exception:
        decoded = raw
    decoded = decoded.replace("/", "\\")
    return _readable_path_name(decoded)


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


# ════════════════════════════════════════════════════════════════════════════════
#  GitHub Copilot stats + loader
# ════════════════════════════════════════════════════════════════════════════════
class CopilotStats:
    __slots__ = ("completions", "sessions", "cost")

    def __init__(self):
        self.completions = 0  # "turns" (interações)
        self.sessions = 0
        self.cost = 0.0

    @property
    def total(self):
        return self.completions + self.sessions


class CopilotLoader:
    def __init__(self):
        self.today    = CopilotStats()
        self.alltime  = CopilotStats()
        self.projects: dict[str, CopilotStats] = {}
        self.limits: list[dict] = []
        self.error: "str | None" = None
        self.updated_at: "datetime | None" = None
        self.source_label = "Copilot CLI"
        self.source_path = str(COPILOT_DB)
        self._lock = threading.Lock()

    def reload(self):
        today_date = datetime.now().date()
        today = CopilotStats()
        alltime = CopilotStats()
        projects: dict[str, CopilotStats] = {}

        sources_used: list[str] = []
        source_paths: list[str] = []
        errors: list[str] = []

        # 1) Native Copilot CLI session store (preferred — has real turn data)
        if COPILOT_DB.exists():
            try:
                found = self._reload_from_cli_db(today, alltime, projects, today_date)
                source_paths.append(str(COPILOT_DB))
                if found:
                    sources_used.append("Copilot CLI")
            except Exception as e:
                errors.append(f"Copilot CLI ({e})")

        # 2) Legacy CLI session export (older installs)
        if COPILOT_LEGACY_SESSIONS_DIR.exists():
            try:
                found = self._reload_from_legacy_jsonl(today, alltime, projects, today_date)
                source_paths.append(str(COPILOT_LEGACY_SESSIONS_DIR))
                if found:
                    sources_used.append("Copilot CLI (legado)")
            except Exception as e:
                errors.append(f"Copilot CLI legado ({e})")

        # 3) VS Code "GitHub Copilot Chat" extension local history (fallback —
        #    works even when the standalone Copilot CLI was never used)
        try:
            found, checked_paths = self._reload_from_vscode_chat(today, alltime, projects, today_date)
            source_paths.extend(checked_paths)
            if found:
                sources_used.append("Copilot Chat (VS Code)")
        except Exception as e:
            errors.append(f"Copilot Chat VS Code ({e})")

        if sources_used:
            error = None
        elif errors:
            error = "Erro ao ler GitHub Copilot: " + "; ".join(errors)
        else:
            error = "GitHub Copilot não encontrado (CLI, legado ou VS Code)"

        source_label = " + ".join(sources_used) if sources_used else "Copilot"

        self._finish_reload(today, alltime, projects, error, source_label, source_paths)

    def _reload_from_cli_db(self, today, alltime, projects, today_date) -> bool:
        conn = sqlite3.connect(str(COPILOT_DB))
        conn.row_factory = sqlite3.Row
        found = False
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, cwd, repository, created_at
                FROM sessions
                ORDER BY created_at DESC
            """)
            sessions = cursor.fetchall()

            cursor.execute("""
                SELECT session_id, COUNT(*) AS turn_count
                FROM turns
                GROUP BY session_id
            """)
            turn_counts = {
                row["session_id"]: int(row["turn_count"] or 0)
                for row in cursor.fetchall()
            }

            for session in sessions:
                turn_count = turn_counts.get(session["id"], 0)
                if turn_count <= 0:
                    continue

                created_at = _parse_dt(session["created_at"]) or datetime.now(timezone.utc)
                project_name = self._copilot_project_name(session["repository"], session["cwd"])
                self._add_session_stats(
                    today,
                    alltime,
                    projects,
                    today_date,
                    created_at,
                    project_name,
                    turn_count,
                )
                found = True
        finally:
            conn.close()
        return found

    def _reload_from_legacy_jsonl(self, today, alltime, projects, today_date) -> bool:
        found = False
        for jf in COPILOT_LEGACY_SESSIONS_DIR.rglob("*.jsonl"):
            project_name = "Sem projeto"
            turn_count = 0
            session_ts = None

            try:
                with open(jf, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        project_name = self._legacy_project_name(entry, project_name, jf)
                        session_ts = session_ts or self._entry_timestamp(entry)
                        if self._is_assistant_turn(entry):
                            turn_count += 1
            except Exception:
                continue

            if turn_count <= 0:
                continue

            if session_ts is None:
                session_ts = datetime.fromtimestamp(jf.stat().st_mtime, tz=timezone.utc)

            self._add_session_stats(
                today,
                alltime,
                projects,
                today_date,
                session_ts,
                project_name,
                turn_count,
            )
            found = True
        return found

    def _reload_from_vscode_chat(self, today, alltime, projects, today_date):
        """Read GitHub Copilot Chat history persisted locally by VS Code
        (and VS Code Insiders), used as a fallback when the standalone
        Copilot CLI store has no data."""
        found = False
        checked_paths: list[str] = []

        for user_dir in VSCODE_USER_DIRS:
            ws_storage = user_dir / "workspaceStorage"
            if ws_storage.exists():
                checked_paths.append(str(ws_storage))
                for ws_dir in ws_storage.iterdir():
                    if not ws_dir.is_dir():
                        continue
                    chat_dir = ws_dir / "chatSessions"
                    if not chat_dir.exists():
                        continue
                    project_name = self._vscode_project_name(ws_dir)
                    for jf in chat_dir.glob("*.jsonl"):
                        if self._consume_vscode_chat_file(
                            jf, project_name, today, alltime, projects, today_date
                        ):
                            found = True

            empty_window_dir = user_dir / "globalStorage" / "emptyWindowChatSessions"
            if empty_window_dir.exists():
                checked_paths.append(str(empty_window_dir))
                for jf in empty_window_dir.glob("*.jsonl"):
                    if self._consume_vscode_chat_file(
                        jf, "VS Code (sem pasta)", today, alltime, projects, today_date
                    ):
                        found = True

        return found, checked_paths

    def _consume_vscode_chat_file(self, jf, project_name, today, alltime, projects, today_date) -> bool:
        try:
            doc = _vscode_merge_chat_session(jf)
        except Exception:
            return False

        if not doc:
            return False

        requests = doc.get("requests") or []
        turn_count = len(requests)
        if turn_count <= 0:
            return False

        session_ts = None
        for req in reversed(requests):
            ts = req.get("timestamp")
            if ts:
                try:
                    session_ts = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
                    break
                except Exception:
                    continue
        if session_ts is None:
            created_ms = doc.get("creationDate")
            if created_ms:
                try:
                    session_ts = datetime.fromtimestamp(int(created_ms) / 1000, tz=timezone.utc)
                except Exception:
                    session_ts = None
        if session_ts is None:
            session_ts = datetime.fromtimestamp(jf.stat().st_mtime, tz=timezone.utc)

        self._add_session_stats(
            today, alltime, projects, today_date, session_ts, project_name, turn_count
        )
        return True

    def _vscode_project_name(self, ws_dir: Path) -> str:
        workspace_json = ws_dir / "workspace.json"
        if workspace_json.exists():
            try:
                with open(workspace_json, encoding="utf-8", errors="ignore") as f:
                    meta = json.load(f)
                folder = meta.get("folder")
                if folder:
                    return _vscode_project_name_from_uri(str(folder))
            except Exception:
                pass
        return "VS Code (sem pasta)"

    def _finish_reload(self, today, alltime, projects, error, source_label, source_paths=None):
        projects = dict(sorted(
            projects.items(),
            key=lambda item: (item[1].completions, item[1].sessions),
            reverse=True,
        ))
        with self._lock:
            self.today = today
            self.alltime = alltime
            self.projects = projects
            self.error = error
            self.source_label = source_label
            self.source_path = " | ".join(source_paths) if source_paths else str(COPILOT_DB)
            self.updated_at = datetime.now()

    def _add_session_stats(self, today, alltime, projects, today_date, created_at, project_name, turn_count):
        alltime.sessions += 1
        alltime.completions += turn_count

        if created_at.astimezone().date() == today_date:
            today.sessions += 1
            today.completions += turn_count

        if project_name not in projects:
            projects[project_name] = CopilotStats()
        projects[project_name].sessions += 1
        projects[project_name].completions += turn_count

    def _copilot_project_name(self, repository, cwd):
        return repository or (_readable_path_name(cwd) if cwd else "Sem projeto")

    def _legacy_project_name(self, entry: dict, current_name: str, jf: Path) -> str:
        repo = entry.get("repository") or entry.get("repo")
        if repo:
            return str(repo)

        cwd = entry.get("cwd") or entry.get("workspace")
        if cwd:
            return _readable_path_name(str(cwd))

        meta = entry.get("session") or {}
        repo = meta.get("repository") or meta.get("repo")
        if repo:
            return str(repo)

        cwd = meta.get("cwd") or meta.get("workspace")
        if cwd:
            return _readable_path_name(str(cwd))

        return current_name if current_name != "Sem projeto" else _readable_path_name(jf.parent.name)

    def _entry_timestamp(self, entry: dict) -> "datetime | None":
        for key in ("timestamp", "created_at", "updated_at", "time"):
            ts = _parse_dt(entry.get(key))
            if ts:
                return ts
        meta = entry.get("session") or {}
        for key in ("created_at", "timestamp", "updated_at"):
            ts = _parse_dt(meta.get(key))
            if ts:
                return ts
        return None

    def _is_assistant_turn(self, entry: dict) -> bool:
        if entry.get("role") == "assistant":
            return True
        if entry.get("type") in {"assistant", "assistant_message", "response"}:
            return True
        message = entry.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            return True
        return False

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
        threading.Thread(target=_loop, daemon=True).start()

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
        threading.Thread(target=_loop, daemon=True).start()

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
#  Widget helpers
# ════════════════════════════════════════════════════════════════════════════════
def _btn(parent, text, cmd, color, size=13):
    return ctk.CTkButton(
        parent, text=text, width=28, height=28,
        fg_color="transparent", hover_color=BORDER,
        text_color=color, font=("Segoe UI", size),
        command=cmd, corner_radius=0,
    )


# ════════════════════════════════════════════════════════════════════════════════
#  Main window  (two modes: MINIMAL pill  ↔  EXPANDED detail)
# ════════════════════════════════════════════════════════════════════════════════
class MonitorWindow(ctk.CTk):
    MINIMAL  = "minimal"
    EXPANDED = "expanded"

    def __init__(self, loader: Loader, poller: ApiPoller, codex_loader: CodexLoader,
                 cursor_loader: CursorLoader, cursor_poller: CursorApiPoller, copilot_loader: CopilotLoader):
        super().__init__()
        self.loader = loader
        self.poller = poller
        self.codex_loader = codex_loader
        self.cursor_loader = cursor_loader
        self.cursor_poller = cursor_poller
        self.copilot_loader = copilot_loader
        self._mode  = self.MINIMAL
        self._tab   = "Claude"
        self._quitting = False
        self._dx = self._dy = self._wx = self._wy = 0

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.97)
        self.attributes("-toolwindow", True)
        self.configure(fg_color=BG)
        self.protocol("WM_DELETE_WINDOW", self._handle_window_close)
        self.bind("<Unmap>", self._handle_unmap)

        self.poller.on_update(lambda: self.after(0, self._update_ui))
        self.cursor_poller.on_update(lambda: self.after(0, self._update_ui))
        self._build()
        self.after(1000, self._keep_minimal_visible)

    def quit(self):
        self._quitting = True
        self.destroy()

    def _handle_window_close(self):
        if self._quitting:
            self.destroy()
            return
        self._go_minimal()
        self.deiconify()
        self.attributes("-topmost", True)
        self.lift()

    def _handle_unmap(self, _event=None):
        if self._quitting:
            return
        self.after(100, self._restore_if_hidden)

    def _restore_if_hidden(self):
        if self._quitting or not self.winfo_exists():
            return
        try:
            if self.state() in ("withdrawn", "iconic"):
                self._mode = self.MINIMAL
                self._build()
                self.deiconify()
                self.attributes("-topmost", True)
                self.lift()
        except Exception:
            pass

    def _keep_minimal_visible(self):
        if self._quitting or not self.winfo_exists():
            return
        if self._mode == self.MINIMAL:
            self._restore_if_hidden()
        self.after(2000, self._keep_minimal_visible)

    # ── drag ──────────────────────────────────────────────────────────────────
    def _bind_drag(self, w):
        w.bind("<ButtonPress-1>", self._drag_start)
        w.bind("<B1-Motion>",     self._drag_move)

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root, e.y_root
        self._wx, self._wy = self.winfo_x(), self.winfo_y()

    def _drag_move(self, e):
        self.geometry(
            f"+{self._wx + e.x_root - self._dx}+{self._wy + e.y_root - self._dy}")

    # ── placement ─────────────────────────────────────────────────────────────
    def _place(self, w, h):
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{sw-w-14}+{sh-h-54}")

    def _place_auto(self):
        """Measure content size then snap to bottom-right corner."""
        self.update_idletasks()
        w = self.winfo_reqwidth()
        h = self.winfo_reqheight()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{sw-w-14}+{sh-h-54}")

    # ── build / rebuild ────────────────────────────────────────────────────────
    def _build(self):
        for c in self.winfo_children():
            c.destroy()
        self.unbind("<FocusOut>")
        if self._mode == self.MINIMAL:
            self._build_minimal()
            self._place_auto()
        else:
            self._build_expanded()
            self._place(WIN_W, WIN_H)
            if AUTO_COLLAPSE_ON_FOCUS_OUT:
                self.bind("<FocusOut>", lambda e: self.after(200, self._focus_out))

    def _focus_out(self):
        try:
            if self.focus_get() is None:
                self._go_minimal()
        except Exception:
            pass

    # ── MINIMAL ───────────────────────────────────────────────────────────────
    def _build_minimal(self):
        outer = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0,
                             border_width=1, border_color=BORDER)
        outer.pack(padx=0, pady=0)
        self._bind_drag(outer)

        # single row: dot · label · bar · pct · reset · expand
        row = ctk.CTkFrame(outer, fg_color="transparent")
        row.pack(padx=10, pady=8)
        self._bind_drag(row)

        self._m_dot = ctk.CTkFrame(row, width=8, height=8,
                                   fg_color=BLUE, corner_radius=4)
        self._m_dot.pack(side="left", padx=(0, 7))
        self._m_dot.pack_propagate(False)

        self._m_lbl = ctk.CTkLabel(row, text="Sessão atual",
                                   font=("Segoe UI", 10, "bold"), text_color=TXT)
        self._m_lbl.pack(side="left", padx=(0, 8))
        self._bind_drag(self._m_lbl)

        self._m_bar = ctk.CTkProgressBar(row, width=110, height=5, corner_radius=0,
                                         fg_color=BORDER, progress_color=BLUE)
        self._m_bar.set(0)
        self._m_bar.pack(side="left", padx=(0, 6))

        self._m_pct = ctk.CTkLabel(row, text="—", width=34,
                                   font=("Segoe UI", 10, "bold"), text_color=BLUE)
        self._m_pct.pack(side="left", padx=(0, 4))

        self._m_reset = ctk.CTkLabel(row, text="", font=("Segoe UI", 8),
                                     text_color=MUTED)
        self._m_reset.pack(side="left", padx=(0, 6))

        _btn(row, "⊞", self._go_expanded, MUTED, 10).pack(side="left")

    # ── EXPANDED ──────────────────────────────────────────────────────────────
    def _build_expanded(self):
        # Header
        hdr = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=46)
        hdr.pack(fill="x", padx=8, pady=(8, 4))
        hdr.pack_propagate(False)
        self._bind_drag(hdr)

        ctk.CTkFrame(hdr, width=8, height=8, fg_color=BLUE,
                     corner_radius=4).place(x=12, rely=0.5, anchor="w")
        t = ctk.CTkLabel(hdr, text="Token Monitor",
                         font=("Segoe UI", 12, "bold"), text_color=TXT)
        t.place(x=26, rely=0.5, anchor="w")
        self._bind_drag(t)

        self._e_time = ctk.CTkLabel(hdr, text="", font=("Segoe UI", 9),
                                    text_color=MUTED)
        self._e_time.pack(side="right", padx=(0, 4))
        _btn(hdr, "⊟", self._go_minimal,       MUTED).pack(side="right", padx=(0, 2))
        _btn(hdr, "↻", self._manual_refresh,   BLUE).pack(side="right")

        self._tabs = ctk.CTkSegmentedButton(
            self,
            values=["Claude", "Codex", "Cursor", "Copilot"],
            command=self._set_tab,
            height=28,
            fg_color=CARD,
            selected_color=BLUE,
            selected_hover_color=BLUE,
            unselected_color=CARD,
            unselected_hover_color=BORDER,
            text_color=TXT,
        )
        self._tabs.pack(fill="x", padx=8, pady=(0, 4))
        self._tabs.set(self._tab)

        # ── API limits block ──────────────────────────────────────────────────
        sec_hdr = ctk.CTkFrame(self, fg_color="transparent")
        sec_hdr.pack(fill="x", padx=12, pady=(4, 2))
        ctk.CTkLabel(sec_hdr, text="LIMITES DE USO",
                     font=("Segoe UI", 9, "bold"), text_color=MUTED).pack(side="left")
        self._e_api_ts = ctk.CTkLabel(sec_hdr, text="",
                                      font=("Segoe UI", 8), text_color=MUTED)
        self._e_api_ts.pack(side="right")

        self._e_limits = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0)
        self._e_limits.pack(fill="x", padx=8, pady=(0, 4))

        # ── Today / All-time row ──────────────────────────────────────────────
        pair = ctk.CTkFrame(self, fg_color="transparent")
        pair.pack(fill="x", padx=8, pady=2)
        self._c_today   = self._mini_card(pair, "Hoje",  BLUE,  left=True)
        self._c_alltime = self._mini_card(pair, "Total", GREEN, left=False)

        # ── Projects ──────────────────────────────────────────────────────────
        ph = ctk.CTkFrame(self, fg_color="transparent")
        ph.pack(fill="x", padx=12, pady=(6, 2))
        ctk.CTkLabel(ph, text="PROJETOS",
                     font=("Segoe UI", 9, "bold"), text_color=MUTED).pack(side="left")

        self._e_proj = ctk.CTkScrollableFrame(
            self, fg_color=BG,
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=MUTED,
            corner_radius=0,
        )
        self._e_proj.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _mini_card(self, parent, label, accent, left: bool) -> ctk.CTkFrame:
        pad = (0, 4) if left else (4, 0)
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=0)
        card.pack(side="left", fill="x", expand=True, padx=pad)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(8, 0))
        ctk.CTkLabel(top, text=label, font=("Segoe UI", 8, "bold"),
                     text_color=MUTED).pack(side="left")
        card._cost = ctk.CTkLabel(top, text="$0.00",
                                  font=("Segoe UI", 9, "bold"), text_color=accent)
        card._cost.pack(side="right")

        card._tok = ctk.CTkLabel(card, text="0",
                                 font=("Segoe UI", 16, "bold"), text_color=TXT)
        card._tok.pack(anchor="w", padx=10)

        sub = ctk.CTkFrame(card, fg_color="transparent")
        sub.pack(fill="x", padx=10, pady=(0, 8))
        card._in  = ctk.CTkLabel(sub, text="↑ 0",  font=("Segoe UI", 8), text_color=BLUE)
        card._out = ctk.CTkLabel(sub, text="↓ 0",  font=("Segoe UI", 8), text_color=ORANGE)
        card._cr  = ctk.CTkLabel(sub, text="⚡ 0", font=("Segoe UI", 8), text_color=GREEN)
        card._in.pack(side="left", padx=(0, 5))
        card._out.pack(side="left", padx=(0, 5))
        card._cr.pack(side="left")
        return card

    def _set_tab(self, tab):
        self._tab = tab
        self._update_ui()

    # ── mode switches ──────────────────────────────────────────────────────────
    def _go_minimal(self):
        self._mode = self.MINIMAL
        self._build()
        self.deiconify()
        self.attributes("-topmost", True)
        self.lift()
        self._update_ui()

    def _go_expanded(self):
        self._mode = self.EXPANDED
        self._build()
        self.deiconify(); self.lift()
        self._update_ui()

    # ── update dispatch ────────────────────────────────────────────────────────
    def _update_ui(self):
        if self._mode == self.MINIMAL:
            self._update_minimal()
        else:
            self._update_expanded()

    # ── MINIMAL update ─────────────────────────────────────────────────────────
    def _update_minimal(self):
        if self._tab == "Codex":
            source = self.codex_loader
            top = self.codex_loader.limits[0] if self.codex_loader.limits else None
        elif self._tab == "Cursor":
            source = self.cursor_poller
            top = self.cursor_poller.top
        elif self._tab == "Copilot":
            # Copilot não tem API limits, então mostra sempre como 0%
            self._m_pct.configure(text="—", text_color=MUTED)
            self._m_bar.set(0)
            self._m_lbl.configure(text=f"Copilot: {self.copilot_loader.alltime.completions} turns")
            self._m_reset.configure(text="")
            return
        else:
            source = self.poller
            top = self.poller.top
        if top is None:
            self._m_pct.configure(text="—", text_color=MUTED)
            self._m_bar.set(0)
            self._m_lbl.configure(text=f"{self._tab}: sem dados")
            self._m_reset.configure(text="")
            return

        pct   = top["pct"]
        color = bar_color(pct)
        self._m_dot.configure(fg_color=color)
        self._m_lbl.configure(text=top["label"])
        self._m_pct.configure(text=f"{pct:.0f}%", text_color=color)
        self._m_bar.configure(progress_color=color)
        self._m_bar.set(pct / 100)

        mins = source.mins_to_reset(top)
        if mins > 0:
            rc = GREEN if mins > 60 else ORANGE
            self._m_reset.configure(text=f"reset {fmt_dur(mins)}", text_color=rc)
        else:
            self._m_reset.configure(text="")

    # ── EXPANDED update ────────────────────────────────────────────────────────
    def _update_expanded(self):
        if self._tab == "Codex":
            self._update_codex_expanded()
            return
        if self._tab == "Cursor":
            self._update_cursor_expanded()
            return
        if self._tab == "Copilot":
            self._update_copilot_expanded()
            return

        # API limits
        if hasattr(self, "_e_limits"):
            for w in self._e_limits.winfo_children():
                w.destroy()

            limits = self.poller.limits
            err    = self.poller.error

            if not limits and err:
                ctk.CTkLabel(self._e_limits, text=f"⚠ {err}",
                             font=("Segoe UI", 9), text_color=ORANGE).pack(padx=12, pady=10)
            elif not limits:
                ctk.CTkLabel(self._e_limits, text="Aguardando API…",
                             font=("Segoe UI", 9), text_color=MUTED).pack(padx=12, pady=10)
            else:
                for lim in limits:
                    self._render_bar(self._e_limits, lim)
                # small bottom padding
                ctk.CTkFrame(self._e_limits, fg_color="transparent",
                             height=6).pack()

            if self.poller.fetched_at:
                self._e_api_ts.configure(
                    text=self.poller.fetched_at.strftime("API %H:%M:%S"))

        # JSONL cards
        if hasattr(self, "_c_today"):
            self._fill_card(self._c_today,   self.loader.today)
            self._fill_card(self._c_alltime, self.loader.alltime)
        if self.loader.updated_at and hasattr(self, "_e_time"):
            self._e_time.configure(
                text=self.loader.updated_at.strftime("%H:%M:%S"))

        # Projects
        if hasattr(self, "_e_proj"):
            self._fill_projects()

    def _update_codex_expanded(self):
        if hasattr(self, "_e_limits"):
            for w in self._e_limits.winfo_children():
                w.destroy()

            limits = self.codex_loader.limits
            err = self.codex_loader.error
            if limits:
                for lim in limits:
                    self._render_bar(self._e_limits, lim, self.codex_loader)
                ctk.CTkFrame(self._e_limits, fg_color="transparent", height=6).pack()
            elif err:
                ctk.CTkLabel(self._e_limits, text=f"⚠ {err}",
                             font=("Segoe UI", 9), text_color=ORANGE).pack(padx=12, pady=10)
            else:
                ctk.CTkLabel(self._e_limits, text="Aguardando Codex…",
                             font=("Segoe UI", 9), text_color=MUTED).pack(padx=12, pady=10)

            if self.codex_loader.updated_at:
                self._e_api_ts.configure(
                    text=self.codex_loader.updated_at.strftime("Codex %H:%M:%S"))

        if hasattr(self, "_c_today"):
            self._fill_codex_card(self._c_today, self.codex_loader.today)
            self._fill_codex_card(self._c_alltime, self.codex_loader.alltime)
        if self.codex_loader.updated_at and hasattr(self, "_e_time"):
            self._e_time.configure(
                text=self.codex_loader.updated_at.strftime("%H:%M:%S"))

        if hasattr(self, "_e_proj"):
            self._fill_codex_projects()

    def _update_cursor_expanded(self):
        if hasattr(self, "_e_limits"):
            for w in self._e_limits.winfo_children():
                w.destroy()

            limits = self.cursor_poller.limits or self.cursor_loader.limits
            err = self.cursor_poller.error or self.cursor_loader.error
            if limits:
                for lim in limits:
                    self._render_bar(self._e_limits, lim, self.cursor_poller)
                ctk.CTkFrame(self._e_limits, fg_color="transparent", height=6).pack()
            elif err:
                ctk.CTkLabel(self._e_limits, text=f"⚠ {err}",
                             font=("Segoe UI", 9), text_color=ORANGE).pack(padx=12, pady=10)
            else:
                ctk.CTkLabel(self._e_limits, text="Aguardando Cursor…",
                             font=("Segoe UI", 9), text_color=MUTED).pack(padx=12, pady=10)

            ts = self.cursor_poller.fetched_at or self.cursor_loader.updated_at
            if ts:
                self._e_api_ts.configure(text=ts.strftime("Cursor %H:%M:%S"))

        if hasattr(self, "_c_today"):
            self._fill_cursor_card(self._c_today, self.cursor_loader.today)
            self._fill_cursor_card(self._c_alltime, self.cursor_loader.alltime)
        if self.cursor_loader.updated_at and hasattr(self, "_e_time"):
            self._e_time.configure(
                text=self.cursor_loader.updated_at.strftime("%H:%M:%S"))

        if hasattr(self, "_e_proj"):
            self._fill_cursor_projects()

    def _update_copilot_expanded(self):
        if hasattr(self, "_e_limits"):
            for w in self._e_limits.winfo_children():
                w.destroy()

            err = self.copilot_loader.error
            if err:
                ctk.CTkLabel(self._e_limits, text=f"⚠ {err}",
                             font=("Segoe UI", 9), text_color=ORANGE).pack(padx=12, pady=10)
            else:
                wrap = ctk.CTkFrame(self._e_limits, fg_color="transparent")
                wrap.pack(fill="x", padx=12, pady=10)

                ctk.CTkLabel(
                    wrap,
                    text=f"{self.copilot_loader.source_label} (histórico local)",
                    font=("Segoe UI", 10, "bold"),
                    text_color=TXT,
                    anchor="w",
                ).pack(fill="x")
                ctk.CTkLabel(
                    wrap,
                    text=_shorten_home_path(self.copilot_loader.source_path),
                    font=("Consolas", 8),
                    text_color=MUTED,
                    anchor="w",
                    justify="left",
                    wraplength=350,
                ).pack(fill="x", pady=(2, 6))

                stats = ctk.CTkFrame(wrap, fg_color="transparent")
                stats.pack(fill="x")
                ctk.CTkLabel(
                    stats,
                    text=f"Hoje: {self.copilot_loader.today.sessions} sess · {self.copilot_loader.today.completions} turns",
                    font=("Segoe UI", 9),
                    text_color=BLUE,
                    anchor="w",
                ).pack(side="left")
                ctk.CTkLabel(
                    stats,
                    text=f"Total: {self.copilot_loader.alltime.sessions} sess · {self.copilot_loader.alltime.completions} turns",
                    font=("Segoe UI", 9),
                    text_color=GREEN,
                    anchor="e",
                ).pack(side="right")

            if self.copilot_loader.updated_at:
                self._e_api_ts.configure(
                    text=self.copilot_loader.updated_at.strftime("Copilot %H:%M:%S"))

        if hasattr(self, "_c_today"):
            self._fill_copilot_card(self._c_today, self.copilot_loader.today)
            self._fill_copilot_card(self._c_alltime, self.copilot_loader.alltime)
        if self.copilot_loader.updated_at and hasattr(self, "_e_time"):
            self._e_time.configure(
                text=self.copilot_loader.updated_at.strftime("%H:%M:%S"))

        if hasattr(self, "_e_proj"):
            self._fill_copilot_projects()

    def _render_bar(self, parent, lim: dict, source=None):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(8, 0))

        pct   = lim["pct"]
        color = bar_color(pct)
        source = source or self.poller
        mins  = source.mins_to_reset(lim)

        # label row
        top = ctk.CTkFrame(row, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkLabel(top, text=lim["label"],
                     font=("Segoe UI", 9, "bold"), text_color=TXT).pack(side="left")
        ctk.CTkLabel(top, text=f"{pct:.0f}%",
                     font=("Segoe UI", 9, "bold"), text_color=color).pack(side="right")

        # bar
        b = ctk.CTkProgressBar(row, height=6, corner_radius=0,
                               fg_color=BORDER, progress_color=color)
        b.set(pct / 100)
        b.pack(fill="x", pady=(3, 2))

        # meta row
        meta = ctk.CTkFrame(row, fg_color="transparent")
        meta.pack(fill="x", pady=(0, 2))

        if lim["kind"] == "credits" and lim["cap_usd"] > 0:
            ctk.CTkLabel(meta,
                         text=f"{fmt_cost(lim['used_usd'])} de {fmt_cost(lim['cap_usd'])}",
                         font=("Segoe UI", 8), text_color=MUTED).pack(side="left")

        if mins > 0:
            rc = GREEN if mins > 60 else ORANGE
            ctk.CTkLabel(meta, text=f"reset {fmt_dur(mins)}",
                         font=("Segoe UI", 8), text_color=rc).pack(side="right")

    def _fill_card(self, card, s: Stats):
        card._tok.configure(text=fmt_tok(s.total))
        card._cost.configure(text=fmt_cost(s.cost))
        card._in.configure(text=f"↑ {fmt_tok(s.inp)}")
        card._out.configure(text=f"↓ {fmt_tok(s.out)}")
        card._cr.configure(text=f"⚡ {fmt_tok(s.cr)}")

    def _fill_codex_card(self, card, s: CodexStats):
        card._tok.configure(text=fmt_tok(s.total))
        card._cost.configure(text=f"ctx {fmt_tok(s.cached)}")
        card._in.configure(text=f"↑ {fmt_tok(s.inp)}")
        card._out.configure(text=f"↓ {fmt_tok(s.out)}")
        card._cr.configure(text=f"⚡ {fmt_tok(s.reasoning)}")

    def _fill_cursor_card(self, card, s: CursorStats):
        card._tok.configure(text=fmt_tok(s.total))
        card._cost.configure(text=fmt_cost(s.cost_cents / 100))
        card._in.configure(text=f"↑ {fmt_tok(s.inp)}")
        card._out.configure(text=f"↓ {fmt_tok(s.out)}")
        card._cr.configure(text=f"ctx {fmt_tok(s.cached)}")

    def _fill_copilot_card(self, card, s: CopilotStats):
        card._tok.configure(text=fmt_tok(s.completions))
        card._cost.configure(text=f"{s.sessions} sess")
        card._in.configure(text="")
        card._out.configure(text="")
        card._cr.configure(text="")

    def _fill_codex_projects(self):
        for w in self._e_proj.winfo_children():
            w.destroy()

        items = list(self.codex_loader.projects.items())[:12]
        if not items:
            ctk.CTkLabel(self._e_proj, text="Sem dados ainda.",
                         text_color=MUTED, font=("Segoe UI", 10)).pack(pady=16)
            return

        for name, s in items:
            row = ctk.CTkFrame(self._e_proj, fg_color=CARD, corner_radius=0)
            row.pack(fill="x", pady=2)

            left = ctk.CTkFrame(row, fg_color="transparent")
            left.pack(side="left", padx=10, pady=6, fill="x", expand=True)
            ctk.CTkLabel(left, text=name[:30], font=("Segoe UI", 10, "bold"),
                         text_color=TXT, anchor="w").pack(anchor="w")
            ctk.CTkLabel(left,
                         text=f"↑{fmt_tok(s.inp)}  ↓{fmt_tok(s.out)}  ctx {fmt_tok(s.cached)}",
                         font=("Segoe UI", 8), text_color=MUTED, anchor="w").pack(anchor="w")

            right = ctk.CTkFrame(row, fg_color="transparent")
            right.pack(side="right", padx=10)
            ctk.CTkLabel(right, text=fmt_tok(s.total),
                         font=("Segoe UI", 10, "bold"), text_color=BLUE).pack(anchor="e")
            ctk.CTkLabel(right, text=f"r {fmt_tok(s.reasoning)}",
                         font=("Segoe UI", 8), text_color=GREEN).pack(anchor="e")

    def _fill_cursor_projects(self):
        for w in self._e_proj.winfo_children():
            w.destroy()

        items = list(self.cursor_loader.projects.items())[:12]
        if not items:
            ctk.CTkLabel(self._e_proj, text="Sem dados ainda.",
                         text_color=MUTED, font=("Segoe UI", 10)).pack(pady=16)
            return

        for name, s in items:
            row = ctk.CTkFrame(self._e_proj, fg_color=CARD, corner_radius=0)
            row.pack(fill="x", pady=2)

            left = ctk.CTkFrame(row, fg_color="transparent")
            left.pack(side="left", padx=10, pady=6, fill="x", expand=True)
            ctk.CTkLabel(left, text=name[:30], font=("Segoe UI", 10, "bold"),
                         text_color=TXT, anchor="w").pack(anchor="w")
            ctk.CTkLabel(left, text="respostas do agente",
                         font=("Segoe UI", 8), text_color=MUTED, anchor="w").pack(anchor="w")

            right = ctk.CTkFrame(row, fg_color="transparent")
            right.pack(side="right", padx=10)
            ctk.CTkLabel(right, text=str(s.messages),
                         font=("Segoe UI", 10, "bold"), text_color=BLUE).pack(anchor="e")
            ctk.CTkLabel(right, text="msgs",
                         font=("Segoe UI", 8), text_color=GREEN).pack(anchor="e")

    def _fill_copilot_projects(self):
        for w in self._e_proj.winfo_children():
            w.destroy()

        items = list(self.copilot_loader.projects.items())[:12]
        if not items:
            ctk.CTkLabel(self._e_proj, text="Sem dados ainda.",
                         text_color=MUTED, font=("Segoe UI", 10)).pack(pady=16)
            return

        for name, s in items:
            row = ctk.CTkFrame(self._e_proj, fg_color=CARD, corner_radius=0)
            row.pack(fill="x", pady=2)

            left = ctk.CTkFrame(row, fg_color="transparent")
            left.pack(side="left", padx=10, pady=6, fill="x", expand=True)
            ctk.CTkLabel(left, text=name[:30], font=("Segoe UI", 10, "bold"),
                         text_color=TXT, anchor="w").pack(anchor="w")
            ctk.CTkLabel(left, text=f"{s.sessions} sess · {s.completions} turns",
                         font=("Segoe UI", 8), text_color=MUTED, anchor="w").pack(anchor="w")

            right = ctk.CTkFrame(row, fg_color="transparent")
            right.pack(side="right", padx=10)
            ctk.CTkLabel(right, text=fmt_tok(s.completions),
                         font=("Segoe UI", 10, "bold"), text_color=BLUE).pack(anchor="e")
            ctk.CTkLabel(right, text="turns",
                         font=("Segoe UI", 8), text_color=GREEN).pack(anchor="e")

    def _manual_refresh(self):
        threading.Thread(target=self._bg_refresh, daemon=True).start()

    def _bg_refresh(self):
        self.loader.reload()
        self.codex_loader.reload()
        self.cursor_loader.reload()
        self.copilot_loader.reload()
        self.poller.fetch_now()
        self.cursor_poller.fetch_now()

    # ── tray entry point ───────────────────────────────────────────────────────
    def show_expanded(self):
        self._mode = self.EXPANDED
        self._build()
        self.deiconify(); self.lift()
        self._update_ui()


# ════════════════════════════════════════════════════════════════════════════════
#  Tray icon image
# ════════════════════════════════════════════════════════════════════════════════
def _make_icon() -> Image.Image:
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d    = ImageDraw.Draw(img)
    d.ellipse([2, 2, 62, 62], fill="#58a6ff")
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
    loader = Loader()
    codex_loader = CodexLoader()
    cursor_loader = CursorLoader()
    copilot_loader = CopilotLoader()
    poller = ApiPoller()
    cursor_poller = CursorApiPoller(cursor_loader)
    app = MonitorWindow(loader, poller, codex_loader, cursor_loader, cursor_poller, copilot_loader)

    def open_expanded(icon, _):
        app.after(0, app.show_expanded)

    def quit_app(icon, _):
        icon.stop()
        app.after(0, app.quit)

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
            loader.reload()
            codex_loader.reload()
            cursor_loader.reload()
            copilot_loader.reload()
            app.after(0, app._update_ui)
            time.sleep(POLL_JSONL_SEC)
    threading.Thread(target=_jsonl_loop, daemon=True).start()

    # Initial JSONL load before first render
    threading.Thread(
        target=lambda: (
            loader.reload(),
            codex_loader.reload(),
            cursor_loader.reload(),
            copilot_loader.reload(),
            cursor_poller.fetch_now(),
            app.after(0, app._update_ui),
        ),
        daemon=True,
    ).start()

    # Start in minimal pill mode (always visible in corner)
    app.deiconify()
    app._update_ui()
    app.mainloop()
    tray.stop()


if __name__ == "__main__":
    main()
