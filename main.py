#!/usr/bin/env python3
"""
Claude Token Monitor
System tray app for Windows that tracks Claude Code token usage.
Reads ~/.claude/projects/*.jsonl — zero API calls, purely local.
"""

import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont
import pystray
from pystray import MenuItem as item

# ──────────────────────────────────────────────
#  Config
# ──────────────────────────────────────────────
CLAUDE_DIR    = Path.home() / ".claude"
PROJECTS_DIR  = CLAUDE_DIR / "projects"
CONFIG_FILE   = Path(__file__).parent / "config.json"
REFRESH_SEC   = 30
WIN_W, WIN_H  = 390, 580

# ── Limites e alertas ──────────────────────────
ALERT_PCT        = 0.70   # notifica ao atingir 70 %
RESET_INTERVAL_H = 5      # janela de reset em horas (Pro = 5 h)
RESET_WARN_MINS  = 60     # avisa N minutos antes do reset

# Limite padrão — substituído pelo valor calibrado em config.json
_DEFAULT_LIMIT   = 5_000_000


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(data: dict):
    cfg = _load_config()
    cfg.update(data)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def get_session_limit() -> int:
    return _load_config().get("session_limit", _DEFAULT_LIMIT)

# USD per 1 M tokens
PRICING = {
    "claude-opus-4-8":           {"i": 15.00, "o": 75.00, "cc": 18.75, "cr": 1.50},
    "claude-sonnet-4-6":         {"i":  3.00, "o": 15.00, "cc":  3.75, "cr": 0.30},
    "claude-haiku-4-5":          {"i":  0.80, "o":  4.00, "cc":  1.00, "cr": 0.08},
    "default":                   {"i":  3.00, "o": 15.00, "cc":  3.75, "cr": 0.30},
}

# UI palette (GitHub dark style)
BG      = "#0d1117"
CARD    = "#161b22"
BORDER  = "#30363d"
TXT     = "#e6edf3"
MUTED   = "#8b949e"
BLUE    = "#58a6ff"
GREEN   = "#3fb950"
ORANGE  = "#f0883e"
PURPLE  = "#bc8cff"


# ──────────────────────────────────────────────
#  Data layer
# ──────────────────────────────────────────────
def _pricing(model: str) -> dict:
    for k, v in PRICING.items():
        if k in model:
            return v
    return PRICING["default"]


def fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def fmt_cost(usd: float) -> str:
    if usd == 0:
        return "$0.00"
    if usd < 0.001:
        return f"< $0.001"
    if usd < 1:
        return f"${usd:.4f}"
    return f"${usd:.2f}"


class Stats:
    __slots__ = ("inp", "out", "cc", "cr", "cost")

    def __init__(self):
        self.inp = self.out = self.cc = self.cr = 0
        self.cost = 0.0

    def add(self, usage: dict, model: str):
        p  = _pricing(model)
        i  = usage.get("input_tokens", 0)
        o  = usage.get("output_tokens", 0)
        cc = usage.get("cache_creation_input_tokens", 0)
        cr = usage.get("cache_read_input_tokens", 0)
        self.inp  += i;  self.out += o
        self.cc   += cc; self.cr  += cr
        self.cost += (i*p["i"] + o*p["o"] + cc*p["cc"] + cr*p["cr"]) / 1_000_000

    @property
    def total(self):
        return self.inp + self.out


def _readable_name(folder: str) -> str:
    """'D--Deverlop-Apps-SimpleLimite' -> 'SimpleLimite'"""
    parts = folder.replace("--", "\x00").split("\x00")
    return parts[-1].replace("-", " ").strip() if parts else folder


class Loader:
    def __init__(self):
        self.today    = Stats()
        self.alltime  = Stats()
        self.projects: dict[str, Stats] = {}
        self.updated_at: datetime | None = None
        self._lock = threading.Lock()

    def reload(self):
        today_date = datetime.now(timezone.utc).date()
        today   = Stats()
        alltime = Stats()
        projects: dict[str, Stats] = {}

        if not PROJECTS_DIR.exists():
            with self._lock:
                self.today = today; self.alltime = alltime
                self.projects = projects; self.updated_at = datetime.now()
            return

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
                                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                                    if ts.date() == today_date:
                                        today.add(usage, model)
                                except Exception:
                                    pass
                except Exception:
                    continue

            if proj.total > 0:
                projects[name] = proj

        projects = dict(sorted(projects.items(), key=lambda x: x[1].total, reverse=True))

        with self._lock:
            self.today   = today
            self.alltime = alltime
            self.projects = projects
            self.updated_at = datetime.now()


# ──────────────────────────────────────────────
#  Session watcher — alertas de limite e reset
# ──────────────────────────────────────────────
class SessionWatcher:
    """
    Roda em background a cada 60 s.
    - Soma tokens da sessão ativa no JSONL mais recente.
    - Dispara notificação pystray ao atingir 70 % do limite de contexto.
    - Dispara notificação quando o reset de uso está próximo.
    """

    def __init__(self):
        self._tray: pystray.Icon | None = None
        self._notified_sessions: set[str] = set()   # session ids já notificados (70 %)
        self._notified_reset_key: str = ""           # chave do reset já notificado
        # dados expostos para a UI
        self.session_id: str = ""
        self.session_tokens: int = 0
        self.session_pct: float = 0.0
        self.next_reset: datetime | None = None
        self.mins_to_reset: int = 0

    def attach(self, tray: pystray.Icon):
        self._tray = tray

    # ── leitura da janela de uso atual ───────
    def _active_session(self) -> tuple[str, int, datetime | None]:
        """
        Retorna ("", tokens_na_janela, window_start).
        Conta input+output (sem cache_read) de TODOS os projetos
        nas últimas RESET_INTERVAL_H horas — espelha a janela de rate limit.
        """
        now          = datetime.now(tz=timezone.utc)
        window_start = now - timedelta(hours=RESET_INTERVAL_H)
        tokens       = self._count_window_tokens(since=window_start)
        return "", tokens, window_start

    def _count_window_tokens(self, since: datetime) -> int:
        """
        Soma input_tokens + output_tokens + cache_creation de todos os
        JSONLs em PROJECTS_DIR com timestamp >= since.
        cache_read_input_tokens é omitido: não contabiliza no rate limit.
        """
        total = 0
        if not PROJECTS_DIR.exists():
            return 0
        for proj_dir in PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
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
                            ts_raw = d.get("timestamp", "")
                            if not ts_raw:
                                continue
                            try:
                                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                                if ts < since:
                                    continue
                            except Exception:
                                continue
                            u = d.get("message", {}).get("usage", {})
                            if u:
                                total += (u.get("input_tokens", 0)
                                          + u.get("output_tokens", 0)
                                          + u.get("cache_creation_input_tokens", 0))
                except Exception:
                    continue
        return total

    # ── cálculo do próximo reset ──────────────
    def _calc_next_reset(self, _unused: datetime | None = None) -> datetime:
        """
        Usa o anchor salvo em config.json (calibrado pelo usuário).
        O anchor é o instante exato do último reset conhecido.
        A partir dele extrapolamos: next = anchor + n * RESET_INTERVAL_H
        até encontrar um instante no futuro.
        Sem anchor: fallback para fronteira de hora fixa.
        """
        now    = datetime.now(tz=timezone.utc)
        anchor = _load_config().get("reset_anchor")
        if anchor:
            try:
                ref = datetime.fromisoformat(anchor)
                if ref.tzinfo is None:
                    ref = ref.replace(tzinfo=timezone.utc)
                interval = timedelta(hours=RESET_INTERVAL_H)
                # avança o anchor até ser > now
                while ref <= now:
                    ref += interval
                return ref
            except Exception:
                pass
        # fallback: próxima fronteira UTC múltipla de RESET_INTERVAL_H
        slot = (now.hour // RESET_INTERVAL_H + 1) * RESET_INTERVAL_H
        return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=slot)

    # ── notificações ─────────────────────────
    def _notify(self, title: str, msg: str):
        if self._tray:
            try:
                self._tray.notify(title, msg)
            except Exception:
                pass

    # ── tick principal ────────────────────────
    def tick(self):
        sid, tokens, started = self._active_session()
        limit = get_session_limit()
        self.session_id     = sid
        self.session_tokens = tokens
        self.session_pct    = tokens / limit if limit else 0

        # ── alerta 70 % ──────────────────────
        if self.session_pct >= ALERT_PCT and "70pct" not in self._notified_sessions:
            self._notified_sessions.add("70pct")
            self._notify(
                f"⚠️  Limite de uso em {self.session_pct*100:.0f}%",
                f"{fmt_tok(tokens)} / {fmt_tok(limit)} tokens usados.\n"
                "Considere iniciar uma nova sessão em breve."
            )

        # ── alerta de reset ───────────────────
        self.next_reset   = self._calc_next_reset(started)
        delta             = self.next_reset - datetime.now(tz=timezone.utc)
        self.mins_to_reset = max(0, int(delta.total_seconds() / 60))
        reset_key         = self.next_reset.strftime("%Y-%m-%dT%H")

        if 0 < self.mins_to_reset <= RESET_WARN_MINS and reset_key != self._notified_reset_key:
            self._notified_reset_key = reset_key
            h = self.mins_to_reset // 60
            m = self.mins_to_reset % 60
            tempo = f"{h}h {m}min" if h else f"{m} minutos"
            self._notify(
                "🔄  Reset de limites próximo",
                f"Falta apenas {tempo} para resetar seus limites de uso."
            )

        # ── tooltip do ícone na taskbar ───────
        self._update_tray_tooltip()

    def _update_tray_tooltip(self):
        if not self._tray:
            return
        limit     = get_session_limit()
        real_pct  = (self.session_tokens / limit * 100) if limit else 0
        h = self.mins_to_reset // 60
        m = self.mins_to_reset % 60
        reset_str = f"Reset em {h}h {m:02d}min" if h else f"Reset em {m}min" if m else "Reset em breve"
        try:
            self._tray.title = (
                f"Claude Token Monitor\n"
                f"Sessão: {real_pct:.0f}%  ({fmt_tok(self.session_tokens)} tokens)\n"
                f"{reset_str}"
            )
        except Exception:
            pass

    def start(self):
        def _loop():
            while True:
                try:
                    self.tick()
                except Exception:
                    pass
                time.sleep(60)
        threading.Thread(target=_loop, daemon=True).start()


# ──────────────────────────────────────────────
#  UI
# ──────────────────────────────────────────────
class MonitorWindow(ctk.CTk):
    def __init__(self, loader: Loader, watcher: "SessionWatcher"):
        super().__init__()
        self.loader  = loader
        self.watcher = watcher

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.97)
        self.configure(fg_color=BG)
        self.geometry(f"{WIN_W}x{WIN_H}")
        self._place_window()
        self._dragging = False
        self._drag_x = self._drag_y = 0

        self._build()
        # Clicking outside closes the window
        self.bind("<FocusOut>", lambda e: self.after(200, self._check_focus))

    def _check_focus(self):
        try:
            if self.focus_get() is None:
                self.hide()
        except Exception:
            pass

    def _place_window(self):
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x  = sw - WIN_W - 14
        y  = sh - WIN_H - 54
        self.geometry(f"{WIN_W}x{WIN_H}+{x}+{y}")

    # ── drag support ──────────────────────────
    def _on_drag_start(self, e):
        self._drag_x, self._drag_y = e.x_root, e.y_root
        self._wx = self.winfo_x(); self._wy = self.winfo_y()

    def _on_drag(self, e):
        dx = e.x_root - self._drag_x; dy = e.y_root - self._drag_y
        self.geometry(f"+{self._wx+dx}+{self._wy+dy}")

    # ── build UI ─────────────────────────────
    def _build(self):
        # ─ header ─
        hdr = ctk.CTkFrame(self, fg_color=CARD, corner_radius=12, height=52)
        hdr.pack(fill="x", padx=10, pady=(10, 5))
        hdr.pack_propagate(False)
        hdr.bind("<ButtonPress-1>",   self._on_drag_start)
        hdr.bind("<B1-Motion>",       self._on_drag)

        dot = ctk.CTkFrame(hdr, width=8, height=8, fg_color=BLUE, corner_radius=4)
        dot.place(x=14, rely=0.5, anchor="w")

        title = ctk.CTkLabel(hdr, text="Claude Tokens", font=("Segoe UI", 13, "bold"),
                             text_color=TXT)
        title.place(x=30, rely=0.5, anchor="w")
        title.bind("<ButtonPress-1>", self._on_drag_start)
        title.bind("<B1-Motion>",     self._on_drag)

        self._lbl_time = ctk.CTkLabel(hdr, text="", font=("Segoe UI", 9),
                                      text_color=MUTED)
        self._lbl_time.pack(side="right", padx=(0, 4))

        _btn(hdr, "✕", self.hide,       MUTED).pack(side="right", padx=(0, 4))
        _btn(hdr, "↻", self.refresh,    BLUE).pack(side="right")
        _btn(hdr, "⚙", self._calibrate, MUTED).pack(side="right")

        # ─ today card ─
        self._card_today   = self._stat_card("Hoje",  BLUE)
        self._card_alltime = self._stat_card("Total", GREEN)

        # ─ sessão atual ─
        self._session_card = self._session_bar()

        # ─ project section ─
        ph = ctk.CTkFrame(self, fg_color="transparent")
        ph.pack(fill="x", padx=12, pady=(6, 2))
        ctk.CTkLabel(ph, text="PROJETOS", font=("Segoe UI", 9, "bold"),
                     text_color=MUTED).pack(side="left")

        self._proj_frame = ctk.CTkScrollableFrame(
            self, fg_color=BG,
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=MUTED,
            corner_radius=0
        )
        self._proj_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _session_bar(self) -> ctk.CTkFrame:
        card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=12)
        card.pack(fill="x", padx=10, pady=3)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(10, 4))

        ctk.CTkLabel(top, text="SESSÃO ATUAL", font=("Segoe UI", 9, "bold"),
                     text_color=MUTED).pack(side="left")

        card._lbl_pct = ctk.CTkLabel(top, text="0%", font=("Segoe UI", 9, "bold"),
                                     text_color=BLUE)
        card._lbl_pct.pack(side="right")

        card._bar = ctk.CTkProgressBar(card, height=6, corner_radius=3,
                                       fg_color=BORDER, progress_color=BLUE)
        card._bar.set(0)
        card._bar.pack(fill="x", padx=14, pady=(0, 6))

        bottom = ctk.CTkFrame(card, fg_color="transparent")
        bottom.pack(fill="x", padx=14, pady=(0, 10))

        card._lbl_tokens = ctk.CTkLabel(bottom, text="0 / 200K tokens",
                                        font=("Segoe UI", 9), text_color=MUTED)
        card._lbl_tokens.pack(side="left")

        card._lbl_reset = ctk.CTkLabel(bottom, text="", font=("Segoe UI", 9),
                                       text_color=GREEN)
        card._lbl_reset.pack(side="right")

        return card

    def _calibrate(self):
        """Diálogo para calibrar limite de tokens e tempo de reset."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("Calibrar")
        dlg.geometry("360x340")
        dlg.configure(fg_color=BG)
        dlg.attributes("-topmost", True)
        dlg.grab_set()
        dlg.geometry(f"+{self.winfo_x()+20}+{self.winfo_y()+80}")

        def _section(text):
            f = ctk.CTkFrame(dlg, fg_color=CARD, corner_radius=10)
            f.pack(fill="x", padx=14, pady=(10, 0))
            ctk.CTkLabel(f, text=text, font=("Segoe UI", 10, "bold"),
                         text_color=MUTED).pack(anchor="w", padx=12, pady=(8, 2))
            return f

        # ── seção 1: limite de tokens ─────────
        sec1 = _section("LIMITE DE USO  (% da página do Claude Code)")
        row1 = ctk.CTkFrame(sec1, fg_color="transparent")
        row1.pack(fill="x", padx=12, pady=(0, 10))
        entry_pct = ctk.CTkEntry(row1, width=90, placeholder_text="ex: 28",
                                 font=("Segoe UI", 13), justify="center")
        entry_pct.pack(side="left")
        ctk.CTkLabel(row1, text="%  →  calcula o limite real",
                     font=("Segoe UI", 10), text_color=MUTED).pack(side="left", padx=8)

        # ── seção 2: tempo de reset ───────────
        sec2 = _section("TEMPO ATÉ RESET  (mostrado na página)")
        row2 = ctk.CTkFrame(sec2, fg_color="transparent")
        row2.pack(fill="x", padx=12, pady=(0, 10))

        entry_h = ctk.CTkEntry(row2, width=52, placeholder_text="h",
                               font=("Segoe UI", 13), justify="center")
        entry_h.pack(side="left")
        ctk.CTkLabel(row2, text="h", font=("Segoe UI", 12), text_color=MUTED).pack(side="left", padx=(2,8))
        entry_m = ctk.CTkEntry(row2, width=52, placeholder_text="min",
                               font=("Segoe UI", 13), justify="center")
        entry_m.pack(side="left")
        ctk.CTkLabel(row2, text="min  →  ancora o reset",
                     font=("Segoe UI", 10), text_color=MUTED).pack(side="left", padx=8)

        # ── feedback ─────────────────────────
        lbl_result = ctk.CTkLabel(dlg, text="", font=("Segoe UI", 10), text_color=MUTED)
        lbl_result.pack(pady=6)

        def apply():
            saved = []
            errors = []

            # calibrar limite
            pct_raw = entry_pct.get().strip().replace("%", "").replace(",", ".")
            if pct_raw:
                try:
                    real_pct = float(pct_raw)
                    if not (0 < real_pct <= 100):
                        raise ValueError
                    tok = self.watcher.session_tokens
                    if tok == 0:
                        errors.append("Nenhum token detectado ainda.")
                    else:
                        new_limit = int(tok / (real_pct / 100))
                        _save_config({"session_limit": new_limit})
                        saved.append(f"Limite: {fmt_tok(new_limit)}")
                except ValueError:
                    errors.append("% inválido — informe um valor entre 1 e 100.")

            # calibrar reset
            h_raw = entry_h.get().strip()
            m_raw = entry_m.get().strip()
            if h_raw or m_raw:
                try:
                    h_val = int(h_raw) if h_raw else 0
                    m_val = int(m_raw) if m_raw else 0
                    total_mins = h_val * 60 + m_val
                    if total_mins <= 0:
                        raise ValueError
                    anchor = datetime.now(tz=timezone.utc) + timedelta(minutes=total_mins)
                    _save_config({"reset_anchor": anchor.isoformat()})
                    saved.append(f"Reset em {h_val}h {m_val:02d}min")
                except ValueError:
                    errors.append("Tempo de reset inválido.")

            if errors:
                lbl_result.configure(text=" | ".join(errors), text_color=ORANGE)
            elif saved:
                lbl_result.configure(text="Salvo: " + "  •  ".join(saved), text_color=GREEN)
                self.after(1400, dlg.destroy)
                self.after(1400, self._update_session_bar)
            else:
                lbl_result.configure(text="Preencha ao menos um campo.", text_color=MUTED)

        def reset_limit():
            cfg = _load_config()
            cfg.pop("session_limit", None)
            CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            lbl_result.configure(text="Limite resetado para o padrão.", text_color=MUTED)
            self.after(1400, dlg.destroy)
            self.after(1400, self._update_session_bar)

        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(pady=8)
        ctk.CTkButton(btn_row, text="Calibrar", command=apply,
                      fg_color=BLUE, hover_color="#4080cc",
                      font=("Segoe UI", 12, "bold"), width=130).pack(side="left", padx=6)
        ctk.CTkButton(btn_row, text="Resetar limite", command=reset_limit,
                      fg_color="#30363d", hover_color="#484f58",
                      font=("Segoe UI", 11), width=120).pack(side="left", padx=6)

    def _update_session_bar(self):
        w = self.watcher
        limit = get_session_limit()
        pct   = min(w.session_tokens / limit if limit else 0, 1.0)
        used = w.session_tokens

        # cor muda conforme % de uso
        used  = w.session_tokens
        real_pct = w.session_tokens / limit if limit else 0

        if real_pct >= 0.90:
            color = "#f85149"
        elif real_pct >= ALERT_PCT:
            color = ORANGE
        else:
            color = BLUE

        self._session_card._bar.configure(progress_color=color)
        self._session_card._bar.set(pct)
        self._session_card._lbl_pct.configure(
            text=f"{real_pct*100:.0f}%", text_color=color)
        self._session_card._lbl_tokens.configure(
            text=f"{fmt_tok(used)} / {fmt_tok(limit)} tokens")

        if w.mins_to_reset > 0:
            h = w.mins_to_reset // 60
            m = w.mins_to_reset % 60
            tempo = f"{h}h {m:02d}min" if h else f"{m}min"
            self._session_card._lbl_reset.configure(
                text=f"🔄 reset em {tempo}",
                text_color=GREEN if w.mins_to_reset > RESET_WARN_MINS else ORANGE)
        else:
            self._session_card._lbl_reset.configure(text="")

    def _stat_card(self, label: str, accent: str) -> ctk.CTkFrame:
        card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=12)
        card.pack(fill="x", padx=10, pady=3)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(10, 2))

        ctk.CTkLabel(top, text=label, font=("Segoe UI", 9, "bold"),
                     text_color=MUTED).pack(side="left")

        lbl_cost = ctk.CTkLabel(top, text="$0.00", font=("Segoe UI", 11, "bold"),
                                text_color=accent)
        lbl_cost.pack(side="right")
        card._cost = lbl_cost

        lbl_tok = ctk.CTkLabel(card, text="0", font=("Segoe UI", 24, "bold"),
                               text_color=TXT)
        lbl_tok.pack(anchor="w", padx=14)
        card._tok = lbl_tok

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(2, 10))

        for attr, sym, color in [("_in", "↑ Input", BLUE),
                                  ("_out", "↓ Output", ORANGE),
                                  ("_cr", "⚡ Cache", GREEN)]:
            lbl = ctk.CTkLabel(row, text=f"{sym}  0", font=("Segoe UI", 9),
                               text_color=color)
            lbl.pack(side="left", padx=(0, 10))
            setattr(card, attr, lbl)

        return card

    def _fill_card(self, card: ctk.CTkFrame, s: Stats):
        card._tok.configure(text=fmt_tok(s.total))
        card._cost.configure(text=fmt_cost(s.cost))
        card._in.configure(text=f"↑ Input  {fmt_tok(s.inp)}")
        card._out.configure(text=f"↓ Output  {fmt_tok(s.out)}")
        card._cr.configure(text=f"⚡ Cache  {fmt_tok(s.cr)}")

    def _fill_projects(self):
        for w in self._proj_frame.winfo_children():
            w.destroy()

        items = list(self.loader.projects.items())[:15]
        if not items:
            ctk.CTkLabel(self._proj_frame, text="Sem dados ainda.",
                         text_color=MUTED, font=("Segoe UI", 11)).pack(pady=20)
            return

        for name, s in items:
            row = ctk.CTkFrame(self._proj_frame, fg_color=CARD, corner_radius=8)
            row.pack(fill="x", pady=2)

            left = ctk.CTkFrame(row, fg_color="transparent")
            left.pack(side="left", padx=12, pady=8, fill="x", expand=True)

            ctk.CTkLabel(left, text=name[:32], font=("Segoe UI", 10, "bold"),
                         text_color=TXT, anchor="w").pack(anchor="w")
            ctk.CTkLabel(left,
                         text=f"↑{fmt_tok(s.inp)}  ↓{fmt_tok(s.out)}  ⚡{fmt_tok(s.cr)}",
                         font=("Segoe UI", 9), text_color=MUTED, anchor="w").pack(anchor="w")

            right = ctk.CTkFrame(row, fg_color="transparent")
            right.pack(side="right", padx=12)
            ctk.CTkLabel(right, text=fmt_tok(s.total),
                         font=("Segoe UI", 11, "bold"), text_color=BLUE).pack(anchor="e")
            ctk.CTkLabel(right, text=fmt_cost(s.cost),
                         font=("Segoe UI", 9), text_color=GREEN).pack(anchor="e")

    # ── public ───────────────────────────────
    def refresh(self):
        threading.Thread(target=self._bg_reload, daemon=True).start()

    def _bg_reload(self):
        self.loader.reload()
        self.after(0, self._update_ui)

    def _update_ui(self):
        self._fill_card(self._card_today,   self.loader.today)
        self._fill_card(self._card_alltime, self.loader.alltime)
        self._update_session_bar()
        self._fill_projects()
        if self.loader.updated_at:
            self._lbl_time.configure(
                text=self.loader.updated_at.strftime("%H:%M:%S"))

    def hide(self):
        self.withdraw()

    def show(self):
        self._place_window()
        self.deiconify()
        self.lift()
        self.focus_force()
        self.refresh()


# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────
def _btn(parent, text, cmd, color):
    return ctk.CTkButton(
        parent, text=text, width=30, height=30,
        fg_color="transparent", hover_color=BORDER,
        text_color=color, font=("Segoe UI", 13),
        command=cmd, corner_radius=6
    )


def _make_icon() -> Image.Image:
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d    = ImageDraw.Draw(img)
    d.ellipse([2, 2, 62, 62], fill="#58a6ff")
    try:
        font = ImageFont.truetype("arialbd.ttf", 22)
    except Exception:
        try:
            font = ImageFont.truetype("arial.ttf", 22)
        except Exception:
            font = ImageFont.load_default()
    text = "CT"
    bb   = d.textbbox((0, 0), text, font=font)
    tw, th = bb[2]-bb[0], bb[3]-bb[1]
    d.text(((size-tw)//2 - bb[0], (size-th)//2 - bb[1]), text, fill="white", font=font)
    return img


# ──────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────
def main():
    loader  = Loader()
    watcher = SessionWatcher()
    app     = MonitorWindow(loader, watcher)
    app.withdraw()

    def toggle(icon, menu_item):
        if app.winfo_viewable():
            app.after(0, app.hide)
        else:
            app.after(0, app.show)

    def quit_app(icon, menu_item):
        icon.stop()
        app.after(0, app.destroy)

    tray = pystray.Icon(
        "claude-tokens",
        _make_icon(),
        "Claude Token Monitor",
        menu=pystray.Menu(
            item("Abrir / Fechar", toggle, default=True),
            item("Sair",           quit_app),
        ),
    )

    tray.run_detached()
    watcher.attach(tray)
    watcher.start()

    # Auto-refresh loop (dados + barra de sessão)
    def _auto():
        while True:
            time.sleep(REFRESH_SEC)
            app.after(0, app.refresh)
            app.after(0, app._update_session_bar)

    threading.Thread(target=_auto, daemon=True).start()

    # First load
    threading.Thread(
        target=lambda: (loader.reload(), app.after(0, app._update_ui)),
        daemon=True
    ).start()

    app.mainloop()
    tray.stop()


if __name__ == "__main__":
    main()
