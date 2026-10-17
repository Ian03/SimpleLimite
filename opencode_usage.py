"""Leitura (read-only) do uso do OpenCode via API local.

O OpenCode expõe estatísticas de sessão em `/api/experimental/session/stats`
e a lista de modelos instalados em `/api/model`, com a janela de contexto de
cada modelo. A API local exige autenticação Basic (usuário `opencode` + senha
do arquivo de estado do servidor) e a porta é dinâmica, então a URL é
descoberta em `~/.local/state/opencode/service.json`.

A aba OpenCode mostra duas coisas:

* consumo de tokens de hoje, total e por modelo (estatísticas da sessão);
* a barra de contexto do modelo em uso (sessão ativa + limite de janela
  declarado pelo OpenCode para esse modelo). Ao atualizar, a barra passa a
  apontar para o modelo que está em uso no momento.

O OpenCode não informa percentuais de limite de plano (quota/créditos) pela
API local — apenas janela de contexto e consumo de tokens.
"""
import base64
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import app_paths

DEFAULT_SERVER = os.environ.get("OPENCODE_SERVER", "http://127.0.0.1:4096")


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / "opencode"


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "opencode"


def discover_server() -> "tuple[str, str | None]":
    """Retorna (url, senha) do servidor OpenCode em execução.

    Ordem: variável OPENCODE_SERVER (sem senha do arquivo de estado,
    a menos que exista), arquivo de estado e, por fim, o padrão local.
    """
    url, password = None, None
    for state in (_state_dir() / "service.json", _config_dir() / "service.json"):
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        url = url or data.get("url")
        password = password or data.get("password")
    explicit = os.environ.get("OPENCODE_SERVER")
    if explicit:
        return explicit.rstrip("/"), password
    if url:
        return url.rstrip("/"), password
    return DEFAULT_SERVER.rstrip("/"), password


def _parse_dt(v):
    if not v:
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(float(v), tz=timezone.utc)
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _cache_file() -> Path:
    return app_paths.DATA_DIR / "opencode.json"


def _fmt_tok(n):
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def build_catalog(payload) -> dict:
    """Monta um índice `providerID/modelID` -> limites declarados do modelo.

    O OpenCode declara para cada modelo instalado a janela de contexto
    (`limit.context`) e a saída máxima (`limit.output`). Não há quota/créditos.
    """
    catalog = {}
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        provider = item.get("providerID") or "?"
        model_id = item.get("modelID") or item.get("id") or "?"
        catalog[f"{provider}/{model_id}"] = {
            "limit": item.get("limit") or {},
            "name": item.get("name") or model_id,
            "enabled": bool(item.get("enabled", True)),
        }
    return catalog


def _session_tokens(message):
    """Tokens da mensagem; considera o prompt completo (não cacheado + cache)."""
    info = message.get("info") if isinstance(message.get("info"), dict) else message
    if not isinstance(info, dict):
        return None
    tk = info.get("tokens")
    if not isinstance(tk, dict):
        return None
    cache = tk.get("cache")
    cache_read = int(cache.get("read") or 0) if isinstance(cache, dict) else int(cache or 0)
    return {
        "input": int(tk.get("input") or 0),
        "output": int(tk.get("output") or 0),
        "reasoning": int(tk.get("reasoning") or 0),
        "cache_read": cache_read,
        "used": int(tk.get("input") or 0) + int(tk.get("output") or 0) + cache_read,
        "model": ((info.get("model") or {}).get("id")
                  if isinstance(info.get("model"), dict) else None),
    }


def build_current_bar(catalog, session, messages):
    """Cria a barra de contexto do modelo da sessão ativa.

    ``messages`` deve vir em ordem decrescente (mais recentes primeiro).
    O contexto atual é aproximado pela última resposta do modelo em uso:
    prompt (cacheado + não cacheado) + saída.
    """
    model = session.get("model") or {}
    provider = model.get("providerID") or "?"
    model_id = model.get("id") or "?"
    key = f"{provider}/{model_id}"
    limit = (catalog.get(key) or {}).get("limit") or {}

    chosen = None
    for msg in messages or []:
        parsed = _session_tokens(msg)
        if not parsed:
            continue
        if parsed["model"] and model_id and parsed["model"] != model_id:
            continue
        chosen = parsed
        break
    if chosen is None:
        for msg in messages or []:
            parsed = _session_tokens(msg)
            if parsed:
                chosen = parsed
                break

    used = chosen["used"] if chosen else 0
    ctx_limit = limit.get("context")
    label = f"{model_id} · {provider}"
    if ctx_limit:
        pct = min(100.0, used / ctx_limit * 100) if ctx_limit else 0.0
        available = max(0, int(ctx_limit) - used)
        detail = (f"{_fmt_tok(used)} de {_fmt_tok(ctx_limit)} tokens usados · "
                  f"{_fmt_tok(available)} disponíveis")
    else:
        pct = 0.0
        detail = (f"O OpenCode não informa janela de contexto para {model_id} · "
                  f"{_fmt_tok(used)} tokens na última resposta")
    out_limit = limit.get("output")
    return {
        "label": label,
        "pct": round(pct, 1),
        "detail": detail,
        "kind": "context",
        "reset_at": None,
        "model": key,
        "used": used,
        "limit_tokens": int(ctx_limit) if ctx_limit else None,
        "output_limit": int(out_limit) if out_limit else None,
        "session_title": session.get("title"),
        "session_id": session.get("id"),
    }


class OpenCodeStats:
    __slots__ = ("inp", "out", "reasoning", "cached", "cost", "steps", "sessions")

    def __init__(self):
        self.inp = self.out = self.reasoning = self.cached = 0
        self.cost = 0.0
        self.steps = 0
        self.sessions = 0

    def add_tokens(self, tokens, cost=0.0, steps=0):
        if isinstance(tokens, dict):
            self.inp += int(tokens.get("input") or 0)
            self.out += int(tokens.get("output") or 0)
            self.reasoning += int(tokens.get("reasoning") or 0)
            cache = tokens.get("cache")
            if isinstance(cache, dict):
                self.cached += int(cache.get("read") or 0)
                self.cached += int(cache.get("write") or 0)
            elif cache:
                self.cached += int(cache)
        try:
            self.cost += float(cost or 0)
        except (TypeError, ValueError):
            pass
        try:
            self.steps += int(steps or 0)
        except (TypeError, ValueError):
            pass

    @property
    def total(self):
        return self.inp + self.out


def stats_from_payload(data) -> "tuple[OpenCodeStats, list[dict], dict]":
    """Converte o payload da API em (stats, modelos, meta)."""
    stats = OpenCodeStats()
    stats.add_tokens(data.get("tokens"), data.get("cost"), data.get("steps"))
    stats.sessions = int(data.get("sessions") or 0)
    models = []
    for item in data.get("models") or []:
        ref = item.get("model") or {}
        provider = ref.get("providerID") or "?"
        model_id = ref.get("id") or "?"
        model_stats = OpenCodeStats()
        model_stats.add_tokens(item.get("tokens"), item.get("cost"), item.get("steps"))
        models.append({
            "name": f"{provider}/{model_id}",
            "stats": model_stats,
        })
    models.sort(key=lambda m: m["stats"].total, reverse=True)
    meta = {
        "sessions": int(data.get("sessions") or 0),
        "subagents": int(data.get("subagents") or 0),
        "prompts": int(data.get("prompts") or 0),
        "steps": int(data.get("steps") or 0),
        "active_days": int(data.get("activeDays") or 0),
        "streak": int(data.get("streak") or 0),
        "cost": float(data.get("cost") or 0),
    }
    return stats, models, meta


class OpenCodeLoader:
    """Fallback: lê o último resultado salvo em cache."""

    def __init__(self):
        self.today = OpenCodeStats()
        self.alltime = OpenCodeStats()
        self.projects: dict[str, OpenCodeStats] = {}
        self.limits: list[dict] = []
        self.error = None
        self.updated_at = None
        self._lock = threading.Lock()

    def reload(self):
        try:
            saved = json.loads(_cache_file().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            with self._lock:
                self.updated_at = self.updated_at or datetime.now()
            return
        with self._lock:
            self.updated_at = _parse_dt(saved.get("fetched_at"))
            today, _models, _meta = stats_from_payload(saved.get("today") or {})
            alltime, models, _meta = stats_from_payload(saved.get("all") or {})
            self.today = today
            self.alltime = alltime
            self.projects = {m["name"]: m["stats"] for m in models}
            bar = saved.get("current")
            if isinstance(bar, dict) and bar.get("label"):
                bar.setdefault("kind", "context")
                bar.setdefault("reset_at", None)
                self.limits = [bar]


class OpenCodeApiPoller:
    def __init__(self):
        self.limits: list[dict] = []
        self.notes: list[str] = []
        self.today = OpenCodeStats()
        self.alltime = OpenCodeStats()
        self.projects: dict[str, OpenCodeStats] = {}
        self.error = None
        self.fetched_at = None
        self.stale = True
        self._fetch_lock = threading.Lock()
        self._cbs: list = []
        self._server = DEFAULT_SERVER

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

    def _headers(self, password):
        if not password:
            return {}
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def _get(self, url, headers, path, params=None):
        """GET autenticado; devolve o campo `data` da resposta quando presente."""
        resp = requests.get(f"{url}{path}", headers=headers, params=params, timeout=10)
        if resp.status_code == 401:
            raise PermissionError(f"OpenCode: autenticação recusada em {path}")
        if resp.status_code == 404:
            raise ValueError(f"OpenCode não expõe {path} (atualize o OpenCode)")
        resp.raise_for_status()
        try:
            payload = resp.json() if resp.content else {}
        except ValueError:
            return {}
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload or {}

    def _get_stats(self, url, headers, params):
        return self._get(url, headers, "/api/experimental/session/stats", params) or {}

    def _fetch_current(self, url, headers):
        """Barra de contexto do modelo em uso na sessão ativa.

        Lê os modelos instalados (`/api/model`), a lista de sessões ordenada
        pela mais recente e a sessão ativa, para escolher o modelo em uso
        naquele momento. Assim, ao atualizar, a barra acompanha a troca de
        modelo feita no cliente.
        """
        catalog = build_catalog(self._get(url, headers, "/api/model"))
        active = self._get(url, headers, "/api/session/active") or {}
        active_ids = set(active) if isinstance(active, dict) else set()
        recent = self._get(url, headers, "/api/session",
                           params={"limit": 10, "order": "desc"}) or []
        if not isinstance(recent, list):
            recent = []
        session = next((s for s in recent
                        if isinstance(s, dict) and s.get("id") in active_ids), None)
        if session is None and recent:
            session = recent[0]
        if not isinstance(session, dict) or not session.get("id"):
            return None, catalog
        messages = self._get(url, headers, f"/api/session/{session['id']}/message",
                             params={"limit": 8, "order": "desc",
                                     "type": "assistant"}) or []
        if not isinstance(messages, list):
            messages = []
        return build_current_bar(catalog, session, messages), catalog

    def _fetch(self):
        try:
            url, password = discover_server()
            self._server = url
            headers = self._headers(password)

            info = requests.get(f"{url}/api/info", headers=headers, timeout=5)
            if info.status_code == 401:
                raise PermissionError("autenticação recusada")
            if info.status_code == 404:
                raise ValueError("OpenCode API não encontrada no servidor informado")
            info.raise_for_status()

            now = datetime.now().astimezone()
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            to_ms = int(now.timestamp() * 1000)
            today_ms = int(midnight.timestamp() * 1000)

            today_raw = self._get_stats(url, headers, {"from": today_ms, "to": to_ms})
            all_raw = self._get_stats(url, headers, {"from": 0, "to": to_ms})

            today, _tm, _tmeta = stats_from_payload(today_raw)
            alltime, models, meta = stats_from_payload(all_raw)

            notes = []
            try:
                bar, catalog = self._fetch_current(url, headers)
            except (requests.exceptions.RequestException, ValueError) as exc:
                bar, catalog = None, {}
                notes.append(f"Não foi possível ler o modelo em uso ({exc})")
            self.limits = [bar] if bar else []

            self.today = today
            self.alltime = alltime
            self.projects = {m["name"]: m["stats"] for m in models}
            self.fetched_at = datetime.now(timezone.utc)
            self.error = None
            self.stale = False
            notes.append(f"Modelos instalados: {len(catalog)}")
            notes.append(f"Sessões: {meta['sessions']} · Passos: {meta['steps'] or alltime.steps}")
            notes.append(f"Dias ativos: {meta['active_days']} · Sequência: {meta['streak']}")
            if alltime.cost:
                notes.append(f"Custo estimado (total): ${alltime.cost:.2f}")
            self.notes = notes
            try:
                _cache_file().parent.mkdir(parents=True, exist_ok=True)
                _cache_file().write_text(json.dumps({
                    "fetched_at": self.fetched_at.isoformat(),
                    "today": today_raw,
                    "all": all_raw,
                    "current": self.limits[0] if self.limits else None,
                }), encoding="utf-8")
            except OSError:
                self.notes.append("Não foi possível salvar o cache de uso")
        except PermissionError as exc:
            self.stale = True
            self.error = f"OpenCode: {exc}. Reinicie o OpenCode ou verifique a autenticação"
            self._load_cache()
        except (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout):
            self.stale = True
            self.error = f"Não foi possível conectar em {self._server}. O OpenCode está rodando?"
            self._load_cache()
        except requests.exceptions.Timeout:
            self.stale = True
            self.error = f"Tempo limite esgotado ao conectar em {self._server}"
            self._load_cache()
        except requests.exceptions.RequestException as exc:
            self.stale = True
            self.error = f"Erro ao conectar em {self._server}: {exc}"
            self._load_cache()
        except ValueError as exc:
            self.stale = True
            self.error = str(exc)
            self._load_cache()
        except Exception:
            self.stale = True
            self.error = "Falha ao consultar OpenCode API"
            self._load_cache()

    def _load_cache(self):
        try:
            saved = json.loads(_cache_file().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.fetched_at = _parse_dt(saved.get("fetched_at"))
        self.today, _m1, _me1 = stats_from_payload(saved.get("today") or {})
        self.alltime, models, _me2 = stats_from_payload(saved.get("all") or {})
        self.projects = {m["name"]: m["stats"] for m in models}
        bar = saved.get("current")
        if isinstance(bar, dict) and bar.get("label"):
            bar.setdefault("kind", "context")
            bar.setdefault("reset_at", None)
            self.limits = [bar]

    def start(self):
        def loop():
            while True:
                self.fetch_now()
                time.sleep(60)

        threading.Thread(target=loop, name="opencode-api-poller", daemon=True).start()

    @staticmethod
    def mins_to_reset(lim):
        reset = lim.get("reset_at")
        if not reset:
            return 0
        return max(0, int((reset - datetime.now(timezone.utc)).total_seconds() / 60))
