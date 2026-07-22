from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from .devices.adam4118 import Adam4118Client, Adam4118MonitorBackend
from .devices.base import ControlCommand
from .devices.brooks0254 import Brooks0254ReadOnlyClient
from .devices.read_only_monitor import ReadOnlyHardwareMonitorBackend
from .devices.serial_transport import PySerialTransaction, SerialSettings
from .devices.simulated import SimulatedBackend
from .legacy import load_legacy_json, load_program
from .models import ProcessProgram, TemperatureMode
from .pid import PIDController, PIDGains
from .runtime import SpecMassRuntime
from .safety import SafetyPolicy
from .state_machine import ControllerStatus, MSMState, ProcessController
from .telemetry import CsvTelemetryWriter, TelemetryWriter
from .tdms_telemetry import TdmsTelemetryWriter, mass_stimuli_from_scan_settings


BACKGROUND = "#f5f7f8"
HEADER_BACKGROUND = "#e7f8fd"
PANEL_BACKGROUND = "#ffffff"
TEXT_COLOR = "#17212b"
MUTED_COLOR = "#5f6b76"
FLOW_COLORS = ("#111111", "#df3e3e", "#27b83f", "#25a9cf")
MASS_COLORS = (
    "#df3e3e",
    "#111111",
    "#25a9cf",
    "#27b83f",
    "#a855f7",
    "#f59e0b",
    "#2563eb",
    "#ec4899",
)


def _create_adam4118_monitor(builds_directory: Path) -> Adam4118MonitorBackend:
    config = load_legacy_json(builds_directory / "data" / "AIOTh")
    module_type = str(config.get("ModuleType", ""))
    if module_type != "4118":
        raise ValueError(f"AIOTh module must be 4118, not {module_type!r}")
    data_format = str(config.get("DataFormat", ""))
    if data_format != "EngineeringUnits":
        raise ValueError(f"AIOTh data format must be EngineeringUnits, not {data_format!r}")
    port = str(config.get("Source", "")).strip()
    if not port:
        raise ValueError("AIOTh Source/COM port is missing")
    timeout_seconds = float(config.get("Timeout", 1000.0)) / 1000.0
    settings = SerialSettings(
        port=port,
        baudrate=int(config.get("BaudRate", 9600)),
        timeout_seconds=max(0.001, timeout_seconds),
        write_timeout_seconds=max(0.001, timeout_seconds),
        read_terminator=b"\r",
    )
    transport = PySerialTransaction(settings, hardware_enabled=True)
    client = Adam4118Client(transport, address=int(config.get("DeviceAddress", 1)))
    names = tuple(str(name) for name in config.get("InputChannels", ()))
    return Adam4118MonitorBackend(client, channel_names=names)


def _create_hardware_monitor(builds_directory: Path) -> ReadOnlyHardwareMonitorBackend:
    temperature_monitor = _create_adam4118_monitor(builds_directory)
    try:
        config = load_legacy_json(builds_directory / "data" / "BrooksDevTh")
        port = str(config.get("Source", "")).strip()
        if not port:
            raise ValueError("BrooksDevTh Source/COM port is missing")
        channels = tuple(str(value) for value in config.get("InputChannels", ()))
        expected_channels = tuple(str(index) for index in range(len(channels)))
        if not channels or len(channels) > 4 or channels != expected_channels:
            raise ValueError(
                "BrooksDevTh InputChannels must be consecutive channels 0 through 3"
            )
        timeout_seconds = float(config.get("Timeout", 1000.0)) / 1000.0
        settings = SerialSettings(
            port=port,
            baudrate=int(config.get("BaudRate", 9600)),
            timeout_seconds=max(0.001, timeout_seconds),
            write_timeout_seconds=max(0.001, timeout_seconds),
            read_terminator=b"\n",
        )
        flow_client = Brooks0254ReadOnlyClient(
            PySerialTransaction(settings, hardware_enabled=True),
            channel_count=len(channels),
        )
        return ReadOnlyHardwareMonitorBackend(
            temperature_monitor,
            flow_client,
            flow_channel_names=tuple(f"Ch{channel}" for channel in channels),
        )
    except Exception:
        temperature_monitor.safe_shutdown()
        raise


def _create_monitor_telemetry(
    path: Path,
    monitor_backend: Adam4118MonitorBackend | ReadOnlyHardwareMonitorBackend,
) -> TelemetryWriter:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing monitor log: {path}")
    temperature_names = tuple(monitor_backend.channel_names)
    flow_count = len(tuple(getattr(monitor_backend, "flow_channel_names", ())))
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return CsvTelemetryWriter(
            path,
            flow_channels=flow_count,
            temperature_names=temperature_names,
        )
    if suffix == ".tdms":
        return TdmsTelemetryWriter(
            path,
            flow_channels=flow_count,
            nominal_increment_seconds=(
                int(getattr(monitor_backend, "poll_interval_ms", 1000)) / 1000.0
            ),
            temperature_names=temperature_names,
            root_properties={
                "SpecMass_Mode": "ReadOnlyHardwareMonitor",
                "SpecMass_OutputCommandsEnabled": 0,
            },
        )
    raise ValueError("Monitor log name must end with .csv or .tdms")


def _format_elapsed(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    remainder = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{remainder:04.1f}"


class ElapsedAxisItem(pg.AxisItem):
    def tickStrings(self, values: list[float], scale: float, spacing: float) -> list[str]:
        del scale, spacing
        return [_format_elapsed(value) for value in values]


class StatusLamp(QtWidgets.QFrame):
    COLORS = {
        "idle": "#52c41a",
        "running": "#52c41a",
        "simulated": "#52c41a",
        "warning": "#f59e0b",
        "error": "#ef2222",
        "disabled": "#94a3b8",
    }

    def __init__(self, parent: QtWidgets.QWidget | None = None, *, diameter: int = 16) -> None:
        super().__init__(parent)
        self._diameter = diameter
        self.setFixedSize(diameter, diameter)
        self.set_state("disabled")

    def set_state(self, state: str) -> None:
        color = self.COLORS.get(state.lower(), self.COLORS["disabled"])
        radius = self._diameter // 2
        self.setStyleSheet(f"background:{color}; border:none; border-radius:{radius}px;")


class TrendPlot(pg.PlotWidget):
    """Efficient rolling plot with native pyqtgraph pan, zoom, and export tools."""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        title: str,
        y_label: str,
        series: tuple[str, ...],
        colors: tuple[str, ...],
        history_seconds: float = 900.0,
        include_zero: bool = False,
    ) -> None:
        axis = ElapsedAxisItem(orientation="bottom")
        super().__init__(parent=parent, axisItems={"bottom": axis})
        self.history_seconds = float(history_seconds)
        self.include_zero = include_zero
        self.follow_live = True
        self.series = series
        self.colors = colors
        self._times: deque[float] = deque(maxlen=50_000)
        self._values: list[deque[float]] = []
        self._curves: list[pg.PlotDataItem] = []

        plot = self.getPlotItem()
        plot.setTitle(title, color=TEXT_COLOR, size="10pt")
        plot.setLabel("left", y_label)
        plot.setLabel("bottom", "Process time")
        plot.showGrid(x=True, y=True, alpha=0.25)
        plot.setClipToView(True)
        plot.setDownsampling(auto=True, mode="peak")
        plot.setMenuEnabled(True)
        self.setBackground(PANEL_BACKGROUND)
        self.setMinimumHeight(220)
        self._legend = plot.addLegend(offset=(55, 5), labelTextColor=TEXT_COLOR)
        self.configure_series(series, colors)

    @property
    def sample_count(self) -> int:
        return len(self._times)

    def configure_series(self, series: tuple[str, ...], colors: tuple[str, ...]) -> None:
        plot = self.getPlotItem()
        for curve in self._curves:
            plot.removeItem(curve)
        self._legend.clear()
        self.series = tuple(series)
        self.colors = tuple(colors)
        self._times.clear()
        self._values = [deque(maxlen=50_000) for _ in self.series]
        self._curves = []
        for index, name in enumerate(self.series):
            color = self.colors[index % len(self.colors)]
            curve = plot.plot(
                name=name,
                pen=pg.mkPen(color=color, width=1.5),
                connect="finite",
            )
            self._curves.append(curve)

    def clear_data(self) -> None:
        self._times.clear()
        for values in self._values:
            values.clear()
        for curve in self._curves:
            curve.setData([], [])

    def set_follow_live(self, enabled: bool) -> None:
        self.follow_live = bool(enabled)

    def append(self, timestamp: float, values: tuple[float | None, ...]) -> None:
        timestamp = float(timestamp)
        self._times.append(timestamp)
        for index, series_values in enumerate(self._values):
            value = values[index] if index < len(values) else None
            series_values.append(float("nan") if value is None else float(value))

        cutoff = timestamp - self.history_seconds
        while self._times and self._times[0] < cutoff:
            self._times.popleft()
            for series_values in self._values:
                series_values.popleft()

        x = np.fromiter(self._times, dtype=np.float64, count=len(self._times))
        finite_batches: list[np.ndarray] = []
        for curve, values_for_series in zip(self._curves, self._values, strict=True):
            y = np.fromiter(values_for_series, dtype=np.float64, count=len(values_for_series))
            curve.setData(x=x, y=y, connect="finite")
            finite = y[np.isfinite(y)]
            if finite.size:
                finite_batches.append(finite)

        if not self.follow_live or x.size == 0:
            return
        x_min = max(0.0, timestamp - self.history_seconds)
        x_max = max(x_min + 1.0, timestamp)
        self.getPlotItem().setXRange(x_min, x_max, padding=0.01)
        if not finite_batches:
            return
        y_min = min(float(values.min()) for values in finite_batches)
        y_max = max(float(values.max()) for values in finite_batches)
        if self.include_zero:
            y_min = min(0.0, y_min)
            y_max = max(0.0, y_max)
        if y_min == y_max:
            padding = max(abs(y_min) * 0.1, 1.0)
        else:
            padding = max((y_max - y_min) * 0.08, 1e-12)
        self.getPlotItem().setYRange(y_min - padding, y_max + padding, padding=0)


class SpecMassWindow(QtWidgets.QMainWindow):
    TICK_MS = 100

    def __init__(
        self,
        *,
        initial_program: Path | None = None,
        monitor_backend: Adam4118MonitorBackend | ReadOnlyHardwareMonitorBackend | None = None,
        monitor_telemetry: TelemetryWriter | None = None,
        monitor_output: Path | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Mass Spectrometer Station — SpecMass Python simulator")
        self.resize(1600, 900)
        self.setMinimumSize(1180, 720)

        self.program: ProcessProgram | None = None
        self.program_path: Path | None = None
        self.backend: SimulatedBackend | None = None
        self.controller: ProcessController | None = None
        self.runtime: SpecMassRuntime | None = None
        self.telemetry: TelemetryWriter | None = monitor_telemetry
        self.monitor_backend = monitor_backend
        self.monitor_output = monitor_output
        self._monitor_error = False
        self._monitor_started_monotonic = 0.0
        self._running = False
        self._run_started_monotonic = 0.0
        self._last_tick_monotonic = 0.0
        self._program_duration = 0.0
        self._mass_names: tuple[str, ...] = ()
        self._flow_control_modes: list[str] = []
        self._flow_override_values: list[float] = []
        self._device_state_labels: dict[str, QtWidgets.QLabel] = {}
        self._device_lamps: dict[str, StatusLamp] = {}

        self._build_layout()
        self._apply_styles()

        self.clock_timer = QtCore.QTimer(self)
        self.clock_timer.setInterval(1000)
        self.clock_timer.timeout.connect(self._update_clock)
        self.clock_timer.start()
        self._update_clock()

        self.tick_timer = QtCore.QTimer(self)
        self.tick_timer.setInterval(self.TICK_MS)
        self.tick_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.tick_timer.timeout.connect(self._tick)
        self.monitor_timer = QtCore.QTimer(self)
        self.monitor_timer.setInterval(500)
        self.monitor_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.monitor_timer.timeout.connect(self._monitor_tick)
        self._update_controls()
        if self.monitor_backend is not None:
            self._start_hardware_monitor()
        elif initial_program is not None:
            self.load_program(initial_program)

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget#central {{ background: {BACKGROUND}; color: {TEXT_COLOR}; }}
            QWidget {{ font-family: 'Segoe UI'; font-size: 9pt; color: {TEXT_COLOR}; }}
            QFrame#header {{ background: {HEADER_BACKGROUND}; border-bottom: 1px solid #87919a; }}
            QFrame#panel, QFrame#footer {{ background: {PANEL_BACKGROUND}; border: 1px solid #d8dde2; }}
            QLabel#section {{ font-size: 10pt; font-weight: 600; background: transparent; }}
            QLabel#headerValue {{ font-size: 10pt; font-weight: 600; background: transparent; }}
            QLabel#muted {{ color: {MUTED_COLOR}; background: transparent; }}
            QLabel#simulation {{ background: #ffe6e6; color: #9b1c1c; font-weight: 700; padding: 4px; }}
            QPushButton {{ padding: 6px 12px; background: #f4f1ed; border: 1px solid #9ba1a6; }}
            QPushButton:hover {{ background: #e9f4fb; border-color: #087cc1; }}
            QPushButton:disabled {{ color: #9aa0a6; background: #eceae7; }}
            QPushButton#primary {{ font-size: 10pt; font-weight: 700; min-height: 40px; }}
            QPushButton#stop {{ font-size: 10pt; font-weight: 700; min-height: 40px; }}
            QListWidget, QComboBox, QDoubleSpinBox {{ background: white; border: 1px solid #aab0b6; padding: 3px; }}
            QListWidget::item:selected {{ background: #087cc1; color: white; }}
            QHeaderView::section {{ background: #e7f8fd; padding: 5px; border: 1px solid #c9d1d8; }}
            """
        )

    def _build_layout(self) -> None:
        central = QtWidgets.QWidget(self)
        central.setObjectName("central")
        root_layout = QtWidgets.QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.setCentralWidget(central)

        root_layout.addWidget(self._build_header())
        self.mode_banner = QtWidgets.QLabel("SIMULATION — HARDWARE COMMUNICATION IS DISABLED")
        self.mode_banner.setObjectName("simulation")
        self.mode_banner.setAlignment(QtCore.Qt.AlignCenter)
        root_layout.addWidget(self.mode_banner)

        workspace = QtWidgets.QWidget()
        workspace_layout = QtWidgets.QGridLayout(workspace)
        workspace_layout.setContentsMargins(8, 8, 8, 4)
        workspace_layout.setHorizontalSpacing(8)
        workspace_layout.setVerticalSpacing(8)
        workspace_layout.setColumnStretch(0, 0)
        workspace_layout.setColumnStretch(1, 1)
        workspace_layout.setColumnStretch(2, 1)
        workspace_layout.setRowStretch(0, 1)
        workspace_layout.setRowStretch(1, 1)

        controls = self._build_controls()
        controls.setFixedWidth(320)
        workspace_layout.addWidget(controls, 0, 0, 2, 1)

        self.temperature_plot = TrendPlot(
            title="Stove Temperature [°C]",
            y_label="Temperature [°C]",
            series=("Temperature", "Setpoint"),
            colors=("#111111", "#26b83f"),
        )
        workspace_layout.addWidget(self.temperature_plot, 0, 1)

        self.flow_plot = TrendPlot(
            title="Brooks Flow Controllers",
            y_label="Flow [ml/min]",
            series=("Ch0", "Ch1", "Ch2", "Ch3"),
            colors=FLOW_COLORS,
            include_zero=True,
        )
        workspace_layout.addWidget(self.flow_plot, 0, 2)

        self.mass_plot = TrendPlot(
            title="Mass Spectrometer Signals",
            y_label="Signal",
            series=(),
            colors=MASS_COLORS,
        )
        workspace_layout.addWidget(self.mass_plot, 1, 1, 1, 2)
        root_layout.addWidget(workspace, 1)
        root_layout.addWidget(self._build_footer())

    def _build_header(self) -> QtWidgets.QFrame:
        header = QtWidgets.QFrame()
        header.setObjectName("header")
        layout = QtWidgets.QGridLayout(header)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setHorizontalSpacing(10)

        self.load_button = QtWidgets.QPushButton("Load\nprogram")
        self.load_button.setMinimumSize(88, 58)
        self.load_button.clicked.connect(self._choose_program)
        layout.addWidget(self.load_button, 0, 0, 2, 1)

        self.details_button = QtWidgets.QPushButton("Program\ndetails")
        self.details_button.setMinimumSize(88, 58)
        self.details_button.clicked.connect(self._show_stages)
        layout.addWidget(self.details_button, 0, 1, 2, 1)

        self.start_button = QtWidgets.QPushButton("▶  START")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self._start)
        layout.addWidget(self.start_button, 0, 2, 2, 1)

        self.stop_button = QtWidgets.QPushButton("■  STOP")
        self.stop_button.setObjectName("stop")
        self.stop_button.clicked.connect(self._stop)
        layout.addWidget(self.stop_button, 0, 3, 2, 1)

        self.program_label = self._header_value("No program loaded")
        self.stage_label = self._header_value("—")
        self.remaining_label = self._header_value("00:00:00.0")
        self.process_time_label = self._header_value("00:00:00.0")
        self.status_label = self._header_value("Idle")
        self.date_label = QtWidgets.QLabel()
        self.clock_label = self._header_value("")

        layout.addWidget(QtWidgets.QLabel("Program folder:"), 0, 4)
        layout.addWidget(self.program_label, 0, 5, 1, 5)
        layout.addWidget(QtWidgets.QLabel("Status:"), 0, 10)
        layout.addWidget(self.status_label, 0, 11)
        layout.addWidget(self.date_label, 0, 12)
        layout.addWidget(self.clock_label, 0, 13)
        layout.addWidget(QtWidgets.QLabel("Current Stage"), 1, 4)
        layout.addWidget(self.stage_label, 1, 5)
        layout.addWidget(QtWidgets.QLabel("Remaining Time"), 1, 6)
        layout.addWidget(self.remaining_label, 1, 7)
        layout.addWidget(QtWidgets.QLabel("Process Time"), 1, 8)
        layout.addWidget(self.process_time_label, 1, 9)
        layout.setColumnStretch(5, 1)
        return header

    @staticmethod
    def _header_value(text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setObjectName("headerValue")
        return label

    def _build_controls(self) -> QtWidgets.QFrame:
        panel = QtWidgets.QFrame()
        panel.setObjectName("panel")
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        layout.addWidget(self._section_label("Furnace and acquisition"))
        filament_layout = QtWidgets.QHBoxLayout()
        filament_layout.addWidget(QtWidgets.QLabel("Filament"))
        filament_layout.addSpacing(10)
        self.f1_lamp = StatusLamp()
        self.f2_lamp = StatusLamp()
        filament_layout.addWidget(self.f1_lamp)
        filament_layout.addWidget(QtWidgets.QLabel("F1"))
        filament_layout.addSpacing(18)
        filament_layout.addWidget(self.f2_lamp)
        filament_layout.addWidget(QtWidgets.QLabel("F2"))
        filament_layout.addStretch(1)
        layout.addLayout(filament_layout)
        layout.addWidget(self._separator())

        status_grid = QtWidgets.QGridLayout()
        self.setpoint_label = self._value_label("—")
        self.temperature_label = self._value_label("—")
        self.heater_label = QtWidgets.QLabel("0.0 %")
        status_grid.addWidget(QtWidgets.QLabel("Temperature Setpoint [°C]"), 0, 0)
        status_grid.addWidget(self.setpoint_label, 0, 1, alignment=QtCore.Qt.AlignRight)
        status_grid.addWidget(QtWidgets.QLabel("Stove"), 1, 0)
        status_grid.addWidget(self.temperature_label, 1, 1, alignment=QtCore.Qt.AlignRight)
        status_grid.addWidget(QtWidgets.QLabel("Heater"), 2, 0)
        status_grid.addWidget(self.heater_label, 2, 1, alignment=QtCore.Qt.AlignRight)
        layout.addLayout(status_grid)
        layout.addWidget(self._separator())

        layout.addWidget(self._section_label("Flow Controllers"))
        self.flow_list = QtWidgets.QListWidget()
        self.flow_list.setFixedHeight(92)
        self.flow_list.currentRowChanged.connect(self._selected_flow_changed)
        layout.addWidget(self.flow_list)

        mode_layout = QtWidgets.QFormLayout()
        self.flow_mode_box = QtWidgets.QComboBox()
        self.flow_mode_box.addItems(("Program", "App override", "Front panel", "External"))
        self.flow_value_spin = QtWidgets.QDoubleSpinBox()
        self.flow_value_spin.setRange(0.0, 1_000_000.0)
        self.flow_value_spin.setDecimals(4)
        self.flow_value_spin.setSuffix(" ml/min")
        mode_layout.addRow("Source", self.flow_mode_box)
        mode_layout.addRow("Setpoint", self.flow_value_spin)
        layout.addLayout(mode_layout)

        self.apply_flows_button = QtWidgets.QPushButton("Apply channel setting")
        self.apply_flows_button.clicked.connect(self._apply_selected_flow)
        layout.addWidget(self.apply_flows_button)
        self.flow_info_label = QtWidgets.QLabel("Select a Brooks channel")
        self.flow_info_label.setObjectName("muted")
        self.flow_info_label.setWordWrap(True)
        layout.addWidget(self.flow_info_label)

        self.continue_button = QtWidgets.QPushButton("Continue Stage")
        self.continue_button.clicked.connect(self._continue_stage)
        layout.addWidget(self.continue_button, alignment=QtCore.Qt.AlignHCenter)
        layout.addWidget(self._separator())

        device_grid = QtWidgets.QGridLayout()
        device_grid.addWidget(self._section_label("Device"), 0, 0)
        device_grid.addWidget(self._section_label("State"), 0, 1)
        devices = ("MSDevTh", "VICIActuator", "Brooks1", "ADAM4050", "ADAM4118")
        for row, name in enumerate(devices, start=1):
            state_label = QtWidgets.QLabel("Idle")
            lamp = StatusLamp()
            lamp.set_state("idle")
            self._device_state_labels[name] = state_label
            self._device_lamps[name] = lamp
            device_grid.addWidget(QtWidgets.QLabel(name), row, 0)
            device_grid.addWidget(state_label, row, 1)
            device_grid.addWidget(lamp, row, 2)
        device_grid.setColumnStretch(0, 1)
        layout.addLayout(device_grid)
        layout.addStretch(1)
        return panel

    def _build_footer(self) -> QtWidgets.QFrame:
        footer = QtWidgets.QFrame()
        footer.setObjectName("footer")
        layout = QtWidgets.QHBoxLayout(footer)
        layout.setContentsMargins(10, 5, 10, 5)
        run_label = self._section_label("Run log:")
        self.output_label = QtWidgets.QLabel("No run log")
        self.output_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.follow_check = QtWidgets.QCheckBox("Follow live plots")
        self.follow_check.setChecked(True)
        self.follow_check.toggled.connect(self._set_follow_live)
        self.cooling_spin = QtWidgets.QDoubleSpinBox()
        self.cooling_spin.setRange(-50.0, 1200.0)
        self.cooling_spin.setValue(50.0)
        self.cooling_spin.setSuffix(" °C")
        layout.addWidget(run_label)
        layout.addWidget(self.output_label, 1)
        layout.addWidget(self.follow_check)
        layout.addSpacing(18)
        layout.addWidget(QtWidgets.QLabel("Cooling threshold:"))
        layout.addWidget(self.cooling_spin)
        return footer

    @staticmethod
    def _section_label(text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setObjectName("section")
        return label

    @staticmethod
    def _value_label(text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        font = label.font()
        font.setPointSize(11)
        font.setBold(True)
        label.setFont(font)
        return label

    @staticmethod
    def _separator() -> QtWidgets.QFrame:
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Sunken)
        return line

    def _choose_program(self) -> None:
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select folder containing Stage*.msdef",
            str(self.program_path or Path.cwd()),
        )
        if chosen:
            self.load_program(Path(chosen))

    def _start_hardware_monitor(self) -> None:
        assert self.monitor_backend is not None
        flow_names = tuple(getattr(self.monitor_backend, "flow_channel_names", ()))
        combined = bool(flow_names)
        monitor_name = "hardware" if combined else "temperature"
        self.setWindowTitle(
            f"Mass Spectrometer Station — SpecMass read-only {monitor_name} monitor"
        )
        devices = "ADAM4118 + BROOKS0254" if combined else "ADAM4118"
        self.mode_banner.setText(
            f"READ-ONLY {devices} MONITOR — ALL HEATER, VALVE, AND FLOW OUTPUTS DISABLED"
        )
        self.mode_banner.setStyleSheet(
            "background:#fff4cc; color:#714b00; font-weight:700; padding:4px;"
        )
        self.program_label.setText(f"No program — {monitor_name} monitor only")
        self.status_label.setText("Monitoring")
        self.stage_label.setText("—")
        self.setpoint_label.setText("—")
        self.heater_label.setText("Disabled")
        self.remaining_label.setText("—")
        self.output_label.setText("Read-only; no actuator output")
        if self.monitor_output is not None:
            resolved_output = str(self.monitor_output.resolve())
            self.output_label.setText(self.monitor_output.name)
            self.output_label.setToolTip(resolved_output)
        self.temperature_plot.configure_series(
            self.monitor_backend.channel_names,
            ("#111111", "#df3e3e"),
        )
        self.flow_plot.configure_series(flow_names, FLOW_COLORS)
        self._build_flow_channels(len(flow_names))
        self.mass_plot.configure_series((), MASS_COLORS)
        self._monitor_started_monotonic = time.monotonic()
        self._update_device_states()
        self._update_controls()
        self.monitor_timer.setInterval(
            int(getattr(self.monitor_backend, "poll_interval_ms", 500))
        )
        self.monitor_timer.start()
        QtCore.QTimer.singleShot(0, self._monitor_tick)

    def _monitor_tick(self) -> None:
        if self.monitor_backend is None or self._monitor_error:
            self.monitor_timer.stop()
            return
        timestamp = time.monotonic() - self._monitor_started_monotonic
        try:
            snapshot = self.monitor_backend.read(timestamp)
            if self.telemetry is not None:
                flow_count = len(snapshot.flows)
                self.telemetry.write(
                    snapshot,
                    ControlCommand.safe(
                        flow_count=flow_count,
                        flow_write_enabled=(False,) * flow_count,
                    ),
                    ControllerStatus(
                        MSMState.IDLE,
                        None,
                        timestamp,
                        0.0,
                        False,
                        False,
                    ),
                )
        except Exception as exc:
            self._monitor_error = True
            self.monitor_timer.stop()
            message = str(exc)
            try:
                self._close_telemetry()
            except Exception as close_exc:
                message = f"{message}\nAdditionally, closing the monitor log failed: {close_exc}"
            self.monitor_backend.safe_shutdown()
            self.status_label.setText("Read/Log Error")
            self._update_device_states(error=True)
            QtWidgets.QMessageBox.critical(self, "Hardware monitor error", message)
            return
        values = snapshot.temperatures or (snapshot.temperature,)
        self.temperature_label.setText(" / ".join(f"{value:.2f} °C" for value in values))
        self.process_time_label.setText(_format_elapsed(timestamp))
        self.temperature_plot.append(timestamp, tuple(values))
        if snapshot.flows:
            self.flow_plot.append(timestamp, snapshot.flows)
            channel = self.flow_list.currentRow()
            if 0 <= channel < len(snapshot.flows):
                self.flow_info_label.setText(
                    f"Brooks1/{channel}: measured {snapshot.flows[channel]:.3g} ml/min; "
                    "read-only, writes disabled"
                )

    def load_program(self, path: Path) -> None:
        if self._running:
            QtWidgets.QMessageBox.warning(self, "Run active", "Stop the run before loading another program.")
            return
        try:
            program = load_program(path)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Cannot load program", str(exc))
            return

        self._close_telemetry()
        flow_count = max((len(stage.start_flows) for stage in program.stages), default=0)
        self.program = program
        self.program_path = path
        self.backend = SimulatedBackend(flow_channels=flow_count)
        self.controller = ProcessController()
        self.controller.load(program)
        self.runtime = None
        self._program_duration = sum(stage.effective_duration_seconds() for stage in program.stages)
        self._mass_names = tuple(mass_stimuli_from_scan_settings(program.scan_settings))

        resolved = str(path.resolve())
        self.program_label.setText(path.name)
        self.program_label.setToolTip(resolved)
        self.status_label.setText("Ready For Start")
        self.stage_label.setText("—")
        self.temperature_label.setText(f"{self.backend.temperature:.2f} °C")
        self.setpoint_label.setText(f"{program.stages[0].start_temperature:.2f}")
        self.heater_label.setText("0.0 %")
        self.remaining_label.setText(_format_elapsed(self._program_duration))
        self.process_time_label.setText(_format_elapsed(0.0))
        self.output_label.setText("No run log")
        self.output_label.setToolTip("")

        self.temperature_plot.clear_data()
        self.flow_plot.configure_series(tuple(f"Ch{index}" for index in range(flow_count)), FLOW_COLORS)
        self.mass_plot.configure_series(self._mass_names, MASS_COLORS)
        self._build_flow_channels(flow_count)
        self._update_filament(program)
        self._update_device_states()
        self._update_controls()

    def _start(self) -> None:
        if not self.program or not self.controller or not self.backend:
            return
        try:
            self.controller.cooling_temperature = self.cooling_spin.value()
            self.controller.start(0.0)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Cannot start", str(exc))
            return

        output_dir = Path.cwd() / ".run-output"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        flow_count = max((len(stage.start_flows) for stage in self.program.stages), default=0)
        if importlib.util.find_spec("nptdms") is not None:
            output_path = output_dir / f"specmass_sim_{stamp}.tdms"
            self.telemetry = TdmsTelemetryWriter(
                output_path,
                flow_channels=flow_count,
                mass_stimuli=mass_stimuli_from_scan_settings(self.program.scan_settings),
                nominal_increment_seconds=self.TICK_MS / 1000.0,
            )
        else:
            output_path = output_dir / f"specmass_sim_{stamp}.csv"
            self.telemetry = CsvTelemetryWriter(output_path, flow_channels=flow_count)
        self.runtime = SpecMassRuntime(
            backend=self.backend,
            controller=self.controller,
            pid=PIDController(PIDGains(kc=20.0, ti_seconds=10.0, td_seconds=0.0)),
            safety=SafetyPolicy(maximum_temperature=1200.0),
            telemetry=self.telemetry,
        )
        resolved_output = str(output_path.resolve())
        self.output_label.setText(output_path.name)
        self.output_label.setToolTip(resolved_output)
        self._running = True
        self._run_started_monotonic = time.monotonic()
        self._last_tick_monotonic = self._run_started_monotonic
        self.temperature_plot.clear_data()
        self.flow_plot.clear_data()
        self.mass_plot.clear_data()
        self._update_device_states()
        self._update_controls()
        self.tick_timer.start()

    def _tick(self) -> None:
        if not self._running or self.runtime is None or self.controller is None:
            self.tick_timer.stop()
            return
        now = time.monotonic()
        timestamp = now - self._run_started_monotonic
        dt = min(1.0, max(0.01, now - self._last_tick_monotonic))
        self._last_tick_monotonic = now
        try:
            frame = self.runtime.step(timestamp, dt)
        except Exception as exc:
            self._running = False
            self.tick_timer.stop()
            self._close_telemetry()
            self.status_label.setText("Error")
            self._update_device_states(error=True)
            self._update_controls()
            QtWidgets.QMessageBox.critical(self, "Safety stop", str(exc))
            return

        command = frame.command
        status = frame.status
        self.status_label.setText(status.state.name.replace("_", " ").title())
        if status.stage_index is None:
            self.stage_label.setText("—")
        else:
            stage = self.program.stages[status.stage_index] if self.program else None
            self.stage_label.setText(stage.name if stage else str(status.stage_index))
        self.process_time_label.setText(_format_elapsed(status.process_elapsed_seconds))
        remaining = max(0.0, self._program_duration - status.process_elapsed_seconds)
        self.remaining_label.setText(_format_elapsed(remaining))
        self.temperature_label.setText(f"{frame.snapshot.temperature:.2f} °C")
        self.setpoint_label.setText(
            "—" if command.temperature_setpoint is None else f"{command.temperature_setpoint:.2f}"
        )
        self.heater_label.setText(f"{command.heater_percent:.1f} %")
        self.temperature_plot.append(timestamp, (frame.snapshot.temperature, command.temperature_setpoint))
        self.flow_plot.append(timestamp, tuple(frame.snapshot.flows))
        masses = frame.snapshot.masses or {}
        self.mass_plot.append(timestamp, tuple(masses.get(name) for name in self._mass_names))
        self._update_flow_info(command, frame.snapshot.flows)
        self._update_controls()
        if status.completed:
            self._running = False
            self.tick_timer.stop()
            self._close_telemetry()
            self.status_label.setText("Ready")
            self._update_device_states()
            self._update_controls()

    def _stop(self) -> None:
        if self.controller is not None:
            self.controller.stop()
        if self.backend is not None:
            self.backend.safe_shutdown()
        self._update_controls()

    def _continue_stage(self) -> None:
        if self.controller is None:
            return
        status = self.controller.status(0.0)
        try:
            if status.waiting_for_confirmation:
                self.controller.confirm_next_stage()
            else:
                self.controller.force_continue_stage()
        except RuntimeError as exc:
            QtWidgets.QMessageBox.warning(self, "Cannot continue", str(exc))
        self._update_controls()

    def _build_flow_channels(self, channel_count: int) -> None:
        self._flow_control_modes = ["Program"] * channel_count
        self._flow_override_values = [0.0] * channel_count
        self.flow_list.clear()
        self.flow_list.addItems([f"Brooks1/{channel}" for channel in range(channel_count)])
        if channel_count:
            self.flow_list.setCurrentRow(0)

    def _selected_flow_changed(self, channel: int) -> None:
        if channel < 0 or channel >= len(self._flow_control_modes):
            return
        self.flow_mode_box.blockSignals(True)
        self.flow_value_spin.blockSignals(True)
        self.flow_mode_box.setCurrentText(self._flow_control_modes[channel])
        self.flow_value_spin.setValue(self._flow_override_values[channel])
        self.flow_mode_box.blockSignals(False)
        self.flow_value_spin.blockSignals(False)
        self.flow_info_label.setText(
            f"Application Ch{channel} = physical Brooks channel {channel + 1}. "
            "Program writes only when the requested value changes."
        )

    def _apply_selected_flow(self) -> None:
        if self.controller is None:
            return
        channel = self.flow_list.currentRow()
        if channel < 0:
            return
        selected = self.flow_mode_box.currentText()
        value = self.flow_value_spin.value()
        try:
            self._flow_control_modes[channel] = selected
            self._flow_override_values[channel] = value
            if selected == "App override":
                self.controller.set_manual_flow_override(channel, value)
            elif selected == "Front panel":
                self.controller.set_flow_channel_external(channel, False)
                self.controller.clear_manual_flow_override(channel)
                if self.backend is not None:
                    self.backend.set_front_panel_flow(channel, value)
            elif selected == "External":
                self.controller.clear_manual_flow_override(channel)
                self.controller.set_flow_channel_external(channel, True)
                if self.backend is not None:
                    self.backend.set_external_flow(channel, value)
            else:
                self.controller.set_flow_channel_external(channel, False)
                self.controller.clear_manual_flow_override(channel)
        except ValueError as exc:
            QtWidgets.QMessageBox.critical(self, "Invalid flow setpoint", str(exc))
            return
        self.flow_info_label.setText(f"Brooks1/{channel}: {selected}, value {value:g} ml/min")
        self._update_controls()

    def _update_flow_info(self, command: object, measured_flows: tuple[float, ...]) -> None:
        channel = self.flow_list.currentRow()
        if channel < 0:
            return
        setpoints = getattr(command, "flow_setpoints", ())
        enabled = getattr(command, "flow_write_enabled", ()) or (True,) * len(setpoints)
        performed = getattr(command, "flow_write_performed", ()) or (False,) * len(setpoints)
        measured = measured_flows[channel] if channel < len(measured_flows) else float("nan")
        requested = setpoints[channel] if channel < len(setpoints) else float("nan")
        allowed = enabled[channel] if channel < len(enabled) else False
        sent = performed[channel] if channel < len(performed) else False
        self.flow_info_label.setText(
            f"Brooks1/{channel}: measured {measured:.3g}, requested {requested:.3g} ml/min; "
            f"write allowed {'yes' if allowed else 'no'}, sent now {'yes' if sent else 'no'}"
        )

    def _update_filament(self, program: ProcessProgram) -> None:
        filament = str(program.scan_settings.get("Filament", "")).upper()
        self.f1_lamp.set_state("running" if filament == "F1" else "disabled")
        self.f2_lamp.set_state("running" if filament == "F2" else "disabled")

    def _update_device_states(self, *, error: bool = False) -> None:
        if self.monitor_backend is not None:
            monitored_devices = set(
                getattr(self.monitor_backend, "monitored_devices", ("ADAM4118",))
            )
            for name, label in self._device_state_labels.items():
                if name in monitored_devices:
                    state = "Error" if error or self._monitor_error else "Monitoring"
                    lamp_state = "error" if state == "Error" else "running"
                else:
                    state = "Disabled"
                    lamp_state = "disabled"
                label.setText(state)
                self._device_lamps[name].set_state(lamp_state)
            return
        for name, label in self._device_state_labels.items():
            if error:
                state = "Error"
            elif self._running:
                state = "Running"
            elif self.program:
                state = "Simulated"
            else:
                state = "Idle"
            label.setText(state)
            self._device_lamps[name].set_state(state)

    def _update_controls(self) -> None:
        if self.monitor_backend is not None:
            self.load_button.setEnabled(False)
            self.details_button.setEnabled(False)
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.continue_button.setEnabled(False)
            self.apply_flows_button.setEnabled(False)
            self.flow_mode_box.setEnabled(False)
            self.flow_value_spin.setEnabled(False)
            self.cooling_spin.setEnabled(False)
            return
        state = self.controller.state if self.controller else MSMState.IDLE
        status = self.controller.status(0.0) if self.controller else None
        can_force = bool(
            self.controller
            and state is MSMState.RUNNING_STAGE
            and self.controller.current_stage
            and self.controller.current_stage.temperature_mode is TemperatureMode.ISOTHERMAL
        )
        can_continue = bool(status and status.waiting_for_confirmation) or can_force
        self.load_button.setEnabled(not self._running)
        self.details_button.setEnabled(self.program is not None)
        self.start_button.setEnabled(
            bool(self.program and state is MSMState.READY_FOR_START and not self._running)
        )
        self.stop_button.setEnabled(self._running)
        self.continue_button.setEnabled(can_continue)
        self.apply_flows_button.setEnabled(self.program is not None)

    def _set_follow_live(self, enabled: bool) -> None:
        self.temperature_plot.set_follow_live(enabled)
        self.flow_plot.set_follow_live(enabled)
        self.mass_plot.set_follow_live(enabled)

    def _show_stages(self) -> None:
        if self.program is None:
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Program details")
        dialog.resize(820, 380)
        layout = QtWidgets.QVBoxLayout(dialog)
        table = QtWidgets.QTableWidget(len(self.program.stages), 7)
        table.setHorizontalHeaderLabels(
            ("Stage", "Mode", "Start °C", "End °C", "Rate °C/min", "Duration s", "Auto")
        )
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        for row, stage in enumerate(self.program.stages):
            values = (
                stage.name,
                "Isothermal" if stage.temperature_mode is TemperatureMode.ISOTHERMAL else "Ramp",
                f"{stage.start_temperature:g}",
                f"{stage.end_temperature:g}",
                f"{stage.temperature_rate_per_minute:g}",
                f"{stage.effective_duration_seconds():g}",
                "Yes" if stage.auto_start else "No",
            )
            for column, value in enumerate(values):
                table.setItem(row, column, QtWidgets.QTableWidgetItem(value))
        table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        layout.addWidget(table)
        close_button = QtWidgets.QPushButton("Close")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button, alignment=QtCore.Qt.AlignRight)
        dialog.exec_()

    def _update_clock(self) -> None:
        self.date_label.setText(time.strftime("%d.%m.%Y"))
        self.clock_label.setText(time.strftime("%H:%M:%S"))

    def _close_telemetry(self) -> None:
        if self.telemetry is not None:
            self.telemetry.close()
            self.telemetry = None

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self.tick_timer.stop()
        self.monitor_timer.stop()
        if self.monitor_backend is not None:
            self.monitor_backend.safe_shutdown()
        if self.runtime is not None:
            self.runtime.safe_shutdown()
        elif self.backend is not None:
            self.backend.safe_shutdown()
        self._close_telemetry()
        event.accept()


def main() -> int:
    parser = argparse.ArgumentParser(description="SpecMass PyQt5 desktop UI")
    parser.add_argument("--program", type=Path, help="initial folder containing Stage*.msdef")
    monitor_group = parser.add_mutually_exclusive_group()
    monitor_group.add_argument(
        "--temperature-monitor",
        action="store_true",
        help="read and plot ADAM4118 temperatures without enabling any actuator",
    )
    monitor_group.add_argument(
        "--hardware-monitor",
        action="store_true",
        help="read and plot ADAM4118 temperatures and Brooks flows without enabling actuators",
    )
    parser.add_argument("--builds", type=Path, help="folder containing MassSpec.exe and data")
    parser.add_argument(
        "--monitor-output",
        type=Path,
        help="optional new .csv or .tdms file for read-only monitor samples",
    )
    parser.add_argument(
        "--allow-read-hardware",
        action="store_true",
        help="explicitly permit read-only ADAM4118 and/or Brooks serial queries",
    )
    args = parser.parse_args()
    monitor_requested = args.temperature_monitor or args.hardware_monitor
    if monitor_requested and args.builds is None:
        parser.error("a hardware monitor requires --builds")
    if monitor_requested and not args.allow_read_hardware:
        parser.error("a hardware monitor requires --allow-read-hardware")
    if monitor_requested and args.program is not None:
        parser.error("a hardware monitor cannot be combined with --program")
    if args.monitor_output is not None and not monitor_requested:
        parser.error("--monitor-output requires --temperature-monitor or --hardware-monitor")

    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    pg.setConfigOptions(antialias=True, background=PANEL_BACKGROUND, foreground=TEXT_COLOR)
    application = QtWidgets.QApplication(sys.argv[:1])
    application.setApplicationName("SpecMass Python")
    application.setStyle("Fusion")
    monitor_backend = None
    monitor_telemetry = None
    if monitor_requested:
        try:
            if args.hardware_monitor:
                monitor_backend = _create_hardware_monitor(args.builds)
            else:
                monitor_backend = _create_adam4118_monitor(args.builds)
            if args.monitor_output is not None:
                monitor_telemetry = _create_monitor_telemetry(
                    args.monitor_output,
                    monitor_backend,
                )
        except Exception as exc:
            if monitor_backend is not None:
                monitor_backend.safe_shutdown()
            QtWidgets.QMessageBox.critical(None, "Cannot configure hardware monitor", str(exc))
            return 2
    window = SpecMassWindow(
        initial_program=args.program,
        monitor_backend=monitor_backend,
        monitor_telemetry=monitor_telemetry,
        monitor_output=args.monitor_output,
    )
    window.show()
    return int(application.exec_())


if __name__ == "__main__":
    raise SystemExit(main())
