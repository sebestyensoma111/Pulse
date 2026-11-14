from collections import deque
from sys import platform
from threading import Event
from time import monotonic

from pulse.core.hardware import Hardware, read_hardware
from pulse.core.monitor import Monitor, Snapshot
from pulse.core.temperatures import read_sensors
from pulse.ui.qt import QtCore, QtGui, QtWidgets, Signal, Slot

Qt = QtCore.Qt

METRICS = ("CPU", "Memory", "Storage", "Network")
COLORS = ("#70e0bd", "#a2aeff", "#ffd08a", "#7ccfff")
HARDWARE_PAGE = len(METRICS) + 1

STYLE = """
QWidget { background: #10151d; color: #e6edf5; font-size: 14px; }
QLabel#title { font-size: 30px; font-weight: 700; }
QLabel#muted { color: #a6b2c1; }
QLabel#reading { font-size: 23px; font-weight: 600; }
QPushButton, QLineEdit { background: #222f40; border: 1px solid #394b61;
    border-radius: 6px; padding: 8px 12px; }
QPushButton:hover, QPushButton:focus, QLineEdit:focus { border-color: #70e0bd; }
QPushButton#card { background: #1a2330; border-radius: 10px; }
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
        self.page = 0

    def run(self) -> None:
        monitor = Monitor()
        while not self.stopped.is_set():
            started = monotonic()
            page = self.page
            self.sampled.emit(monitor.sample(
                include_processes=page in (1, 2),
                include_volumes=page == 3,
                include_interfaces=page == 4,
            ))
            self.stopped.wait(max(0.0, 1.0 - (monotonic() - started)))


class HardwareSampler(QtCore.QThread):
    hardware_ready = Signal(object)
    sensors_ready = Signal(object)

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.stopped = Event()

    def run(self) -> None:
        hardware = read_hardware()
        self.hardware_ready.emit(hardware)
        interval = 10 if platform == "win32" else 2
        while not self.stopped.is_set():
            self.sensors_ready.emit(read_sensors(hardware))
            self.stopped.wait(interval)


class Sparkline(QtWidgets.QWidget):
    def __init__(self, color: str, fixed_scale: bool) -> None:
        super().__init__()
        self.color = QtGui.QColor(color)
        self.fixed_scale = fixed_scale
        self.points: deque[tuple[float, float | None]] = deque(maxlen=60)
        self.setMinimumHeight(48)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)

    def append(self, timestamp: float, value: float | None) -> None:
        self.points.append((timestamp, value))
        while self.points and self.points[0][0] < timestamp - 60:
            self.points.popleft()
        self.update()

    def paintEvent(self, event) -> None:
        if not self.points:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(self.color, 2))
        peak = 100.0 if self.fixed_scale else max(
            1.0, *(value or 0 for _, value in self.points)
        )
        newest = self.points[-1][0]
        path = QtGui.QPainterPath()
        connected = False
        for timestamp, value in self.points:
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


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Pulse System Monitor")
        self.resize(980, 670)
        self.setMinimumSize(600, 500)
        self.setStyleSheet(STYLE)
        self.snapshot: Snapshot | None = None
        self.hardware = Hardware()
        self.animation: QtCore.QPropertyAnimation | None = None
        self.closing = False
        self.stack = QtWidgets.QStackedWidget()
        self.setCentralWidget(self.stack)
        self.readings: list[QtWidgets.QLabel] = []
        self.notes: list[QtWidgets.QLabel] = []
        self.charts: list[Sparkline] = []
        self.detail_titles: list[QtWidgets.QLabel] = []
        self.detail_notes: list[QtWidgets.QLabel] = []
        self.tables: list[DataTable] = []
        self.hardware_tables: dict[int, DataTable] = {}
        self.hardware_notes: dict[int, QtWidgets.QLabel] = {}
        self.filters: dict[int, QtWidgets.QLineEdit] = {}
        self.build_dashboard()
        for index in range(len(METRICS)):
            self.build_detail(index)
        self.build_hardware_page()
        self.statusBar().showMessage("Sampling system metrics…")
        self.sampler = Sampler(self)
        self.sampler.sampled.connect(self.update_snapshot)
        self.hardware_sampler = HardwareSampler(self)
        self.hardware_sampler.hardware_ready.connect(self.update_hardware)
        self.hardware_sampler.sensors_ready.connect(self.update_sensors)
        self.workers = (self.sampler, self.hardware_sampler)
        for worker in self.workers:
            worker.finished.connect(self.finish_close, Qt.ConnectionType.QueuedConnection)
            worker.start()

    def build_dashboard(self) -> None:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(14)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(label("Pulse", "title"), 1)
        hardware = QtWidgets.QPushButton("Hardware →")
        hardware.clicked.connect(lambda: self.navigate(HARDWARE_PAGE))
        top.addWidget(hardware)
        layout.addLayout(top)
        layout.addWidget(label("System overview", "muted"))
        clock_bar = QtWidgets.QHBoxLayout()
        self.clock_summary = label("Reading CPU and GPU clocks…", "muted")
        self.clock_summary.setWordWrap(True)
        clock_bar.addWidget(self.clock_summary, 1)
        clocks = QtWidgets.QPushButton("Clocks →")
        clocks.clicked.connect(self.open_clocks)
        clock_bar.addWidget(clocks)
        layout.addLayout(clock_bar)
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
            content.setContentsMargins(16, 12, 16, 12)
            title = label(f"{metric}  →", "metric")
            reading = label("—", "reading")
            note = label("Waiting for first sample", "muted")
            note.setWordWrap(True)
            chart = Sparkline(COLORS[index], index < 2)
            for widget in (title, reading, chart, note):
                widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                content.addWidget(widget)
            self.readings.append(reading)
            self.notes.append(note)
            self.charts.append(chart)
            grid.addWidget(card, index // 2, index % 2)
        layout.addLayout(grid, 1)
        self.stack.addWidget(page)

    def build_detail(self, index: int) -> None:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(24, 18, 24, 18)
        back = QtWidgets.QPushButton("← Overview")
        back.clicked.connect(lambda: self.navigate(0))
        layout.addWidget(back, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(label(METRICS[index], "title"))
        reading = label("—", "reading")
        note = label("Waiting for first sample", "muted")
        note.setWordWrap(True)
        self.detail_titles.append(reading)
        self.detail_notes.append(note)
        layout.addWidget(reading)
        layout.addWidget(note)
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
            self.hardware_tabs, "Temperatures", "Temperature", "Readings depend on hardware and driver support.",
        )
        self.clock_table = self.add_sensor_tab(
            self.hardware_tabs, "Clocks", "Frequency",
            "Live clocks depend on driver support. RAM rates are configured or rated specifications.",
        )
        self.stack.addWidget(page)

    def open_clocks(self) -> None:
        self.hardware_tabs.setCurrentWidget(self.clock_table.parentWidget())
        self.navigate(HARDWARE_PAGE)

    def add_sensor_tab(self, tabs: QtWidgets.QTabWidget, title: str, unit: str, description: str) -> DataTable:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        note = label(description, "muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        table = DataTable(("Device", "Sensor", unit, "Source"))
        table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
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
        self.sampler.page = page
        if 0 < page < HARDWARE_PAGE:
            self.update_detail(page - 1)
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
            else "Memory module details are unavailable. Total usable RAM is shown above."
        )
        self.hardware_notes[2].setText("Physical drive details are unavailable.")
        self.hardware_notes[2].setVisible(not hardware.drives)
        self.notes[2].setText(self.storage_note())

    @Slot(object)
    def update_sensors(self, readings) -> None:
        temperatures, clocks = readings
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
        self.temperature_table.update_rows(
            ((reading.device, reading.device.casefold()), (reading.sensor, reading.sensor.casefold()),
             (f"{reading.celsius:.1f} °C", reading.celsius), (reading.source, reading.source))
            for reading in temperatures
        )
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
            ("CPU", "CPU frequency", bool(cpu)),
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
        memory = snapshot.memory
        read, _ = snapshot.disk
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
        for index, value in enumerate((snapshot.cpu, memory.percent if memory else None, read, received)):
            self.readings[index].setText(readings[index])
            self.notes[index].setText(notes[index])
            self.charts[index].append(now, value)
        if 0 < self.stack.currentIndex() < HARDWARE_PAGE:
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

    def closeEvent(self, event) -> None:
        if any(worker.isRunning() for worker in self.workers):
            event.ignore()
            self.closing = True
            for worker in self.workers:
                worker.stopped.set()
            return
        super().closeEvent(event)

    @Slot()
    def finish_close(self) -> None:
        if self.closing and not any(worker.isRunning() for worker in self.workers):
            self.close()
