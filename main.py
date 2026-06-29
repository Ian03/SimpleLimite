#!/usr/bin/env python3
"""
Claude Token Monitor
Minimal pill + expanded detail view.
Session limits via Anthropic OAuth API; project costs via local JSONL.
"""

import json
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

# ── Intervals ─────────────────────────────────────────────────────────────────
POLL_API_SEC  = 60
POLL_JSONL_SEC = 30

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
    except Exception:
        pass
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


def _readable_name(folder: str) -> str:
    parts = folder.replace("--", "\x00").split("\x00")
    return parts[-1].replace("-", " ").strip() if parts else folder


class Loader:
    def __init__(self):
        self.today    = Stats()
        self.alltime  = Stats()
        self.projects: dict[str, Stats] = {}
        self.updated_at: "datetime | None" = None
        self._lock = threading.Lock()

    def reload(self):
        today_date = datetime.now(timezone.utc).date()
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
                                        if ts.date() == today_date:
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
            self.today    = today
            self.alltime  = alltime
            self.projects = projects
            self.updated_at = datetime.now()


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
            self.limits     = limits
            self.error      = err
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

    def __init__(self, loader: Loader, poller: ApiPoller):
        super().__init__()
        self.loader = loader
        self.poller = poller
        self._mode  = self.MINIMAL
        self._dx = self._dy = self._wx = self._wy = 0

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.97)
        self.attributes("-toolwindow", True)
        self.configure(fg_color=BG)

        self.poller.on_update(lambda: self.after(0, self._update_ui))
        self._build()

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
        t = ctk.CTkLabel(hdr, text="Claude Tokens",
                         font=("Segoe UI", 12, "bold"), text_color=TXT)
        t.place(x=26, rely=0.5, anchor="w")
        self._bind_drag(t)

        self._e_time = ctk.CTkLabel(hdr, text="", font=("Segoe UI", 9),
                                    text_color=MUTED)
        self._e_time.pack(side="right", padx=(0, 4))
        _btn(hdr, "⊟", self._go_minimal,       MUTED).pack(side="right", padx=(0, 2))
        _btn(hdr, "↻", self._manual_refresh,   BLUE).pack(side="right")

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

    # ── mode switches ──────────────────────────────────────────────────────────
    def _go_minimal(self):
        self._mode = self.MINIMAL
        self._build()
        self._update_ui()

    def _go_expanded(self):
        self._mode = self.EXPANDED
        self._build()
        self.deiconify(); self.lift(); self.focus_force()
        self._update_ui()

    # ── update dispatch ────────────────────────────────────────────────────────
    def _update_ui(self):
        if self._mode == self.MINIMAL:
            self._update_minimal()
        else:
            self._update_expanded()

    # ── MINIMAL update ─────────────────────────────────────────────────────────
    def _update_minimal(self):
        top = self.poller.top
        if top is None:
            self._m_pct.configure(text="—", text_color=MUTED)
            self._m_bar.set(0)
            self._m_lbl.configure(text="Sem dados")
            self._m_reset.configure(text="")
            return

        pct   = top["pct"]
        color = bar_color(pct)
        self._m_dot.configure(fg_color=color)
        self._m_lbl.configure(text=top["label"])
        self._m_pct.configure(text=f"{pct:.0f}%", text_color=color)
        self._m_bar.configure(progress_color=color)
        self._m_bar.set(pct / 100)

        mins = self.poller.mins_to_reset(top)
        if mins > 0:
            rc = GREEN if mins > 60 else ORANGE
            self._m_reset.configure(text=f"reset {fmt_dur(mins)}", text_color=rc)
        else:
            self._m_reset.configure(text="")

    # ── EXPANDED update ────────────────────────────────────────────────────────
    def _update_expanded(self):
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

    def _render_bar(self, parent, lim: dict):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(8, 0))

        pct   = lim["pct"]
        color = bar_color(pct)
        mins  = self.poller.mins_to_reset(lim)

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

    def _fill_projects(self):
        for w in self._e_proj.winfo_children():
            w.destroy()

        items = list(self.loader.projects.items())[:12]
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
                         text=f"↑{fmt_tok(s.inp)}  ↓{fmt_tok(s.out)}  ⚡{fmt_tok(s.cr)}",
                         font=("Segoe UI", 8), text_color=MUTED, anchor="w").pack(anchor="w")

            right = ctk.CTkFrame(row, fg_color="transparent")
            right.pack(side="right", padx=10)
            ctk.CTkLabel(right, text=fmt_tok(s.total),
                         font=("Segoe UI", 10, "bold"), text_color=BLUE).pack(anchor="e")
            ctk.CTkLabel(right, text=fmt_cost(s.cost),
                         font=("Segoe UI", 8), text_color=GREEN).pack(anchor="e")

    # ── manual refresh ─────────────────────────────────────────────────────────
    def _manual_refresh(self):
        threading.Thread(target=self._bg_refresh, daemon=True).start()

    def _bg_refresh(self):
        self.loader.reload()
        self.poller.fetch_now()

    # ── tray entry point ───────────────────────────────────────────────────────
    def show_expanded(self):
        self._mode = self.EXPANDED
        self._build()
        self.deiconify(); self.lift(); self.focus_force()
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
    poller = ApiPoller()
    app    = MonitorWindow(loader, poller)

    def open_expanded(icon, _):
        app.after(0, app.show_expanded)

    def quit_app(icon, _):
        icon.stop()
        app.after(0, app.destroy)

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

    # Background: JSONL refresh loop
    def _jsonl_loop():
        while True:
            loader.reload()
            app.after(0, app._update_ui)
            time.sleep(POLL_JSONL_SEC)
    threading.Thread(target=_jsonl_loop, daemon=True).start()

    # Initial JSONL load before first render
    threading.Thread(
        target=lambda: (loader.reload(), app.after(0, app._update_ui)),
        daemon=True,
    ).start()

    # Start in minimal pill mode (always visible in corner)
    app.deiconify()
    app._update_ui()
    app.mainloop()
    tray.stop()


if __name__ == "__main__":
    main()
