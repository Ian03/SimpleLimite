import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import main
import opencode_usage
from opencode_usage import (OpenCodeApiPoller, OpenCodeLoader, discover_server,
                            stats_from_payload, build_catalog, build_current_bar)

TODAY = {"tokens": {"input": 1000, "output": 500, "reasoning": 100,
                    "cache": {"read": 200, "write": 50}},
         "cost": 0.001, "sessions": 2, "steps": 10,
         "models": [{"model": {"id": "big-pickle", "providerID": "opencode"},
                     "steps": 3, "tokens": {"input": 1000, "output": 500}, "cost": 0.001}]}
ALL = {"tokens": {"input": 4000, "output": 2000, "reasoning": 400,
                  "cache": {"read": 800, "write": 0}},
       "cost": 0.01, "sessions": 5, "steps": 30, "activeDays": 3, "streak": 2,
       "models": [{"model": {"id": "big-pickle", "providerID": "opencode"},
                   "steps": 20, "tokens": {"input": 4000, "output": 2000}, "cost": 0.01}]}
CATALOG = [
    {"providerID": "opencode", "modelID": "big-pickle", "name": "Big Pickle",
     "enabled": True, "limit": {"context": 200000, "input": 160000, "output": 32000}},
    {"providerID": "opencode", "modelID": "mimo-v2.6-flash-free", "name": "MiMo",
     "enabled": True, "limit": {"context": 100000, "output": 32000}},
]
ACTIVE = {"ses_abc123": {"type": "running"}}
SESSION = {"id": "ses_abc123", "title": "Teste",
           "model": {"id": "mimo-v2.6-flash-free", "providerID": "opencode"}}
MESSAGES = [
    {"info": {"id": "m1", "model": {"id": "mimo-v2.6-flash-free"},
              "tokens": {"input": 1000, "output": 500, "reasoning": 0,
                         "cache": {"read": 5000, "write": 0}}}},
]


def resp(data=None, status=200):
    r = Mock(status_code=status)
    r.raise_for_status = Mock()
    r.json.return_value = data if data is not None else {}
    return r


class OpenCodeTests(unittest.TestCase):
    def test_stats_from_payload(self):
        stats, models, meta = stats_from_payload(ALL)
        self.assertEqual(stats.total, 6000)
        self.assertEqual(stats.inp, 4000)
        self.assertEqual(stats.out, 2000)
        self.assertEqual(stats.cached, 800)
        self.assertEqual(stats.reasoning, 400)
        self.assertEqual(models[0]["name"], "opencode/big-pickle")
        self.assertEqual(meta["sessions"], 5)
        self.assertEqual(meta["active_days"], 3)

    def test_discovery_prefers_env(self):
        with patch.dict(os.environ, {"OPENCODE_SERVER": "http://localhost:9999"}):
            url, _pw = discover_server()
            self.assertEqual(url, "http://localhost:9999")

    def test_build_current_bar_uses_session_model(self):
        catalog = build_catalog(CATALOG)
        bar = build_current_bar(catalog, SESSION, MESSAGES)
        self.assertEqual(bar["label"], "mimo-v2.6-flash-free · opencode")
        self.assertEqual(bar["limit_tokens"], 100000)
        self.assertEqual(bar["used"], 6500)
        self.assertAlmostEqual(bar["pct"], 6.5, places=1)
        self.assertIn("93.5K disponíveis", bar["detail"])

    def test_build_current_bar_clamps_at_100(self):
        catalog = build_catalog(CATALOG)
        msgs = [{"info": {"model": {"id": "mimo-v2.6-flash-free"},
                          "tokens": {"input": 60000, "output": 50000,
                                     "cache": {"read": 100000, "write": 0}}}}]
        bar = build_current_bar(catalog, SESSION, msgs)
        self.assertEqual(bar["used"], 210000)
        self.assertEqual(bar["pct"], 100.0)

    def test_build_current_bar_without_limit(self):
        bar = build_current_bar({}, SESSION, MESSAGES)
        self.assertEqual(bar["pct"], 0.0)
        self.assertIn("não informa janela", bar["detail"])

    def test_poller_switches_to_current_model_on_refresh(self):
        session_b = {"id": "ses_abc123", "model": {"id": "big-pickle", "providerID": "opencode"}}
        msgs_b = [{"info": {"model": {"id": "big-pickle"},
                            "tokens": {"input": 40000, "output": 3000,
                                       "cache": {"read": 110000, "write": 0}}}}]
        with tempfile.TemporaryDirectory() as root:
            with patch.object(opencode_usage, "discover_server", return_value=("http://127.0.0.1:45278", "pw")), \
                 patch.object(opencode_usage.app_paths, "DATA_DIR", Path(root)):
                p = OpenCodeApiPoller()
                # primeira consulta: modelo A
                with patch.object(requests, "get") as get:
                    get.side_effect = [resp({"version": "2"}), resp({"data": TODAY}),
                                       resp({"data": ALL}), resp({"data": CATALOG}),
                                       resp({"data": ACTIVE}), resp({"data": [SESSION]}),
                                       resp({"data": MESSAGES})]
                    p.fetch_now()
                self.assertEqual(p.limits[0]["model"], "opencode/mimo-v2.6-flash-free")
                # ao atualizar, o usuário trocou de modelo no OpenCode
                with patch.object(requests, "get") as get:
                    get.side_effect = [resp({"version": "2"}), resp({"data": TODAY}),
                                       resp({"data": ALL}), resp({"data": CATALOG}),
                                       resp({"data": ACTIVE}), resp({"data": [session_b]}),
                                       resp({"data": msgs_b})]
                    p.fetch_now()
                self.assertEqual(p.limits[0]["model"], "opencode/big-pickle")
                self.assertEqual(p.limits[0]["used"], 153000)
                self.assertAlmostEqual(p.limits[0]["pct"], 76.5, places=1)
                self.assertIn("47.0K disponíveis", p.limits[0]["detail"])

    def test_poller_prefers_active_session_over_more_recent_idle(self):
        idle_newer = {"id": "ses_newer", "model": {"id": "big-pickle", "providerID": "opencode"}}
        active_older = {"id": "ses_active", "model": {"id": "mimo-v2.6-flash-free", "providerID": "opencode"}}
        with tempfile.TemporaryDirectory() as root:
            with patch.object(opencode_usage, "discover_server", return_value=("http://x", "pw")), \
                 patch.object(opencode_usage.app_paths, "DATA_DIR", Path(root)):
                p = OpenCodeApiPoller()
                with patch.object(requests, "get") as get:
                    get.side_effect = [resp({"version": "2"}), resp({"data": TODAY}),
                                       resp({"data": ALL}), resp({"data": CATALOG}),
                                       resp({"data": {"ses_active": {"type": "running"}}}),
                                       resp({"data": [idle_newer, active_older]}),
                                       resp({"data": MESSAGES})]
                    p.fetch_now()
                self.assertEqual(p.limits[0]["model"], "opencode/mimo-v2.6-flash-free")

    def test_poller_fetches_and_caches(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(opencode_usage, "discover_server", return_value=("http://127.0.0.1:45278", "pw")), \
                 patch.object(opencode_usage.app_paths, "DATA_DIR", Path(root)):
                p = OpenCodeApiPoller()
                with patch.object(requests, "get") as get:
                    get.side_effect = [
                        resp({"version": "2.0.31"}),   # /api/info
                        resp({"data": TODAY}),          # hoje
                        resp({"data": ALL}),            # total
                        resp({"data": CATALOG}),        # modelos instalados
                        resp({"data": ACTIVE}),         # sessão ativa
                        resp({"data": [SESSION]}),      # lista de sessões
                        resp({"data": MESSAGES}),       # mensagens
                    ]
                    p.fetch_now()
                self.assertFalse(p.stale)
                self.assertIsNone(p.error)
                self.assertEqual(p.today.total, 1500)
                self.assertEqual(p.alltime.total, 6000)
                self.assertIn("opencode/big-pickle", p.projects)
                # barra do modelo em uso
                self.assertEqual(len(p.limits), 1)
                self.assertEqual(p.limits[0]["label"], "mimo-v2.6-flash-free · opencode")
                self.assertEqual(p.limits[0]["limit_tokens"], 100000)
                # 1000 (input) + 500 (output) + 5000 (cache read) = 6500
                self.assertEqual(p.limits[0]["used"], 6500)
                self.assertAlmostEqual(p.limits[0]["pct"], 6.5, places=1)
                self.assertIn("disponíveis", p.limits[0]["detail"])
                # Basic auth enviado
                _args, kwargs = get.call_args_list[0]
                self.assertTrue(kwargs["headers"]["Authorization"].startswith("Basic "))
                cache = Path(root) / "opencode.json"
                self.assertTrue(cache.exists())

    def test_poller_reports_connection_error(self):
        with patch.object(opencode_usage, "discover_server", return_value=("http://127.0.0.1:1", None)):
            p = OpenCodeApiPoller()
            with patch.object(requests, "get", side_effect=requests.exceptions.ConnectionError()):
                p.fetch_now()
            self.assertTrue(p.stale)
            self.assertIn("Não foi possível conectar", p.error)

    def test_loader_reads_cache(self):
        with tempfile.TemporaryDirectory() as root:
            cache = Path(root) / "opencode.json"
            cache.write_text(
                json.dumps({
                    "fetched_at": "2026-01-01T00:00:00Z",
                    "today": TODAY,
                    "all": ALL,
                    "current": build_current_bar(build_catalog(CATALOG), SESSION, MESSAGES),
                }), encoding="utf-8")
            with patch.object(opencode_usage.app_paths, "DATA_DIR", Path(root)):
                loader = OpenCodeLoader()
                loader.reload()
                self.assertEqual(loader.alltime.total, 6000)
                self.assertEqual(loader.today.total, 1500)
                self.assertEqual(len(loader.limits), 1)
                self.assertEqual(loader.limits[0]["label"], "mimo-v2.6-flash-free · opencode")
                self.assertEqual(loader.limits[0]["kind"], "context")

    def test_ui_includes_opencode_tab(self):
        import ui
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        w = ui.MonitorWindow(
            main.Loader(),
            main.ApiPoller(),
            main.CodexLoader(),
            main.CursorLoader(),
            main.CursorApiPoller(main.CursorLoader()),
            main.CodexApiPoller(),
            opencode_loader=OpenCodeLoader(),
            opencode_poller=OpenCodeApiPoller(),
        )
        w._go_expanded()
        w._tab = "OpenCode"
        w._update_expanded()
        tabs = []
        for btn in w.findChildren(ui.QPushButton):
            txt = btn.text()
            if txt in ("Claude", "Codex", "Cursor", "OpenCode"):
                tabs.append(txt)
        w.close()
        w.deleteLater()
        app.processEvents()
        self.assertIn("OpenCode", tabs)

    def test_ui_renders_context_bar(self):
        import ui
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QCoreApplication, QEvent

        app = QApplication.instance() or QApplication([])
        poller = OpenCodeApiPoller()
        poller.limits = [build_current_bar(build_catalog(CATALOG), SESSION, MESSAGES)]
        w = ui.MonitorWindow(
            main.Loader(),
            main.ApiPoller(),
            main.CodexLoader(),
            main.CursorLoader(),
            main.CursorApiPoller(main.CursorLoader()),
            main.CodexApiPoller(),
            opencode_loader=OpenCodeLoader(),
            opencode_poller=poller,
        )
        w._go_expanded()
        w._tab = "OpenCode"
        w._update_expanded()
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
        bars = w._e_limits.findChildren(ui.QProgressBar)
        labels = [l.text() for l in w._e_limits.findChildren(ui.QLabel)]
        bar_value = bars[0].value() if bars else None
        first_row = w._projects_layout.itemAt(0).widget() if w._projects_layout.count() else None
        row_labels = [l.text() for l in first_row.findChildren(ui.QLabel)] if first_row else []
        w.close()
        w.deleteLater()
        app.processEvents()
        self.assertEqual(len(bars), 1)
        self.assertEqual(bar_value, 6)
        self.assertTrue(any("mimo-v2.6-flash-free" in t for t in labels))
        self.assertTrue(any("disponíveis" in t for t in labels))
        # o modelo em uso fica fixado no topo da lista por modelo
        self.assertTrue(any("opencode/mimo-v2.6-flash-free" in t for t in row_labels))
        self.assertTrue(any("em uso" in t for t in row_labels))


if __name__ == "__main__":
    unittest.main()
