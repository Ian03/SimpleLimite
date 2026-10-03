import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import unittest

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QLabel

import main
from codex_usage import CodexApiPoller, normalize_usage
from ui import MonitorWindow
from test_usage import NOW, PAYLOAD


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.codex = CodexApiPoller()
        self.codex.limits, self.codex.notes = normalize_usage(PAYLOAD, NOW)
        self.codex.fetched_at = NOW
        self.codex.stale = False
        cursor = main.CursorLoader()
        self.window = MonitorWindow(main.Loader(), main.ApiPoller(), main.CodexLoader(),
                                    cursor, main.CursorApiPoller(cursor), self.codex)
        self.window._tab = "Codex"
        self.window.show()
        self.window._update_ui()
        self.app.processEvents()

    def tearDown(self):
        self.window._quitting = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def drag(self, widget):
        start = widget.mapToGlobal(QPoint(5, 5))
        offset = QPoint(-120, -100)
        for kind, pos, button, buttons in (
            (QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
            (QEvent.Type.MouseMove, start + offset, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton),
            (QEvent.Type.MouseButtonRelease, start + offset, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton),
        ):
            event = QMouseEvent(kind, QPointF(widget.mapFromGlobal(pos)), QPointF(pos), button, buttons,
                                Qt.KeyboardModifier.NoModifier)
            self.app.sendEvent(widget, event)
        self.app.processEvents()

    def test_drag_keeps_position_and_background_updates(self):
        before = self.window.pos()
        self.drag(self.window._m_pct)
        after = self.window.pos()
        self.assertNotEqual(before, after)
        self.assertIsNone(self.window._drag_offset)
        self.codex.limits[0]["pct"] = 64
        worker = threading.Thread(target=self.codex._notify)
        worker.start()
        worker.join()
        self.app.processEvents()
        self.assertEqual(self.window._m_pct.text(), "64%")
        self.assertEqual(self.window.pos(), after)
        self.window._go_expanded()
        title = next(w for w in self.window.findChildren(QLabel) if w.text() == "Simple Limite")
        self.drag(title)
        expanded_position = self.window.pos()
        self.codex.limits[1]["pct"] = 81
        self.codex.error = "API com rate limit"
        self.codex.stale = True
        self.window.request_ui_update()
        self.app.processEvents()
        labels = [w.text() for w in self.window._e_limits.findChildren(QLabel)]
        self.assertIn("81%", labels)
        self.assertIn("API com rate limit", labels)
        self.assertEqual(self.window.pos(), expanded_position)
        self.window._go_minimal()
        self.assertEqual(self.window.pos(), expanded_position)


if __name__ == "__main__":
    unittest.main()
