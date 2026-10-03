"""Read-only Codex plan usage, with an account-scoped portable cache."""
import hashlib
import json
import math
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from app_paths import DATA_DIR, codex_home

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def parse_time(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(float(value), timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def normalize_limits(block, observed_at, prefix="Codex"):
    if not isinstance(block, dict):
        return []
    limits = []
    for key, alias in (("primary_window", "primary"), ("secondary_window", "secondary")):
        window = block.get(key) or block.get(alias)
        if not isinstance(window, dict) or window.get("used_percent") is None:
            continue
        pct = float(window["used_percent"])
        if not math.isfinite(pct) or not 0 <= pct <= 101:
            raise ValueError("Percentual inválido")
        seconds = int(window.get("limit_window_seconds") or int(window.get("window_minutes") or 0) * 60)
        if seconds >= 604800:
            label = "Semanal"
        elif seconds == 86400:
            label = "Diário"
        elif seconds:
            label = f"Janela de {seconds / 3600:g}h"
        else:
            label = "Janela principal" if key == "primary_window" else "Janela secundária"
        reset = parse_time(window.get("reset_at", window.get("resets_at")))
        if reset is None and window.get("reset_after_seconds") is not None:
            reset = observed_at + timedelta(seconds=float(window["reset_after_seconds"]))
        limits.append({"label": f"{prefix} · {label}", "pct": min(pct, 100),
                       "reset_at": reset, "is_session": seconds < 86400,
                       "kind": "timed", "used_usd": 0, "cap_usd": 0,
                       "fetched_at": observed_at})
    return limits


def normalize_usage(data, observed_at):
    limits = normalize_limits(data.get("rate_limit"), observed_at)
    limits += normalize_limits(data.get("code_review_rate_limit"), observed_at, "Revisão de código")
    for extra in data.get("additional_rate_limits") or []:
        limits += normalize_limits(extra.get("rate_limit"), observed_at,
                                   extra.get("limit_name") or extra.get("metered_feature") or "Limite adicional")
    if not limits:
        raise ValueError("A API não informou janelas de uso")
    notes = []
    if data.get("plan_type"):
        notes.append(f"Plano: {data['plan_type']}")
    rate = data.get("rate_limit") or {}
    if rate.get("limit_reached") is True or rate.get("allowed") is False:
        notes.append("Limite de uso atingido")
    elif rate.get("allowed") is True:
        notes.append("Uso permitido")
    resets = data.get("rate_limit_reset_credits")
    if isinstance(resets, dict) and resets.get("available_count") is not None:
        notes.append(f"Resets disponíveis: {int(resets['available_count'])}")
        expiries = [parse_time(c.get("expires_at")) for c in resets.get("credits") or []
                    if c.get("status") == "available"]
        expiries = [e for e in expiries if e]
        if expiries:
            notes.append(f"Primeiro reset extra expira: {min(expiries).astimezone():%d/%m %H:%M}")
    else:
        notes.append("Resets disponíveis: não informados")
    credits = data.get("credits") or {}
    if credits.get("unlimited"):
        notes.append("Créditos extras ilimitados")
    elif credits.get("balance") is not None:
        notes.append(f"Saldo de créditos extras: {credits['balance']}")
    for model, state in (data.get("model_usage") or {}).items():
        if state.get("available") is False:
            notes.append(f"{model}: indisponível")
    return limits, notes


class CodexApiPoller:
    def __init__(self):
        self.limits = []
        self.notes = []
        self.error = None
        self.fetched_at = None
        self.stale = True
        self._account = None
        self._retry_at = 0
        self._fetch_lock = threading.Lock()
        self._cbs = []

    def on_update(self, fn):
        self._cbs.append(fn)

    def _notify(self):
        for fn in self._cbs:
            try:
                fn()
            except Exception:
                pass

    def fetch_now(self):
        if not self._fetch_lock.acquire(blocking=False):
            return
        try:
            self._fetch()
        finally:
            self._fetch_lock.release()
            self._notify()

    def _fetch(self):
        try:
            auth = json.loads((codex_home() / "auth.json").read_text(encoding="utf-8"))
            tokens = auth.get("tokens") or {}
            token = tokens.get("access_token")
            if not token:
                raise ValueError("Entre no Codex com sua conta ChatGPT para consultar o plano")
            identity = tokens.get("account_id") or token
            account = hashlib.sha256(identity.encode()).hexdigest()[:24]
            cache = DATA_DIR / f"codex-{account}.json"
            if account != self._account:
                self.limits, self.notes, self.fetched_at = [], [], None
                self.stale = True
                self._retry_at = 0
                self._account = account
                try:
                    saved = json.loads(cache.read_text(encoding="utf-8"))
                    observed = parse_time(saved["fetched_at"])
                    if observed:
                        self.limits, self.notes = normalize_usage(saved["usage"], observed)
                        self.fetched_at = observed
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            if time.time() < self._retry_at:
                self.error = f"API com rate limit; nova tentativa em {math.ceil((self._retry_at - time.time()) / 60)}min"
                return
            headers = {"Authorization": f"Bearer {token}", "User-Agent": "codex-cli"}
            if tokens.get("account_id"):
                headers["ChatGPT-Account-Id"] = tokens["account_id"]
            response = requests.get(USAGE_URL, headers=headers, timeout=10)
            if response.status_code == 429:
                try:
                    delay = max(300, int(response.headers.get("Retry-After", 300)))
                except ValueError:
                    delay = 300
                self._retry_at = time.time() + delay
                raise ValueError(f"API com rate limit; nova tentativa em {math.ceil(delay / 60)}min")
            if response.status_code in (401, 403):
                raise ValueError("Autenticação Codex expirada ou recusada — reabra o Codex ou execute codex login")
            if not response.ok:
                raise ValueError(f"API Codex: HTTP {response.status_code}")
            data = response.json()
            resets = data.get("rate_limit_reset_credits") or {}
            if resets.get("available_count", 0):
                try:
                    details = requests.get(USAGE_URL.rsplit("/", 1)[0] + "/rate-limit-reset-credits",
                                           headers=headers, timeout=10)
                    if details.ok:
                        resets["credits"] = [{"status": c.get("status"), "expires_at": c.get("expires_at")}
                                             for c in details.json().get("credits") or []]
                except (requests.RequestException, ValueError, TypeError, AttributeError):
                    pass
            observed = datetime.now(timezone.utc)
            limits, notes = normalize_usage(data, observed)
            self.limits, self.notes = limits, notes
            self.fetched_at, self.error, self.stale = observed, None, False
            # Store only usage fields, never credentials or account profile data.
            safe = {k: data.get(k) for k in ("plan_type", "rate_limit", "code_review_rate_limit",
                    "additional_rate_limits", "credits", "model_usage")}
            safe["rate_limit_reset_credits"] = {
                "available_count": resets.get("available_count"),
                "credits": [{"status": c.get("status"), "expires_at": c.get("expires_at")}
                            for c in resets.get("credits") or []]}
            try:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=DATA_DIR,
                                                 delete=False) as f:
                    temp = f.name
                    json.dump({"fetched_at": observed.isoformat(), "usage": safe}, f)
                os.replace(temp, cache)
            except OSError:
                self.notes.append("Não foi possível salvar o cache de uso")
        except (OSError, ValueError, TypeError, AttributeError, requests.RequestException):
            self.stale = True
            # Exception messages from requests can contain URLs; avoid logging credentials.
            import sys
            exc = sys.exc_info()[1]
            self.error = str(exc) if isinstance(exc, ValueError) else "Falha ao consultar Codex; verifique conexão e autenticação"

    def start(self):
        def loop():
            while True:
                self.fetch_now()
                time.sleep(60)
        threading.Thread(target=loop, name="codex-api-poller", daemon=True).start()

    @staticmethod
    def mins_to_reset(lim):
        reset = lim.get("reset_at")
        return max(0, math.ceil((reset - datetime.now(timezone.utc)).total_seconds() / 60)) if reset else 0
