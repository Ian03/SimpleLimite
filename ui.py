"""PySide6 presentation layer for the Simple Limite monitor."""

from datetime import datetime, timezone

from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton,
    QProgressBar, QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout,
    QWidget, QButtonGroup,
)


BG = "#090b10"
CARD = "#11151d"
BORDER = "#242b38"
TEXT = "#f1f5fb"
MUTED = "#8a94a6"
BLUE = "#70a7ff"
GREEN = "#5bd6a2"
ORANGE = "#ffb86b"
RED = "#ff7185"

QSS = f"""
QWidget {{ color: {TEXT}; font-family: 'Segoe UI'; font-size: 10pt; }}
QFrame#surface {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 22px; }}
QFrame#header, QFrame#usageCard, QFrame#statCard, QFrame#projectCard {{
    background: {CARD}; border: 1px solid #1b2230; border-radius: 15px;
}}
QLabel#muted {{ color: {MUTED}; }}
QLabel#sectionTitle {{ color: {MUTED}; font-size: 9pt; font-weight: 700; letter-spacing: 1px; }}
QLabel#statValue {{ font-size: 19pt; font-weight: 700; }}
QPushButton {{ border: 0; border-radius: 10px; padding: 7px 11px; color: {MUTED}; background: transparent; }}
QPushButton:hover {{ color: {TEXT}; background: #202838; }}
QPushButton#tab {{ background: #11151d; color: {MUTED}; font-weight: 600; }}
QPushButton#tab:checked {{ background: #243650; color: {BLUE}; }}
QPushButton#expand {{ font-size: 13pt; padding: 2px 8px; }}
QProgressBar {{ border: 0; border-radius: 4px; background: {BORDER}; max-height: 8px; text-align: center; }}
QProgressBar::chunk {{ border-radius: 4px; background: {BLUE}; }}
QProgressBar#compactBar {{ min-height: 7px; max-height: 7px; }}
QScrollArea {{ border: 0; background: transparent; }}
QScrollArea QWidget#scrollContents {{ background: transparent; }}
QScrollBar:vertical {{ width: 7px; background: transparent; margin: 3px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 3px; min-height: 24px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""


def _fmt_tok(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_cost(usd):
    if usd == 0:
        return "$0.00"
    if usd < 0.001:
        return "< $0.001"
    return f"${usd:.4f}" if usd < 1 else f"${usd:.2f}"


def _fmt_dur(mins):
    if mins <= 0:
        return "agora"
    if mins < 60:
        return f"{mins}min"
    h, m = divmod(mins, 60)
    return f"{h}h {m:02d}m" if m else f"{h}h"


def _color(pct):
    return RED if pct >= 90 else ORANGE if pct >= 70 else BLUE


class UsageRing(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(28, 28)
        self.pct = 0
        self.accent = QColor(BLUE)

    def set_value(self, pct, accent):
        self.pct = max(0, min(100, pct))
        self.accent = QColor(accent)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(3, 3, -3, -3)
        painter.setPen(QPen(QColor(BORDER), 3))
        painter.drawArc(rect, 0, 360 * 16)
        painter.setPen(QPen(self.accent, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(rect, 90 * 16, -int(self.pct * 3.6 * 16))


class MonitorWindow(QMainWindow):
    ui_call = Signal(object)

    MINIMAL = "minimal"
    EXPANDED = "expanded"

    def __init__(self, loader, poller, codex_loader, cursor_loader, cursor_poller, codex_poller):
        super().__init__()
        self.loader = loader
        self.poller = poller
        self.codex_loader = codex_loader
        self.cursor_loader = cursor_loader
        self.cursor_poller = cursor_poller
        self.codex_poller = codex_poller
        self._mode = self.MINIMAL
        self._tab = "Claude"
        self._quitting = False
        self._drag_offset = None
        self._minimal_size = None
        self._user_position = None
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setStyleSheet(QSS)
        self.setWindowTitle("Simple Limite")
        self.root = QFrame()
        self.root.setObjectName("surface")
        self.setCentralWidget(self.root)
        self.root_layout = QVBoxLayout(self.root)
        self.ui_call.connect(self._run_ui_call)
        self.poller.on_update(self.request_ui_update)
        self.cursor_poller.on_update(self.request_ui_update)
        self.codex_poller.on_update(self.request_ui_update)
        self._build()
        self._position_minimal()
        self._timer = self.startTimer(15000)

    def timerEvent(self, event):
        self._update_ui()

    def _run_ui_call(self, fn):
        if not self._quitting:
            try:
                fn()
            except Exception as exc:
                print(f"UI callback failed: {exc}")

    def request_ui_call(self, fn):
        if not self._quitting:
            self.ui_call.emit(fn)

    def request_ui_update(self):
        self.request_ui_call(self._update_ui)

    def quit(self):
        self._quitting = True
        QApplication.instance().quit()

    def closeEvent(self, event):
        if self._quitting:
            event.accept()
        else:
            self._go_minimal()
            event.ignore()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            self._user_position = self.pos()
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_offset = None

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self.mousePressEvent(event)
            return True
        if event.type() == QEvent.Type.MouseMove and self._drag_offset is not None:
            self.mouseMoveEvent(event)
            return True
        if event.type() == QEvent.Type.MouseButtonRelease and self._drag_offset is not None:
            self.mouseReleaseEvent(event)
            return True
        return super().eventFilter(obj, event)

    def _position_minimal(self):
        if self._user_position is not None:
            self.move(self._user_position)
            return
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 14, screen.bottom() - self.height() - 14)

    def _position_expanded(self):
        if self._user_position is not None:
            screen = (QApplication.screenAt(self._user_position) or QApplication.primaryScreen()).availableGeometry()
            self.move(max(screen.left(), min(self._user_position.x(), screen.right() - self.width())),
                      max(screen.top(), min(self._user_position.y(), screen.bottom() - self.height())))
            return
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 14, screen.bottom() - self.height() - 14)

    def _clear(self):
        layout = self.root_layout
        self._clear_layout(layout)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(9)
        return layout

    def _build(self):
        # The expanded view is fixed-size; release that constraint before
        # rebuilding so the compact droplet can shrink to its own contents.
        self.setMinimumSize(1, 1)
        self.setMaximumSize(16777215, 16777215)
        if self._mode == self.MINIMAL:
            self._build_minimal()
            self.root_layout.activate()
            if self._minimal_size is None:
                self.adjustSize()
                self._minimal_size = self.sizeHint()
            self.setFixedSize(self._minimal_size)
            self._position_minimal()
        else:
            self._build_expanded()
            self.setFixedSize(420, 630)
            self._position_expanded()

    def _build_minimal(self):
        self.root.setObjectName("surface")
        layout = self._clear()
        layout.setContentsMargins(12, 8, 8, 8)
        layout.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(8)
        layout.addLayout(row)
        self._m_ring = UsageRing()
        row.addWidget(self._m_ring)
        self._m_pct = QLabel("--%")
        self._m_pct.installEventFilter(self)
        self._m_ring.installEventFilter(self)
        self._m_pct.setStyleSheet(f"color: {BLUE}; font-weight: 700")
        row.addWidget(self._m_pct)
        expand = QPushButton("⤢")
        expand.setObjectName("expand")
        expand.clicked.connect(self._go_expanded)
        row.addWidget(expand)

    def _build_expanded(self):
        self.root.setObjectName("surface")
        layout = self._clear()
        header = QFrame()
        header.setObjectName("header")
        h = QHBoxLayout(header)
        h.setContentsMargins(14, 8, 10, 8)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {BLUE}")
        h.addWidget(dot)
        title = QLabel("Simple Limite")
        for drag_widget in (header, dot, title):
            drag_widget.installEventFilter(self)
        title.setStyleSheet("font-size: 12pt; font-weight: 700")
        h.addWidget(title)
        h.addStretch(1)
        self._e_time = QLabel("")
        self._e_time.setObjectName("muted")
        h.addWidget(self._e_time)
        refresh = QPushButton("↻")
        refresh.clicked.connect(self._manual_refresh)
        h.addWidget(refresh)
        close = QPushButton("—")
        close.clicked.connect(self._go_minimal)
        h.addWidget(close)
        layout.addWidget(header)

        tabs = QHBoxLayout()
        tabs.setSpacing(6)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        for name in ("Claude", "Codex", "Cursor"):
            button = QPushButton(name)
            button.setObjectName("tab")
            button.setCheckable(True)
            button.setChecked(name == self._tab)
            button.clicked.connect(lambda checked=False, tab=name: self._set_tab(tab))
            self._tab_group.addButton(button)
            tabs.addWidget(button)
        layout.addLayout(tabs)

        section = QHBoxLayout()
        title = QLabel("LIMITES DE USO")
        title.setObjectName("sectionTitle")
        section.addWidget(title)
        section.addStretch(1)
        self._e_api_ts = QLabel("")
        self._e_api_ts.setObjectName("muted")
        section.addWidget(self._e_api_ts)
        layout.addLayout(section)

        self._e_limits = QFrame()
        self._e_limits.setObjectName("usageCard")
        self._limits_layout = QVBoxLayout(self._e_limits)
        self._limits_layout.setContentsMargins(12, 8, 12, 10)
        self._limits_layout.setSpacing(7)
        limits_scroll = QScrollArea()
        limits_scroll.setWidgetResizable(True)
        limits_scroll.setWidget(self._e_limits)
        limits_scroll.setMinimumHeight(140)
        limits_scroll.setMaximumHeight(300)
        layout.addWidget(limits_scroll, 1)

        cards = QHBoxLayout()
        cards.setSpacing(8)
        self._c_today = self._make_stat_card("Hoje", BLUE)
        self._c_alltime = self._make_stat_card("Total", GREEN)
        cards.addWidget(self._c_today)
        cards.addWidget(self._c_alltime)
        layout.addLayout(cards)

        projects_header = QLabel("PROJETOS")
        projects_header.setObjectName("sectionTitle")
        layout.addWidget(projects_header)
        self._e_proj = QScrollArea()
        self._e_proj.setWidgetResizable(True)
        contents = QWidget()
        contents.setObjectName("scrollContents")
        self._projects_layout = QVBoxLayout(contents)
        self._projects_layout.setContentsMargins(0, 0, 4, 0)
        self._projects_layout.setSpacing(6)
        self._projects_layout.addStretch(1)
        self._e_proj.setWidget(contents)
        layout.addWidget(self._e_proj, 1)

    def _make_stat_card(self, title, accent):
        card = QFrame()
        card.setObjectName("statCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        top = QHBoxLayout()
        label = QLabel(title)
        label.setObjectName("muted")
        top.addWidget(label)
        top.addStretch(1)
        cost = QLabel("$0.00")
        cost.setStyleSheet(f"color: {accent}; font-weight: 700")
        top.addWidget(cost)
        layout.addLayout(top)
        value = QLabel("0")
        value.setObjectName("statValue")
        layout.addWidget(value)
        detail = QLabel("↑ 0   ↓ 0   ⚡ 0")
        detail.setObjectName("muted")
        detail.setStyleSheet(f"color: {MUTED}; font-size: 8pt")
        layout.addWidget(detail)
        card._cost, card._tok, card._detail = cost, value, detail
        return card

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    def _set_tab(self, tab):
        self._tab = tab
        self._update_ui()

    def _go_minimal(self):
        self._mode = self.MINIMAL
        self._build()
        self.show()
        self._update_ui()

    def _go_expanded(self):
        self._mode = self.EXPANDED
        self._build()
        self.show()
        self.raise_()
        self._update_ui()

    def show_expanded(self):
        self._go_expanded()

    def _update_ui(self):
        if self._mode == self.MINIMAL:
            self._update_minimal()
        else:
            self._update_expanded()

    def _selected(self):
        if self._tab == "Codex":
            source = self.codex_poller if self.codex_poller.limits else self.codex_loader
            return self.codex_loader, source.limits, source
        if self._tab == "Cursor":
            return self.cursor_loader, self.cursor_poller.limits or self.cursor_loader.limits, self.cursor_poller
        return self.loader, self.poller.limits, self.poller

    def _update_minimal(self):
        _, limits, source = self._selected()
        tooltip = [self._tab]
        for lim in limits:
            reset = lim.get("reset_at")
            tooltip.append(f"{lim['label']}: {lim['pct']:.0f}% usado" +
                           (f" · reset {reset.astimezone():%d/%m %H:%M}" if reset else ""))
        if getattr(source, "error", None):
            tooltip.append(source.error)
        self.root.setToolTip("\n".join(tooltip))
        top = (source.top if self._tab == "Cursor" else limits[0] if limits else None)
        if top is None:
            self._m_pct.setText("--%")
            self._m_pct.setStyleSheet(f"color: {MUTED}; font-weight: 700")
            self._m_ring.set_value(0, BORDER)
            return
        pct = top["pct"]
        accent = _color(pct)
        self._m_ring.set_value(pct, accent)
        self._m_pct.setText(f"{pct:.0f}%")
        self._m_pct.setStyleSheet(f"color: {accent}; font-weight: 700")

    def _update_expanded(self):
        data, limits, source = self._selected()
        self._clear_layout(self._limits_layout)
        err = getattr(source, "error", None)
        if limits:
            for lim in limits:
                self._render_bar(lim, source)
        else:
            message = QLabel(f"⚠ {err}" if err else "Aguardando dados…")
            message.setObjectName("muted")
            self._limits_layout.addWidget(message)
        if self._tab == "Codex":
            for note in self.codex_poller.notes:
                self._limit_note(note)
            if source is self.codex_loader:
                self._limit_note("Histórico local · resets extras não informados")
            else:
                self._limit_note("API Codex" + (" · dados anteriores" if source.stale else " · sincronizado"))
            if self.codex_poller.error:
                self._limit_note(self.codex_poller.error, ORANGE)
        elif err and limits:
            self._limit_note(f"Dados anteriores · {err}", ORANGE)
        timestamp = getattr(source, "fetched_at", None) or getattr(data, "updated_at", None)
        if timestamp:
            timestamp = timestamp.astimezone()
        self._e_api_ts.setText(timestamp.strftime("%H:%M:%S") if timestamp else "")
        self._e_time.setText(timestamp.strftime("Atualizado %H:%M") if timestamp else "")
        if self._tab == "Claude":
            self._fill_stat(self._c_today, self.loader.today, "cost")
            self._fill_stat(self._c_alltime, self.loader.alltime, "cost")
            projects = [(n, s.total, s.cost, f"↑{_fmt_tok(s.inp)}  ↓{_fmt_tok(s.out)}  ⚡{_fmt_tok(s.cr)}") for n, s in list(self.loader.projects.items())[:12]]
        elif self._tab == "Codex":
            self._fill_stat(self._c_today, self.codex_loader.today, "codex")
            self._fill_stat(self._c_alltime, self.codex_loader.alltime, "codex")
            projects = [(n, s.total, None, f"↑{_fmt_tok(s.inp)}  ↓{_fmt_tok(s.out)}  ctx {_fmt_tok(s.cached)}") for n, s in list(self.codex_loader.projects.items())[:12]]
        else:
            self._fill_stat(self._c_today, self.cursor_loader.today, "cursor")
            self._fill_stat(self._c_alltime, self.cursor_loader.alltime, "cursor")
            projects = [(n, s.total, s.cost_cents / 100, f"{s.messages} respostas do agente") for n, s in list(self.cursor_loader.projects.items())[:12]]
        self._clear_layout(self._projects_layout)
        for name, total, cost, detail in projects:
            self._project_row(name, total, cost, detail)
        self._projects_layout.addStretch(1)

    def _limit_note(self, text, color=MUTED):
        note = QLabel(text)
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {color}; font-size: 8pt")
        self._limits_layout.addWidget(note)

    def _render_bar(self, lim, source):
        item = QWidget()
        layout = QVBoxLayout(item)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        top = QHBoxLayout()
        label = QLabel(lim["label"])
        label.setStyleSheet("font-weight: 650")
        top.addWidget(label)
        top.addStretch(1)
        pct = lim["pct"]
        color = _color(pct)
        percent = QLabel(f"{pct:.0f}%")
        percent.setStyleSheet(f"color: {color}; font-weight: 700")
        top.addWidget(percent)
        layout.addLayout(top)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(int(pct))
        bar.setTextVisible(False)
        bar.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; }}")
        layout.addWidget(bar)
        mins = source.mins_to_reset(lim)
        reset = lim.get("reset_at")
        if reset:
            remaining = f"em {_fmt_dur(mins)}" if reset > datetime.now(timezone.utc) else "aguardando atualização"
            text = f"{100 - pct:.0f}% disponível · reset {reset.astimezone():%d/%m %H:%M} ({remaining})"
        else:
            text = f"{100 - pct:.0f}% disponível · reset não informado"
        if lim.get("kind") == "credits":
            text = f"{_fmt_cost(lim['used_usd'])} / {_fmt_cost(lim['cap_usd'])} usados" + (
                f" · reset {reset.astimezone():%d/%m %H:%M}" if reset else "")
        meta = QLabel(text)
        meta.setWordWrap(True)
        meta.setObjectName("muted")
        meta.setStyleSheet(f"color: {GREEN if mins > 60 else ORANGE}; font-size: 8pt")
        layout.addWidget(meta)
        self._limits_layout.addWidget(item)

    def _fill_stat(self, card, stats, kind):
        card._tok.setText(_fmt_tok(stats.total))
        if kind == "cost":
            card._cost.setText(_fmt_cost(stats.cost))
            detail = f"↑ {_fmt_tok(stats.inp)}   ↓ {_fmt_tok(stats.out)}   ⚡ {_fmt_tok(stats.cr)}"
        elif kind == "codex":
            card._cost.setText(f"ctx {_fmt_tok(stats.cached)}")
            detail = f"↑ {_fmt_tok(stats.inp)}   ↓ {_fmt_tok(stats.out)}   ◈ {_fmt_tok(stats.reasoning)}"
        else:
            card._cost.setText(_fmt_cost(stats.cost_cents / 100))
            detail = f"↑ {_fmt_tok(stats.inp)}   ↓ {_fmt_tok(stats.out)}   ctx {_fmt_tok(stats.cached)}"
        card._detail.setText(detail)

    def _project_row(self, name, total, cost, detail):
        card = QFrame()
        card.setObjectName("projectCard")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(12, 9, 12, 9)
        left = QVBoxLayout()
        title = QLabel(name[:34])
        title.setStyleSheet("font-weight: 650")
        subtitle = QLabel(detail)
        subtitle.setObjectName("muted")
        subtitle.setStyleSheet(f"color: {MUTED}; font-size: 8pt")
        left.addWidget(title)
        left.addWidget(subtitle)
        layout.addLayout(left, 1)
        right = QVBoxLayout()
        total_label = QLabel(_fmt_tok(total))
        total_label.setStyleSheet(f"color: {BLUE}; font-weight: 700")
        right.addWidget(total_label, alignment=Qt.AlignmentFlag.AlignRight)
        if cost is not None:
            cost_label = QLabel(_fmt_cost(cost))
            cost_label.setStyleSheet(f"color: {GREEN}; font-size: 8pt")
            right.addWidget(cost_label, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addLayout(right)
        self._projects_layout.addWidget(card)

    def _manual_refresh(self):
        import threading
        threading.Thread(target=self._bg_refresh, name="manual-refresh", daemon=True).start()

    def _bg_refresh(self):
        try:
            self.codex_poller.fetch_now()
            self.loader.reload()
            self.codex_loader.reload()
            self.cursor_loader.reload()
            self.poller.fetch_now()
            self.cursor_poller.fetch_now()
        finally:
            self.request_ui_update()
