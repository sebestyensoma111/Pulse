# made by Péter Noé

from collections import deque
from dataclasses import replace
from sys import platform
from threading import Event
from time import monotonic

from pulse.core.clocks import Clock
from pulse.core.gpu import Gpu
from pulse.core.hardware import Drive, Hardware, linux_drives, read_hardware
from pulse.core.monitor import Monitor, Snapshot
from pulse.core.smart import SmartReport, read_smart
from pulse.core.temperatures import LinuxThermals, TemperatureStats, read_sensors
from pulse.ui.qt import QtCore, QtGui, QtWidgets, Signal, Slot

Qt = QtCore.Qt

METRICS = ("CPU", "Memory", "Storage", "Network")
COLORS = ("#70e0bd", "#a2aeff", "#ffd08a", "#7ccfff")
HARDWARE_PAGE = len(METRICS) + 1
SMART_PAGE = HARDWARE_PAGE + 1
RATE_CHANNELS = (("Read", "Write"), ("Download", "Upload"))
RATE_ACCENTS = ("#ffd08a", "#c2a5ff")

STYLE = """
QWidget { background: #10151d; color: #e6edf5; font-size: 14px; }
QWidget#shell { background: #0b1018; }
QWidget#workspace { background: #10151d; border: 1px solid #35475c; border-radius: 15px; }
QStackedWidget#pages, QWidget#page { background: transparent; }
QLabel#brand { background: transparent; font-size: 22px; font-weight: 700; }
QLabel#brandMark { background: #203d38; color: #a7f3d7; border: 1px solid #46776a;
    border-radius: 8px; font-size: 22px; font-weight: 700; }
QLabel#location { background: transparent; color: #bbc8d8; }
QWidget#titleBar { background: transparent; }
QToolButton[windowControl="true"] { background: transparent; color: #e6edf5;
    border: 1px solid transparent; border-radius: 6px; font-size: 17px; }
QToolButton[windowControl="true"]:hover { background: #29384a; }
QToolButton[windowControl="true"]:focus { border-color: #70e0bd; }
QToolButton#closeWindow:hover { background: #a62e43; color: #ffffff; }
QWidget#clockPanel { background: #172331; border: 1px solid #35475c; border-radius: 10px; }
QLabel#clockSummary { background: transparent; color: #d0dfed; }
QStatusBar, QStatusBar QLabel { background: #0b1018; color: #a6b2c1; }
QLabel#title { font-size: 30px; font-weight: 700; }
QLabel#muted { color: #a6b2c1; }
QLabel#reading { font-size: 23px; font-weight: 600; }
QPushButton, QLineEdit, QComboBox { background: #222f40; border: 1px solid #394b61;
    border-radius: 6px; padding: 8px 12px; }
QPushButton:disabled { color: #8492a5; }
QComboBox QAbstractItemView { background: #172331; selection-background-color: #285b73; }
QPushButton:hover, QPushButton:focus, QLineEdit:focus { border-color: #70e0bd; }
QPushButton#card { background: #1a2330; border-radius: 12px; }
QPushButton#card:hover, QPushButton#card:focus { background: #202d3d; }
QPushButton#card QLabel { background: transparent; }
QLabel#metric { font-weight: 600; }
QTableView { background-color: #151d28; alternate-background-color: #222e3e;
    color: #e6edf5; selection-background-color: #285b73; selection-color: #ffffff;
    border: 1px solid #3d4f65; gridline-color: #35465a; }
QTableView::item:selected { background-color: #285b73; color: #ffffff; }
QHeaderView::section { background: #2b3b50; color: #f2f6fc;
    border: 0; border-right: 1px solid #3d4f65; padding: 7px; }
QTableCornerButton::section { background: #2b3b50; border: 0; }
QTabWidget::pane { border: 1px solid #3d4f65; }
QTabBar::tab { background: #222e3e; color: #d1dceb; padding: 8px 14px; }
QTabBar::tab:selected { background: #285b73; color: #ffffff; }
QTabBar::tab:hover:!selected { background: #2b3b50; }
"""


def label(text: str, role: str = "") -> QtWidgets.QLabel:
    widget = QtWidgets.QLabel(text)
    widget.setObjectName(role)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


def size(value: float | None, rate: bool = False) -> str:
    if value is None:
        return "—"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024 or unit == "PiB":
            return f"{value:.1f} {unit}" + ("/s" if rate else "")
        value /= 1024
    raise AssertionError("Missing size unit")


def percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def numeric_cell(value: float | None, unit: str, precision: int = 1) -> tuple[str, float]:
    if value is None:
        return "—", float("-inf")
    return f"{value:.{precision}f} {unit}", value


def uptime_text(seconds: int | None) -> str:
    if seconds is None:
        return "Uptime · —"
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"Uptime · {days}d {duration}" if days else f"Uptime · {duration}"


def drive_capacity(value: int | None) -> str:
    if value is None:
        return "—"
    for unit, scale in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6)):
        if value >= scale:
            return f"{value / scale:.3g} {unit}"
    return size(value)


class Sampler(QtCore.QThread):
    sampled = Signal(object)

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.stopped = Event()
        self.refresh = Event()
        self.view = (0, True)

    def set_view(self, page: int, visible: bool) -> None:
        view = (page if visible else 0, visible)
        if self.view != view:
            self.view = view
            self.refresh.set()

    def stop(self) -> None:
        self.stopped.set()
        self.refresh.set()

    def run(self) -> None:
        monitor = Monitor()
        while True:
            self.refresh.clear()
            if self.stopped.is_set():
                break
            started = monotonic()
            page, visible = self.view
            self.sampled.emit(monitor.sample(
                include_processes=page in (1, 2),
                include_volumes=page == 3,
                include_interfaces=page == 4,
            ))
            interval = 1 if visible else 5
            self.refresh.wait(max(0.0, interval - (monotonic() - started)))


class HardwareSampler(QtCore.QThread):
    hardware_ready = Signal(object)
    sensors_ready = Signal(object)

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.stopped = Event()
        self.refresh = Event()
        self.interval = 10 if platform == "win32" else 4

    def set_view(self, visible: bool, live_sensors: bool) -> None:
        if platform == "win32":
            interval = 10 if visible else 20
        else:
            interval = 2 if live_sensors else 4 if visible else 10
        if interval != self.interval:
            self.interval = interval
            self.refresh.set()

    def stop(self) -> None:
        self.stopped.set()
        self.refresh.set()

    def run(self) -> None:
        hardware = read_hardware()
        self.hardware_ready.emit(hardware)
        thermals = LinuxThermals() if platform == "linux" else None
        rescan_at = monotonic() + 15
        while True:
            self.refresh.clear()
            if self.stopped.is_set():
                break
            started = monotonic()
            if platform == "linux" and started >= rescan_at:
                drives = linux_drives()
                if drives is not None and drives != hardware.drives:
                    hardware = replace(hardware, drives=drives)
                    self.hardware_ready.emit(hardware)
                rescan_at = started + 15
            self.sensors_ready.emit(read_sensors(hardware, thermals))
            self.refresh.wait(max(0.0, self.interval - (monotonic() - started)))


class SmartReader(QtCore.QThread):
    report_ready = Signal(object)

    def __init__(self, parent: QtWidgets.QWidget, drive: Drive) -> None:
        super().__init__(parent)
        self.drive = drive

    def run(self) -> None:
        self.report_ready.emit(read_smart(self.drive))


class Sparkline(QtWidgets.QWidget):
    def __init__(self, color: str, fixed_scale: bool, secondary_color: str | None = None) -> None:
        super().__init__()
        self.colors = tuple(QtGui.QColor(color) for color in (color, secondary_color) if color)
        self.fixed_scale = fixed_scale
        self.points: deque[tuple[float, tuple[float | None, ...]]] = deque(maxlen=61)
        self.setMinimumHeight(48)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)

    def append(self, timestamp: float, *values: float | None) -> None:
        self.points.append((timestamp, values))
        while self.points and self.points[0][0] < timestamp - 60:
            self.points.popleft()
        if self.isVisible():
            self.update()

    def paintEvent(self, event) -> None:
        if not self.points:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        peak = 100.0 if self.fixed_scale else max(
            (value for _, values in self.points for value in values if value is not None), default=1.0,
        )
        peak = max(1.0, peak)
        newest = self.points[-1][0]
        for index, color in enumerate(self.colors):
            pen = QtGui.QPen(color, 2)
            if index == 1:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            path = QtGui.QPainterPath()
            connected = False
            for timestamp, values in self.points:
                value = values[index]
                if value is None:
                    connected = False
                    continue
                point = QtCore.QPointF(
                    2 + (1 - (newest - timestamp) / 60) * (self.width() - 4),
                    2 + (1 - min(value / peak, 1.0)) * (self.height() - 4),
                )
                if connected:
                    path.lineTo(point)
                else:
                    path.moveTo(point)
                    painter.drawPoint(point)
                connected = True
            painter.drawPath(path)


class TableModel(QtCore.QAbstractTableModel):
    def __init__(self, headers: tuple[str, ...], parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.headers = headers
        self.rows = ()
        self.sorted_rows = ()
        self.sort_column = 0
        self.sort_order = Qt.SortOrder.AscendingOrder

    def rowCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.sorted_rows)

    def columnCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return self.sorted_rows[index.row()][index.column()][0]
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        return None

    def sort(self, column, order=Qt.SortOrder.AscendingOrder) -> None:
        self.sort_column = column
        self.sort_order = order
        self.refresh()

    def update_rows(self, rows) -> None:
        rows = tuple(rows)
        if rows != self.rows:
            self.rows = rows
            self.refresh()

    def refresh(self) -> None:
        rows = tuple(sorted(
            self.rows, key=lambda row: row[self.sort_column][1],
            reverse=self.sort_order == Qt.SortOrder.DescendingOrder,
        ))
        if rows == self.sorted_rows:
            return
        if len(rows) != len(self.sorted_rows):
            self.beginResetModel()
            self.sorted_rows = rows
            self.endResetModel()
            return
        self.sorted_rows = rows
        self.dataChanged.emit(self.index(0, 0), self.index(len(rows) - 1, len(self.headers) - 1))


class DataTable(QtWidgets.QTableView):
    def __init__(self, headers: tuple[str, ...]) -> None:
        super().__init__()
        self.setModel(TableModel(headers, self))
        self.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.verticalHeader().hide()
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.horizontalHeader().setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        self.setSortingEnabled(True)

    def update_rows(self, rows) -> None:
        self.model().update_rows(rows)


class TitleBar(QtWidgets.QWidget):
    def __init__(self, window: QtWidgets.QMainWindow) -> None:
        super().__init__(window)
        self.setObjectName("titleBar")
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        mark = label("∿", "brandMark")
        mark.setFixedSize(34, 34)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.location = label("Overview", "location")
        for text in (mark, label("Pulse", "brand")):
            text.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(text)
        layout.addStretch()
        self.location.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.location)

        minimize = self.control("−", "Minimize")
        minimize.clicked.connect(window.showMinimized)
        self.maximize = self.control("□", "Maximize")
        self.maximize.clicked.connect(self.toggle_maximized)
        close = self.control("×", "Close")
        close.setObjectName("closeWindow")
        close.clicked.connect(window.close)
        for button in (minimize, self.maximize, close):
            layout.addWidget(button)

    def control(self, text: str, name: str) -> QtWidgets.QToolButton:
        button = QtWidgets.QToolButton(self)
        button.setProperty("windowControl", True)
        button.setText(text)
        button.setToolTip(name)
        button.setAccessibleName(name)
        button.setFixedSize(34, 34)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        return button

    def toggle_maximized(self) -> None:
        window = self.window()
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()

    def mousePressEvent(self, event) -> None:
        handle = self.window().windowHandle()
        if event.button() == Qt.MouseButton.LeftButton and handle and handle.startSystemMove():
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class ResizeHandle(QtWidgets.QWidget):
    def __init__(self, window: QtWidgets.QWidget, edges, cursor) -> None:
        super().__init__(window)
        self.edges = edges
        self.setCursor(cursor)
        self.setStyleSheet("background: transparent")

    def mousePressEvent(self, event) -> None:
        handle = self.window().windowHandle()
        if event.button() == Qt.MouseButton.LeftButton and handle and handle.startSystemResize(self.edges):
            event.accept()
            return
        super().mousePressEvent(event)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.resize_handles: list[ResizeHandle] = []
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle("Pulse System Monitor")
        self.resize(980, 670)
        self.setMinimumSize(600, 500)
        self.setStyleSheet(STYLE)
        self.snapshot: Snapshot | None = None
        self.hardware = Hardware()
        self.latest_clocks: tuple[Clock, ...] | None = None
        self.latest_gpus: tuple[Gpu, ...] = ()
        self.temperature_history: dict[tuple[str, str, str], TemperatureStats] = {}
        self.current_temperatures: set[tuple[str, str, str]] = set()
        self.tray = None
        self.animation: QtCore.QPropertyAnimation | None = None
        self.closing = False
        shell = QtWidgets.QWidget()
        shell.setObjectName("shell")
        self.setCentralWidget(shell)
        shell_layout = QtWidgets.QVBoxLayout(shell)
        shell_layout.setContentsMargins(14, 10, 14, 4)
        shell_layout.setSpacing(10)

        self.title_bar = TitleBar(self)
        shell_layout.addWidget(self.title_bar)

        workspace = QtWidgets.QWidget()
        workspace.setObjectName("workspace")
        frame_layout = QtWidgets.QVBoxLayout(workspace)
        frame_layout.setContentsMargins(1, 1, 1, 1)
        self.stack = QtWidgets.QStackedWidget()
        self.stack.setObjectName("pages")
        frame_layout.addWidget(self.stack)
        shell_layout.addWidget(workspace, 1)
        self.readings: list[QtWidgets.QLabel] = []
        self.notes: list[QtWidgets.QLabel] = []
        self.charts: list[Sparkline] = []
        self.detail_titles: list[QtWidgets.QLabel] = []
        self.detail_notes: list[QtWidgets.QLabel] = []
        self.tables: list[DataTable] = []
        self.hardware_tables: dict[int, DataTable] = {}
        self.hardware_notes: dict[int, QtWidgets.QLabel] = {}
        self.filters: dict[int, QtWidgets.QLineEdit] = {}
        self.rate_charts: dict[int, Sparkline] = {}
        self.build_dashboard()
        for index in range(len(METRICS)):
            self.build_detail(index)
        self.build_hardware_page()
        self.build_smart_page()
        self.setup_tray()
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().showMessage("Sampling system metrics…")
        edge = Qt.Edge
        cursor = Qt.CursorShape
        self.resize_handles = [
            ResizeHandle(self, edges, shape)
            for edges, shape in (
                (edge.TopEdge, cursor.SizeVerCursor),
                (edge.BottomEdge, cursor.SizeVerCursor),
                (edge.LeftEdge, cursor.SizeHorCursor),
                (edge.RightEdge, cursor.SizeHorCursor),
                (edge.TopEdge | edge.LeftEdge, cursor.SizeFDiagCursor),
                (edge.TopEdge | edge.RightEdge, cursor.SizeBDiagCursor),
                (edge.BottomEdge | edge.LeftEdge, cursor.SizeBDiagCursor),
                (edge.BottomEdge | edge.RightEdge, cursor.SizeFDiagCursor),
            )
        ]
        for handle in self.resize_handles:
            handle.raise_()
        self.update_window_frame()
        self.sampler = Sampler(self)
        self.sampler.sampled.connect(self.update_snapshot)
        self.hardware_sampler = HardwareSampler(self)
        self.hardware_sampler.hardware_ready.connect(self.update_hardware)
        self.hardware_sampler.sensors_ready.connect(self.update_sensors)
        self.hardware_tabs.currentChanged.connect(self.sensor_tab_changed)
        self.workers = (self.sampler, self.hardware_sampler)
        for worker in self.workers:
            worker.finished.connect(self.finish_close, Qt.ConnectionType.QueuedConnection)
            worker.start()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.update_window_frame()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.Type.WindowStateChange:
            self.update_window_frame()
            self.sync_sampling_view()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.sync_sampling_view()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.sync_sampling_view()

    def sync_sampling_view(self) -> None:
        visible = self.isVisible() and not self.isMinimized()
        page = self.stack.currentIndex()
        self.sampler.set_view(page, visible)
        live_sensors = visible and page == HARDWARE_PAGE and self.hardware_tabs.currentIndex() != 0
        self.hardware_sampler.set_view(visible, live_sensors)

    def update_window_frame(self) -> None:
        if not self.resize_handles:
            return
        maximized = self.isMaximized()
        button = self.title_bar.maximize
        name = "Restore" if maximized else "Maximize"
        button.setText("❐" if maximized else "□")
        button.setToolTip(name)
        button.setAccessibleName(name)
        width, height, edge = self.width(), self.height(), 6
        for handle, rect in zip(self.resize_handles, (
            (edge, 0, width - 2 * edge, edge),
            (edge, height - edge, width - 2 * edge, edge),
            (0, edge, edge, height - 2 * edge),
            (width - edge, edge, edge, height - 2 * edge),
            (0, 0, edge, edge),
            (width - edge, 0, edge, edge),
            (0, height - edge, edge, edge),
            (width - edge, height - edge, edge, edge),
        )):
            handle.setGeometry(*rect)
            handle.setVisible(not maximized and not self.isFullScreen())

    def build_dashboard(self) -> None:
        page = QtWidgets.QWidget()
        page.setObjectName("page")
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(14)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(label("System overview", "title"), 1)
        hardware = QtWidgets.QPushButton("Hardware →")
        hardware.clicked.connect(lambda: self.navigate(HARDWARE_PAGE))
        top.addWidget(hardware)
        gpu = QtWidgets.QPushButton("GPU →")
        gpu.clicked.connect(self.open_gpu)
        top.addWidget(gpu)
        layout.addLayout(top)
        clock_panel = QtWidgets.QWidget()
        clock_panel.setObjectName("clockPanel")
        clock_bar = QtWidgets.QHBoxLayout(clock_panel)
        clock_bar.setContentsMargins(12, 6, 6, 6)
        self.clock_summary = label("Reading CPU and GPU clocks…", "clockSummary")
        self.clock_summary.setWordWrap(True)
        clock_bar.addWidget(self.clock_summary, 1)
        self.uptime_label = label(uptime_text(None), "clockSummary")
        clock_bar.addWidget(self.uptime_label)
        clocks = QtWidgets.QPushButton("Clocks →")
        clocks.clicked.connect(self.open_clocks)
        clock_bar.addWidget(clocks)
        layout.addWidget(clock_panel)
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(14)
        for index, metric in enumerate(METRICS):
            card = QtWidgets.QPushButton()
            card.setObjectName("card")
            card.setAccessibleName(f"Open {metric.lower()} details")
            card.setToolTip(f"Open {metric.lower()} details")
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            card.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
            card.clicked.connect(lambda checked=False, i=index: self.navigate(i + 1))
            content = QtWidgets.QVBoxLayout(card)
            content.setContentsMargins(18, 14, 18, 14)
            title = label(f"{metric}  →", "metric")
            title.setStyleSheet(f"color: {COLORS[index]}")
            reading = label("—", "reading")
            note = label("Waiting for first sample", "muted")
            note.setWordWrap(True)
            chart = Sparkline(COLORS[index], index < 2, RATE_ACCENTS[index - 2] if index >= 2 else None)
            for widget in (title, reading, chart, note):
                widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                content.addWidget(widget)
            if index >= 2:
                content.addLayout(self.rate_legend(index))
            self.readings.append(reading)
            self.notes.append(note)
            self.charts.append(chart)
            grid.addWidget(card, index // 2, index % 2)
        layout.addLayout(grid, 1)
        self.stack.addWidget(page)

    def rate_legend(self, index: int) -> QtWidgets.QHBoxLayout:
        legend = QtWidgets.QHBoxLayout()
        for name, mark, color in zip(RATE_CHANNELS[index - 2], ("━", "┄"), (COLORS[index], RATE_ACCENTS[index - 2])):
            item = label(f"{mark} {name}")
            item.setStyleSheet(f"background: transparent; color: {color}")
            item.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            legend.addWidget(item)
        legend.addStretch()
        return legend

    def build_detail(self, index: int) -> None:
        page = QtWidgets.QWidget()
        page.setObjectName("page")
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(24, 18, 24, 18)
        back = QtWidgets.QPushButton("← Overview")
        back.clicked.connect(lambda: self.navigate(0))
        layout.addWidget(back, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(label(METRICS[index], "title"))
        if index == 2:
            smart = QtWidgets.QPushButton("SMART →")
            smart.clicked.connect(lambda: self.navigate(SMART_PAGE))
            layout.addWidget(smart, alignment=Qt.AlignmentFlag.AlignLeft)
        reading = label("—", "reading")
        note = label("Waiting for first sample", "muted")
        note.setWordWrap(True)
        self.detail_titles.append(reading)
        self.detail_notes.append(note)
        layout.addWidget(reading)
        layout.addWidget(note)
        if index >= 2:
            chart = Sparkline(COLORS[index], False, RATE_ACCENTS[index - 2])
            chart.points = self.charts[index].points
            chart.setMaximumHeight(105)
            self.rate_charts[index] = chart
            layout.addWidget(chart)
            layout.addLayout(self.rate_legend(index))
        if index in (1, 2):
            memory = index == 1
            layout.addWidget(label("Memory modules" if memory else "Physical drives", "metric"))
            hardware_note = label("Reading hardware information…", "muted")
            hardware_note.setWordWrap(True)
            layout.addWidget(hardware_note)
            hardware_table = DataTable(
                ("Slot", "Model", "Type", "Capacity", "Speed") if memory
                else ("Device", "Model", "Type", "Capacity")
            )
            hardware_table.setMinimumHeight(85)
            hardware_table.setMaximumHeight(160)
            header = hardware_table.horizontalHeader()
            for column in range(hardware_table.model().columnCount()):
                if column != 1:
                    header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
            layout.addWidget(hardware_table)
            hardware_table.hide()
            self.hardware_notes[index] = hardware_note
            self.hardware_tables[index] = hardware_table
            if not memory:
                layout.addWidget(label("Mounted volumes", "metric"))
        if index < 2:
            field = QtWidgets.QLineEdit()
            palette = field.palette()
            palette.setColor(QtGui.QPalette.ColorRole.PlaceholderText, QtGui.QColor("#b8c7d9"))
            field.setPalette(palette)
            field.setPlaceholderText("Filter processes by name or PID")
            field.setAccessibleName("Filter processes")
            field.textChanged.connect(lambda _text, i=index: self.update_detail(i))
            self.filters[index] = field
            layout.addWidget(field)
            headers = ("Process", "PID", "CPU share", "RAM (RSS)")
        elif index == 2:
            headers = ("Mount", "Filesystem", "Used", "Capacity")
        else:
            headers = ("Interface", "Received", "Sent")
        table = DataTable(headers)
        self.tables.append(table)
        if index < 2:
            table.sortByColumn(index + 2, Qt.SortOrder.DescendingOrder)
        layout.addWidget(table, 1)
        self.stack.addWidget(page)

    def build_hardware_page(self) -> None:
        page = QtWidgets.QWidget()
        page.setObjectName("page")
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(24, 18, 24, 18)
        back = QtWidgets.QPushButton("← Overview")
        back.clicked.connect(lambda: self.navigate(0))
        layout.addWidget(back, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(label("Hardware", "title"))
        self.hardware_tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.hardware_tabs, 1)
        self.inventory = DataTable(("Category", "Model", "Identifier", "Driver / specification"))
        self.hardware_tabs.addTab(self.inventory, "Devices")
        self.temperature_table = self.add_sensor_tab(
            self.hardware_tabs, "Temperatures", ("Device", "Sensor", "Current", "Min", "Avg", "Max", "Source"),
            "Session statistics from available sensors. Missing current readings are shown as a dash.",
        )
        self.clock_table = self.add_sensor_tab(
            self.hardware_tabs, "Clocks", ("Device", "Sensor", "Frequency", "Source"),
            "Live clocks depend on driver support. RAM rates are configured or rated specifications.",
        )
        self.gpu_table = self.add_sensor_tab(
            self.hardware_tabs, "GPU", ("GPU", "Adapter", "Load", "VRAM used / total", "Clock", "Temp.", "Power"),
            "Available readings depend on the graphics driver.",
        )
        self.stack.addWidget(page)

    def build_smart_page(self) -> None:
        page = QtWidgets.QWidget()
        page.setObjectName("page")
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(24, 18, 24, 18)
        back = QtWidgets.QPushButton("← Storage")
        back.clicked.connect(lambda: self.navigate(3))
        layout.addWidget(back, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(label("SMART", "title"))
        controls = QtWidgets.QHBoxLayout()
        self.smart_drives = QtWidgets.QComboBox()
        self.smart_drives.setAccessibleName("Physical drive")
        self.smart_drives.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.smart_drives.currentIndexChanged.connect(self.refresh_smart)
        controls.addWidget(self.smart_drives, 1)
        self.smart_refresh = QtWidgets.QPushButton("Refresh")
        self.smart_refresh.setEnabled(False)
        self.smart_refresh.clicked.connect(self.refresh_smart)
        controls.addWidget(self.smart_refresh)
        layout.addLayout(controls)
        self.smart_status = label("Waiting for drive inventory…", "reading")
        self.smart_status.setWordWrap(True)
        layout.addWidget(self.smart_status)
        self.smart_note = label("", "muted")
        self.smart_note.setWordWrap(True)
        self.smart_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.smart_note)
        self.smart_table = DataTable(("Attribute", "Value"))
        layout.addWidget(self.smart_table, 1)
        self.smart_reader: SmartReader | None = None
        self.stack.addWidget(page)

    def refresh_smart(self) -> None:
        if self.stack.currentIndex() != SMART_PAGE or self.closing or self.smart_reader is not None:
            return
        drive = self.smart_drives.currentData()
        if drive is None:
            return
        self.smart_drives.setEnabled(False)
        self.smart_refresh.setEnabled(False)
        self.show_smart_report(SmartReport("Reading drive diagnostics…"))
        self.smart_reader = SmartReader(self, drive)
        self.smart_reader.report_ready.connect(self.apply_smart_report)
        self.smart_reader.finished.connect(self.finish_smart_read)
        self.smart_reader.start()

    @Slot(object)
    def apply_smart_report(self, report: SmartReport) -> None:
        if self.smart_reader is not None and self.smart_reader.drive == self.smart_drives.currentData():
            self.show_smart_report(report)

    @Slot(object)
    def show_smart_report(self, report: SmartReport) -> None:
        self.smart_status.setText(report.status)
        self.smart_note.setText(report.note)
        self.smart_note.setVisible(bool(report.note))
        drive = self.smart_drives.currentData()
        rows = report.rows
        if drive is not None:
            rows = (
                ("Model", drive.model or "—"),
                ("Device", drive.name),
                ("Type", drive.kind or "—"),
                ("Capacity", drive_capacity(drive.capacity)),
            ) + rows
        self.smart_table.update_rows(
            ((name, index), (value, value.casefold())) for index, (name, value) in enumerate(rows)
        )

    @Slot()
    def finish_smart_read(self) -> None:
        drive = self.smart_reader.drive
        self.smart_reader.deleteLater()
        self.smart_reader = None
        self.smart_drives.setEnabled(self.smart_drives.count() > 0)
        self.smart_refresh.setEnabled(self.smart_drives.count() > 0)
        if self.smart_drives.currentData() != drive:
            self.refresh_smart()
        self.finish_close()

    def open_gpu(self) -> None:
        self.hardware_tabs.setCurrentWidget(self.gpu_table.parentWidget())
        self.navigate(HARDWARE_PAGE)

    def open_clocks(self) -> None:
        self.hardware_tabs.setCurrentWidget(self.clock_table.parentWidget())
        self.navigate(HARDWARE_PAGE)

    def add_sensor_tab(self, tabs: QtWidgets.QTabWidget, title: str, headers: tuple[str, ...], description: str) -> DataTable:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        note = label(description, "muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        table = DataTable(headers)
        layout.addWidget(table, 1)
        tabs.addTab(page, title)
        return table

    def navigate(self, page: int) -> None:
        if self.animation is not None:
            old_page = self.stack.currentWidget()
            self.animation.stop()
            self.animation.deleteLater()
            self.animation = None
            old_page.setGraphicsEffect(None)
        self.stack.setCurrentIndex(page)
        self.title_bar.location.setText(("Overview", *METRICS, "Hardware", "SMART")[page])
        self.sensor_tab_changed()
        if 0 < page < HARDWARE_PAGE:
            self.update_detail(page - 1)
        elif page == SMART_PAGE:
            self.refresh_smart()
        current = self.stack.currentWidget()
        effect = QtWidgets.QGraphicsOpacityEffect(current)
        current.setGraphicsEffect(effect)
        animation = QtCore.QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(180)
        animation.setStartValue(0.35)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        animation.finished.connect(lambda: self.finish_transition(current, animation))
        self.animation = animation
        animation.start()

    def finish_transition(self, page: QtWidgets.QWidget, animation: QtCore.QPropertyAnimation) -> None:
        page.setGraphicsEffect(None)
        animation.deleteLater()
        self.animation = None

    @Slot(object)
    def update_hardware(self, hardware: Hardware) -> None:
        self.hardware = hardware
        selected = self.smart_drives.currentData()
        with QtCore.QSignalBlocker(self.smart_drives):
            self.smart_drives.clear()
            for drive in hardware.drives:
                model = drive.model or "Drive"
                self.smart_drives.addItem(f"{model} · {drive.name} · {drive_capacity(drive.capacity)}", drive)
                if drive == selected:
                    self.smart_drives.setCurrentIndex(self.smart_drives.count() - 1)
        ready = bool(hardware.drives) and self.smart_reader is None
        self.smart_drives.setEnabled(ready)
        self.smart_refresh.setEnabled(ready)
        if not hardware.drives:
            self.show_smart_report(SmartReport("No physical drives detected"))
        elif selected != self.smart_drives.currentData():
            self.show_smart_report(SmartReport("Select a drive to read diagnostics"))
            self.refresh_smart()
        inventory = [
            (device.category, device.model, device.identifier, device.driver)
            for device in hardware.devices
        ]
        if hardware.cpu_model:
            inventory.append(("CPU", hardware.cpu_model, "", ""))
        inventory.extend(
            ("RAM", module.model, module.slot,
             " · ".join(part for part in (module.kind, size(module.capacity), module.speed) if part))
            for module in hardware.memory
        )
        inventory.extend(
            ("Storage", drive.model, drive.name,
             " · ".join(part for part in (drive.kind, drive_capacity(drive.capacity)) if part))
            for drive in hardware.drives
        )
        self.inventory.update_rows(
            tuple((field or "—", field.casefold()) for field in row) for row in inventory
        )
        for index, devices, format_capacity in (
            (1, hardware.memory, size), (2, hardware.drives, drive_capacity),
        ):
            table = self.hardware_tables[index]
            rows = []
            for device in devices:
                fields = (device.slot if index == 1 else device.name, device.model, device.kind)
                cells = [(field or "—", field.casefold()) for field in fields]
                cells.append((format_capacity(device.capacity), device.capacity or 0))
                if index == 1:
                    cells.append((device.speed or "—", device.speed))
                rows.append(cells)
            table.update_rows(rows)
            table.setVisible(bool(rows))
        self.hardware_notes[1].setText(
            "CAS latency (CL) unavailable" if hardware.memory
            else (hardware.memory_note or "Memory module details are unavailable.")
            + " Total usable RAM is shown above."
        )
        self.hardware_notes[2].setText("Physical drive details are unavailable.")
        self.hardware_notes[2].setVisible(not hardware.drives)
        self.notes[2].setText(self.storage_note())

    @Slot(object)
    def update_sensors(self, readings) -> None:
        temperatures, clocks, gpus = readings
        self.latest_gpus = gpus
        self.current_temperatures = set()
        for reading in temperatures:
            key = (reading.device, reading.sensor, reading.source)
            if key in self.current_temperatures:
                continue
            self.current_temperatures.add(key)
            stats = self.temperature_history.get(key)
            if stats is None:
                value = reading.celsius
                self.temperature_history[key] = TemperatureStats(reading, value, value, value)
            else:
                stats.add(reading)
        self.latest_clocks = clocks
        cpu = [reading for reading in clocks if reading.category == "CPU"]
        graphics = [reading.mhz for reading in clocks
                    if reading.category == "GPU" and reading.channel.startswith("Graphics")]
        cpu_text = "unavailable"
        if cpu:
            low, high = min(item.mhz for item in cpu), max(item.mhz for item in cpu)
            cpu_text = f"{low:.0f} MHz" if round(low) == round(high) else f"{low:.0f}–{high:.0f} MHz"
            if any("nominal" in item.source for item in cpu):
                cpu_text += " (OS reported)"
        gpu_text = f"{max(graphics):.0f} MHz" if graphics else "unavailable"
        self.clock_summary.setText(f"CPU · {cpu_text}     GPU · {gpu_text}")
        self.update_sensor_view()

    def sensor_view_is_open(self, table: DataTable) -> bool:
        return (self.isVisible() and not self.isMinimized() and self.stack.currentIndex() == HARDWARE_PAGE
                and self.hardware_tabs.currentWidget() is table.parentWidget())

    def sensor_tab_changed(self) -> None:
        self.sync_sampling_view()
        self.update_sensor_view()

    def update_sensor_view(self) -> None:
        if self.sensor_view_is_open(self.temperature_table):
            rows = []
            for key, stats in self.temperature_history.items():
                device, sensor, source = key
                current = stats.latest.celsius if key in self.current_temperatures else None
                rows.append((
                    (device, device.casefold()), (sensor, sensor.casefold()),
                    numeric_cell(current, "°C"), numeric_cell(stats.minimum, "°C"),
                    numeric_cell(stats.average, "°C"), numeric_cell(stats.maximum, "°C"), (source, source),
                ))
            self.temperature_table.update_rows(rows)
        elif self.latest_clocks is not None and self.sensor_view_is_open(self.clock_table):
            self.update_clock_table(self.latest_clocks)
        elif self.sensor_view_is_open(self.gpu_table):
            self.gpu_table.update_rows(
                ((gpu.name, gpu.name.casefold()), (gpu.address or "—", gpu.address),
                 numeric_cell(gpu.load, "%"), (f"{size(gpu.used)} / {size(gpu.total)}", gpu.used or 0),
                 numeric_cell(gpu.clock, "MHz", 0), numeric_cell(gpu.temperature, "°C"),
                 numeric_cell(gpu.power, "W"))
                for gpu in self.latest_gpus
            )

    def update_clock_table(self, clocks: tuple[Clock, ...]) -> None:
        clock_rows = [
            ((reading.device, reading.device.casefold()), (reading.channel, reading.channel.casefold()),
             (f"{reading.mhz:.0f} MHz", reading.mhz), (reading.source, reading.source))
            for reading in clocks
        ]
        for module in self.hardware.memory:
            model = module.model or module.slot or "RAM"
            channel = f"RAM transfer rate · {module.slot}" if module.slot else "RAM transfer rate"
            clock_rows.append(
                ((model, model.casefold()), (channel, channel.casefold()),
                 (module.speed or "—", -1.0), ("Firmware specification", "firmware specification"))
            )
        for category, channel, present in (
            ("CPU", "CPU frequency", any(item.category == "CPU" for item in clocks)),
            ("GPU", "GPU frequency", any(item.category == "GPU" for item in clocks)),
            ("RAM", "RAM transfer rate", bool(self.hardware.memory)),
        ):
            if not present:
                clock_rows.append(
                    ((category, category.casefold()), (channel, channel.casefold()),
                     ("Unavailable", -1.0), ("—", ""))
                )
        self.clock_table.update_rows(clock_rows)

    def storage_note(self) -> str:
        written = self.snapshot.disk[1] if self.snapshot else None
        note = f"Read · Write {size(written, True)}"
        if len(self.hardware.drives) == 1:
            return f"{note}\nDrive capacity · {drive_capacity(self.hardware.drives[0].capacity)}"
        if self.hardware.drives:
            return f"{note}\n{len(self.hardware.drives)} drives"
        return note

    @Slot(object)
    def update_snapshot(self, snapshot: Snapshot) -> None:
        if self.snapshot is None:
            self.statusBar().showMessage("Sampling every second · Charts cover the last 60 seconds")
        self.snapshot = snapshot
        self.uptime_label.setText(uptime_text(snapshot.uptime_seconds))
        memory = snapshot.memory
        read, written = snapshot.disk
        received, sent = snapshot.network
        readings = (percent(snapshot.cpu), percent(memory.percent if memory else None),
                    size(read, True), size(received, True))
        notes = (
            f"{len(snapshot.cores)} logical CPUs" if snapshot.cores else "CPU usage unavailable",
            f"{size(memory.used)} of {size(memory.total)} used" if memory else "Memory unavailable",
            self.storage_note(),
            f"Download · Upload {size(sent, True)}",
        )
        now = monotonic()
        series = ((snapshot.cpu,), (memory.percent if memory else None,), (read, written), (received, sent))
        for index, values in enumerate(series):
            self.readings[index].setText(readings[index])
            self.notes[index].setText(notes[index])
            self.charts[index].append(now, *values)
        for chart in self.rate_charts.values():
            if chart.isVisible():
                chart.update()
        if self.tray is not None and self.tray.isVisible():
            self.tray.setToolTip(f"Pulse\nCPU {readings[0]} · RAM {readings[1]}")
        if self.isVisible() and not self.isMinimized() and 0 < self.stack.currentIndex() < HARDWARE_PAGE:
            self.update_detail(self.stack.currentIndex() - 1)

    def update_detail(self, index: int) -> None:
        snap = self.snapshot
        if snap is None:
            return
        self.detail_titles[index].setText(self.readings[index].text())
        note = self.notes[index].text()
        if index < 2:
            if index == 0 and self.hardware.cpu_model:
                note = f"{self.hardware.cpu_model}\n{note}"
            if index == 0 and snap.cores:
                loads = " · ".join(f"{i + 1}: {percent(load)}" for i, load in enumerate(snap.cores[:8]))
                extra = f" · +{len(snap.cores) - 8} more" if len(snap.cores) > 8 else ""
                note += f"\nLogical CPU load · {loads}{extra}"
            if index == 1 and snap.memory:
                note += f"\nSwap · {size(snap.memory.swap_used)} of {size(snap.memory.swap_total)}"
            query = self.filters[index].text().strip().casefold()
            rows = (
                ((p.name, p.name.casefold()), (str(p.pid), p.pid),
                 (percent(p.cpu), -1 if p.cpu is None else p.cpu),
                 (size(p.memory), -1 if p.memory is None else p.memory))
                for p in snap.processes or () if query in p.name.casefold() or query in str(p.pid)
            )
            if snap.processes is None:
                note += "\nProcess data unavailable"
        elif index == 2:
            if not snap.volumes:
                note += "\nNo mount data available"
            rows = (
                ((v.mount, v.mount.casefold()), (v.filesystem, v.filesystem),
                 (size(v.used), -1 if v.used is None else v.used),
                 (size(v.total), -1 if v.total is None else v.total))
                for v in snap.volumes or ()
            )
        else:
            if not snap.interfaces:
                note += "\nNo interface data available"
            rows = (
                ((n.name, n.name.casefold()), (size(n.received), n.received), (size(n.sent), n.sent))
                for n in snap.interfaces or ()
            )
        self.detail_notes[index].setText(note)
        self.tables[index].update_rows(rows)

    def setup_tray(self) -> None:
        if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = QtGui.QPixmap(32, 32)
        icon.fill(QtGui.QColor("#203d38"))
        painter = QtGui.QPainter(icon)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor("#a7f3d7"), 2))
        path = QtGui.QPainterPath(QtCore.QPointF(4, 16))
        path.lineTo(10, 16)
        path.lineTo(14, 8)
        path.lineTo(18, 24)
        path.lineTo(22, 16)
        path.lineTo(28, 16)
        painter.drawPath(path)
        painter.end()
        self.tray = QtWidgets.QSystemTrayIcon(QtGui.QIcon(icon), self)
        self.tray.setToolTip("Pulse")
        menu = QtWidgets.QMenu(self)
        menu.addAction("Show Pulse", self.restore_from_tray)
        menu.addAction("Quit Pulse", self.close)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)
        button = self.title_bar.control("↓", "Hide to tray")
        button.clicked.connect(self.hide_to_tray)
        self.title_bar.layout().insertWidget(self.title_bar.layout().count() - 3, button)

    def hide_to_tray(self) -> None:
        if self.tray is not None and QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.setToolTip(
                f"Pulse\nCPU {self.readings[0].text()} · RAM {self.readings[1].text()}"
            )
            self.tray.show()
            self.hide()
        else:
            self.showMinimized()

    def tray_activated(self, reason) -> None:
        if reason in (QtWidgets.QSystemTrayIcon.ActivationReason.Trigger,
                      QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick):
            self.restore_from_tray()

    def restore_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.update_sensor_view()
        if self.tray is not None:
            self.tray.hide()

    def closeEvent(self, event) -> None:
        if self.smart_reader is not None or any(worker.isRunning() for worker in self.workers):
            event.ignore()
            self.closing = True
            self.sampler.stop()
            self.hardware_sampler.stop()
            return
        if self.tray is not None:
            self.tray.hide()
        super().closeEvent(event)

    @Slot()
    def finish_close(self) -> None:
        if self.closing and self.smart_reader is None and not any(worker.isRunning() for worker in self.workers):
            self.close()
