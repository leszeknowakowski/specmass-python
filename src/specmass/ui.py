from __future__ import annotations

import argparse
import copy
import importlib.util
import re
import shutil
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from .devices.adam4118 import Adam4118Client, Adam4118MonitorBackend
from .devices.base import ControlCommand
from .devices.brooks0254 import Brooks0254ReadOnlyClient
from .devices.hardware_shadow import HardwareShadowBackend, HidenHardwareShadowBackend
from .devices.hiden import HidenScanClient, HidenTrendAcquisition
from .devices.read_only_monitor import ReadOnlyHardwareMonitorBackend
from .devices.serial_transport import PySerialTransaction, SerialSettings
from .devices.simulated import SimulatedBackend
from .hiden import (
    DEFAULT_HIDEN_MASS_NAMES,
    HIDEN_ENVIRONMENT_SETTINGS_NAME,
    HidenEnvironmentConfig,
    HidenEnvironmentSettings,
    HidenScanDefinition,
    HidenScanPlan,
    apply_hiden_environment_settings,
    find_hiden_environment_config,
    hiden_scan_label,
    load_hiden_connection,
    load_hiden_environment_config,
    load_hiden_environment_settings,
    load_hiden_scan_plan,
    new_hiden_mass_scan,
    validate_hiden_environment_settings,
)
from .legacy import (
    create_program_directory,
    default_stage_mapping,
    load_legacy_json,
    load_program,
    load_stage_documents,
    save_legacy_json,
)
from .models import ProcessProgram, ProcessStage, TemperatureMode, ValveMode
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

SAVED_HIDEN_IDENTITY = 'HAL RC RGA 201 #16359'
HIDEN_ENVIRONMENT_PARAMETERS: tuple[tuple[str, str, str], ...] = (
    ("multiplier", "0", "V"),
    ("curtail-clipping", "0", "(1 = on, 0 = off)"),
    ("F1", "0", "(1 = on, 0 = off)"),
    ("F2", "0", "(1 = on, 0 = off)"),
    ("resolution", "0", "%"),
    ("delta-m", "0", "%"),
    ("mass", "5.50", "amu"),
    ("emission", "1000.00", "uA"),
    ("electron-energy", "70.00", "V"),
    ("cage", "3.00", "V"),
    ("focus", "-90", "V"),
    ("mode-change-delay", "1000", "ms"),
    ("Faraday_range", "-5", "mbar"),
    ("Faraday", "0.00", "mbar"),
    ("SEM_range", "-7", "mbar"),
    ("SEM", "0.00", "mbar"),
    ("Total_range", "-5", "mbar"),
    ("Total", "0.00", "mbar"),
    ("auxiliary1_range", "0", "V"),
    ("auxiliary1", "0.00", "V"),
    ("auxiliary2_range", "0", "V"),
    ("auxiliary2", "0.00", "V"),
)


@dataclass(frozen=True, slots=True)
class _HidenEnvironmentParameter:
    name: str
    base_value: float
    description: str
    format_string: str
    minimum: float | None = None
    maximum: float | None = None
    resolution: float | None = None

    def render(self, value: float) -> str:
        if self.format_string == "%d":
            return str(int(round(value)))
        return f"{value:.2f}"


_HIDEN_LOCAL_ENVIRONMENT_COMMAND = re.compile(
    r"^lset\s+([A-Za-z0-9_-]+)\s+"
    r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)$",
    re.IGNORECASE,
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


def _create_hiden_hardware_shadow(
    builds_directory: Path,
    program_directory: Path,
) -> HidenHardwareShadowBackend:
    reader = _create_hardware_monitor(builds_directory)
    try:
        connection = load_hiden_connection(builds_directory)
        if not connection.enabled:
            raise ValueError("MSDevTh EnableMS is false; Hiden acquisition is disabled")
        if connection.connection_type != 0:
            raise ValueError(
                "This milestone supports the deployed serial Hiden connection only"
            )
        if connection.resource.strip().upper() != "COM3":
            raise ValueError(
                "The guarded Hiden milestone is locked to the inventoried COM3 "
                f"device, not {connection.resource!r}"
            )
        if (
            connection.parity_name != "none"
            or connection.data_bits != 8
            or connection.stop_bits != 1.0
        ):
            raise ValueError(
                "The Hiden serial connection must use 8 data bits, no parity, "
                "1 stop bit"
            )
        base_environment = load_hiden_environment_config(
            find_hiden_environment_config(builds_directory)
        )
        environment_settings = load_hiden_environment_settings(
            program_directory,
            base_environment,
        )
        environment = apply_hiden_environment_settings(
            base_environment,
            environment_settings,
        )
        plan = load_hiden_scan_plan(program_directory)
        timeout_seconds = max(0.001, connection.timeout_ms / 1000.0)
        transport = PySerialTransaction(
            SerialSettings(
                port=connection.resource,
                baudrate=connection.baud_rate,
                timeout_seconds=timeout_seconds,
                write_timeout_seconds=timeout_seconds,
                read_terminator=b"\n",
                # Report-17 data can span several `data` responses.
                reset_input_buffer_before_write=False,
            ),
            hardware_enabled=True,
        )
        client = HidenScanClient(transport, environment)
        acquisition = HidenTrendAcquisition(
            client,
            plan,
            names_by_mass={item.mass: item.name for item in connection.masses},
        )
        return HidenHardwareShadowBackend(
            reader,
            acquisition,
            program_directory=program_directory,
            scan_plan=plan,
            environment_settings=environment_settings,
        )
    except Exception:
        reader.safe_shutdown()
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


class MassScanDialog(QtWidgets.QDialog):
    """Offline LabVIEW-style editor for one Hiden mass scan."""

    INPUT_DEVICES = (
        "Faraday",
        "SEM",
        "Total",
        "auxiliary1",
        "auxiliary2",
        "0V",
        "test",
        "scanV",
        "monitor1",
        "monitor2",
        "monitor3",
        "nul-dev",
        "none",
    )

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        initial_mass: float = 18.0,
        initial_scan: Mapping[str, Any] | None = None,
        scan_number: int = 1,
        environment_config: HidenEnvironmentConfig | None = None,
    ) -> None:
        super().__init__(parent)
        self._environment_parameters = self._make_environment_parameters(
            environment_config
        )
        self._refreshing_environment_table = False
        self.setWindowTitle(
            "Scan editor: edit scan" if initial_scan is not None else "Scan editor: new scan"
        )
        self.setModal(True)
        self.resize(680, 520)
        self.setMinimumSize(600, 470)
        layout = QtWidgets.QVBoxLayout(self)

        explanation = QtWidgets.QLabel(
            "Offline scan definition editor — Done returns settings to the program editor; "
            "no Hiden command is sent."
        )
        explanation.setWordWrap(True)
        explanation.setObjectName("muted")
        layout.addWidget(explanation)

        editing_row = QtWidgets.QHBoxLayout()
        editing_row.addStretch(1)
        editing_row.addWidget(QtWidgets.QLabel("Editing Scan"))
        self.editing_scan_spin = QtWidgets.QSpinBox()
        self.editing_scan_spin.setRange(scan_number, scan_number)
        self.editing_scan_spin.setValue(scan_number)
        self.editing_scan_spin.setEnabled(False)
        editing_row.addWidget(self.editing_scan_spin)
        layout.addLayout(editing_row)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_environment_tab(), "Environment")
        self.tabs.addTab(self._build_scan_tab(initial_mass), "Scan")
        self.tabs.addTab(self._build_detector_tab(), "Detector")
        self.tabs.addTab(self._build_advanced_tab(), "Advanced")
        layout.addWidget(self.tabs, 1)
        self.environment_parameter_table.currentCellChanged.connect(
            self._environment_parameter_selected
        )
        self.environment_parameter_table.itemChanged.connect(
            self._environment_table_item_changed
        )
        self.environment_change_button.clicked.connect(
            self._change_environment_value
        )
        self.environment_new_value.lineEdit().returnPressed.connect(
            self._change_environment_value
        )
        self.environment_changes_edit.textChanged.connect(
            self._refresh_environment_table_values
        )

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Done")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("Abort")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._scan_type_changed(self.scan_type_box.currentText())
        self._continuous_scan_changed(self.continuous_scan_check.isChecked())
        self._update_detector_range_label()
        if initial_scan is not None:
            self._load_scan_definition(initial_scan)
        self._refresh_environment_table_values()

    def _build_environment_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        top = QtWidgets.QHBoxLayout()
        mode_form = QtWidgets.QFormLayout()
        self.environment_global_mode = QtWidgets.QComboBox()
        self.environment_global_mode.addItem("RGA")
        self.environment_global_mode.setEnabled(False)
        mode_form.addRow("Global mode", self.environment_global_mode)
        top.addLayout(mode_form)
        top.addStretch(1)
        layout.addLayout(top)

        layout.addWidget(QtWidgets.QLabel("Global mode parameters"))
        self.environment_parameter_table = QtWidgets.QTableWidget(
            len(self._environment_parameters), 3
        )
        self.environment_parameter_table.setHorizontalHeaderLabels(
            ("Parameter", "Value", "Description")
        )
        self.environment_parameter_table.setEditTriggers(
            QtWidgets.QAbstractItemView.DoubleClicked
            | QtWidgets.QAbstractItemView.EditKeyPressed
        )
        self.environment_parameter_table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows
        )
        for row, parameter in enumerate(self._environment_parameters):
            values = (
                parameter.name,
                parameter.render(parameter.base_value),
                parameter.description,
            )
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if column != 1 or parameter.name.casefold() == "mass":
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
                self.environment_parameter_table.setItem(row, column, item)
        header = self.environment_parameter_table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        self.environment_parameter_table.setCurrentCell(0, 0)
        layout.addWidget(self.environment_parameter_table, 1)

        change_row = QtWidgets.QHBoxLayout()
        change_row.addStretch(1)
        change_row.addWidget(QtWidgets.QLabel("New value"))
        self.environment_new_value = QtWidgets.QDoubleSpinBox()
        self.environment_new_value.setRange(-1_000_000.0, 1_000_000.0)
        self.environment_new_value.setDecimals(4)
        change_row.addWidget(self.environment_new_value)
        self.environment_change_button = QtWidgets.QPushButton("Change")
        change_row.addWidget(self.environment_change_button)
        layout.addLayout(change_row)
        note = QtWidgets.QLabel(
            "Double-click a Value cell, or select a row and use New value + Change. "
            "This creates a local override for this scan using the legacy "
            "\"lset parameter value\" format. The scanned parameter itself (mass) "
            "cannot be overridden. Editing is offline; overrides are sent only when "
            "a guarded Hiden acquisition starts."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        return tab

    @staticmethod
    def _make_environment_parameters(
        environment_config: HidenEnvironmentConfig | None,
    ) -> tuple[_HidenEnvironmentParameter, ...]:
        if environment_config is None:
            return tuple(
                _HidenEnvironmentParameter(
                    name=name,
                    base_value=float(value),
                    description=description,
                    format_string="%.2f" if "." in value else "%d",
                )
                for name, value, description in HIDEN_ENVIRONMENT_PARAMETERS[:12]
            )

        try:
            mode_index = next(
                index
                for index, mode in enumerate(environment_config.modes)
                if mode.strip().casefold() == "rga"
            )
        except StopIteration:
            mode_index = 0
        parameters = tuple(
            _HidenEnvironmentParameter(
                name=device.name,
                base_value=device.values_by_mode[mode_index],
                description=device.unit,
                format_string=device.format_string,
                minimum=device.minimum,
                maximum=device.maximum,
                resolution=device.resolution,
            )
            for device in environment_config.devices
            if device.group_membership & 1
        )
        if not parameters:
            raise ValueError("Hiden environment has no editable output parameters")
        return parameters

    def _environment_overrides(self) -> dict[str, float]:
        result: dict[str, float] = {}
        for command in self.environment_changes_edit.toPlainText().split(","):
            match = _HIDEN_LOCAL_ENVIRONMENT_COMMAND.fullmatch(command.strip())
            if match is not None:
                result[match.group(1).casefold()] = float(match.group(2))
        return result

    def _refresh_environment_table_values(self) -> None:
        overrides = self._environment_overrides()
        self._refreshing_environment_table = True
        try:
            for row, parameter in enumerate(self._environment_parameters):
                item = self.environment_parameter_table.item(row, 1)
                override = overrides.get(parameter.name.casefold())
                value = parameter.base_value if override is None else override
                item.setText(parameter.render(value))
                font = item.font()
                font.setBold(override is not None)
                item.setFont(font)
                if override is None:
                    item.setBackground(QtGui.QBrush())
                    item.setToolTip(
                        "Global RGA value; double-click to create a local override"
                    )
                else:
                    item.setBackground(QtGui.QColor("#fff2b3"))
                    item.setToolTip(
                        "Local override saved for this scan; double-click to edit"
                    )
        finally:
            self._refreshing_environment_table = False
        self._environment_parameter_selected()

    def _environment_table_item_changed(
        self, item: QtWidgets.QTableWidgetItem
    ) -> None:
        if self._refreshing_environment_table or item.column() != 1:
            return
        parameter = self._environment_parameters[item.row()]
        try:
            value = float(item.text())
        except ValueError:
            QtWidgets.QMessageBox.warning(
                self,
                "Invalid environment value",
                f"{parameter.name} requires a numeric value.",
            )
            self._refresh_environment_table_values()
            return
        if not self._set_environment_override(parameter, value):
            self._refresh_environment_table_values()

    def _environment_parameter_selected(self, *ignored: int) -> None:
        del ignored
        row = self.environment_parameter_table.currentRow()
        if not 0 <= row < len(self._environment_parameters):
            self.environment_new_value.setEnabled(False)
            self.environment_change_button.setEnabled(False)
            return
        parameter = self._environment_parameters[row]
        minimum = (
            parameter.minimum
            if parameter.minimum is not None
            else -1_000_000_000.0
        )
        maximum = (
            parameter.maximum
            if parameter.maximum is not None
            else 1_000_000_000.0
        )
        self.environment_new_value.blockSignals(True)
        self.environment_new_value.setDecimals(
            0 if parameter.format_string == "%d" else 2
        )
        self.environment_new_value.setRange(minimum, maximum)
        if parameter.resolution is not None:
            self.environment_new_value.setSingleStep(parameter.resolution)
        value = self._environment_overrides().get(
            parameter.name.casefold(), parameter.base_value
        )
        self.environment_new_value.setValue(value)
        self.environment_new_value.blockSignals(False)

        editable = parameter.name.casefold() != "mass"
        self.environment_new_value.setEnabled(editable)
        self.environment_change_button.setEnabled(editable)
        if editable:
            self.environment_change_button.setToolTip(
                f"Save a local {parameter.name} override for this scan"
            )
        else:
            self.environment_change_button.setToolTip(
                "mass is the scanned parameter and cannot also be a local override"
            )

    def _change_environment_value(self) -> None:
        row = self.environment_parameter_table.currentRow()
        if not 0 <= row < len(self._environment_parameters):
            return
        parameter = self._environment_parameters[row]
        if parameter.name.casefold() == "mass":
            QtWidgets.QMessageBox.warning(
                self,
                "Cannot override mass",
                "mass is the parameter scanned by this definition and cannot also "
                "be changed in its local environment.",
            )
            return

        self._set_environment_override(
            parameter,
            self.environment_new_value.value(),
        )

    def _set_environment_override(
        self,
        parameter: _HidenEnvironmentParameter,
        value: float,
    ) -> bool:
        if parameter.minimum is not None and value < parameter.minimum:
            QtWidgets.QMessageBox.warning(
                self,
                "Invalid environment value",
                f"{parameter.name} must be at least {parameter.minimum:g}.",
            )
            return False
        if parameter.maximum is not None and value > parameter.maximum:
            QtWidgets.QMessageBox.warning(
                self,
                "Invalid environment value",
                f"{parameter.name} must be at most {parameter.maximum:g}.",
            )
            return False
        if parameter.format_string == "%d" and value != round(value):
            QtWidgets.QMessageBox.warning(
                self,
                "Invalid environment value",
                f"{parameter.name} requires an integer value.",
            )
            return False
        replacement = f"lset {parameter.name} {parameter.render(value)}"
        commands = [
            command.strip()
            for command in self.environment_changes_edit.toPlainText().split(",")
            if command.strip()
        ]
        updated: list[str] = []
        replaced = False
        for command in commands:
            match = _HIDEN_LOCAL_ENVIRONMENT_COMMAND.fullmatch(command)
            if (
                match is not None
                and match.group(1).casefold() == parameter.name.casefold()
            ):
                if not replaced:
                    updated.append(replacement)
                    replaced = True
                continue
            updated.append(command)
        if not replaced:
            updated.append(replacement)
        self.environment_changes_edit.setPlainText(", ".join(updated))
        return True

    def _build_scan_tab(self, initial_mass: float) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(tab)
        grid.setColumnStretch(4, 1)

        self.scan_mode_box = QtWidgets.QComboBox()
        self.scan_mode_box.addItem("RGA", 1)
        self.scan_mode_box.setEnabled(False)
        grid.addWidget(QtWidgets.QLabel("Scan mode"), 0, 0)
        grid.addWidget(self.scan_mode_box, 0, 1)

        self.scan_type_box = QtWidgets.QComboBox()
        self.scan_type_box.addItems(("trend", "linear"))
        self.scan_type_box.currentTextChanged.connect(self._scan_type_changed)
        grid.addWidget(QtWidgets.QLabel("Scan type"), 1, 0)
        grid.addWidget(self.scan_type_box, 1, 1)

        self.scan_parameter_box = QtWidgets.QComboBox()
        self.scan_parameter_box.addItem("mass")
        self.scan_parameter_box.setEnabled(False)
        grid.addWidget(QtWidgets.QLabel("Scan parameter"), 2, 0)
        grid.addWidget(self.scan_parameter_box, 2, 1)
        self.scan_start_relation_label = QtWidgets.QLabel("at")
        grid.addWidget(self.scan_start_relation_label, 2, 2)
        self.scan_start_spin = QtWidgets.QDoubleSpinBox()
        self.scan_start_spin.setRange(0.4, 200.0)
        self.scan_start_spin.setDecimals(4)
        self.scan_start_spin.setValue(min(200.0, max(0.4, initial_mass)))
        self.scan_start_spin.setSuffix(" amu")
        grid.addWidget(self.scan_start_spin, 2, 3)

        self.scan_limits_label = QtWidgets.QLabel("Limits 0.4 to 200.0 amu")
        self.scan_limits_label.setObjectName("muted")
        grid.addWidget(self.scan_limits_label, 3, 1, 1, 3)

        self.scan_stop_relation_label = QtWidgets.QLabel("to")
        self.scan_stop_spin = QtWidgets.QDoubleSpinBox()
        self.scan_stop_spin.setRange(0.4, 200.0)
        self.scan_stop_spin.setDecimals(4)
        self.scan_stop_spin.setValue(200.0)
        self.scan_stop_spin.setSuffix(" amu")
        grid.addWidget(self.scan_stop_relation_label, 4, 2)
        grid.addWidget(self.scan_stop_spin, 4, 3)

        self.scan_step_relation_label = QtWidgets.QLabel("with step")
        self.scan_step_spin = QtWidgets.QDoubleSpinBox()
        self.scan_step_spin.setRange(0.0001, 200.0)
        self.scan_step_spin.setDecimals(4)
        self.scan_step_spin.setValue(0.01)
        self.scan_step_spin.setSuffix(" amu")
        grid.addWidget(self.scan_step_relation_label, 5, 2)
        grid.addWidget(self.scan_step_spin, 5, 3)

        self.continuous_scan_check = QtWidgets.QCheckBox("Continuous scan")
        self.continuous_scan_check.setChecked(True)
        self.continuous_scan_check.toggled.connect(self._continuous_scan_changed)
        grid.addWidget(self.continuous_scan_check, 7, 0, 1, 2)
        grid.addWidget(QtWidgets.QLabel("Acquisition cycles"), 6, 2)
        self.acquisition_cycles_spin = QtWidgets.QSpinBox()
        self.acquisition_cycles_spin.setRange(0, 1_000_000)
        self.acquisition_cycles_spin.setValue(0)
        grid.addWidget(self.acquisition_cycles_spin, 7, 2)
        grid.addWidget(QtWidgets.QLabel("Min cycle time (s)"), 6, 3)
        self.minimum_cycle_time_spin = QtWidgets.QDoubleSpinBox()
        self.minimum_cycle_time_spin.setRange(0.0, 86_400.0)
        self.minimum_cycle_time_spin.setDecimals(3)
        self.minimum_cycle_time_spin.setValue(0.0)
        grid.addWidget(self.minimum_cycle_time_spin, 7, 3)
        grid.setRowStretch(8, 1)
        return tab

    def _build_detector_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(tab)
        grid.setColumnStretch(5, 1)

        grid.addWidget(QtWidgets.QLabel("Input device"), 0, 0)
        self.input_device_box = QtWidgets.QComboBox()
        self.input_device_box.addItems(self.INPUT_DEVICES)
        self.input_device_box.setCurrentText("SEM")
        grid.addWidget(self.input_device_box, 0, 1)
        self.detector_range_label = QtWidgets.QLabel()
        self.detector_range_label.setObjectName("muted")
        grid.addWidget(self.detector_range_label, 1, 1, 1, 2)
        self.autozero_check = QtWidgets.QCheckBox("Use Autozero")
        grid.addWidget(self.autozero_check, 0, 3, 1, 2)

        self.start_range_spin = QtWidgets.QSpinBox()
        self.autorange_high_spin = QtWidgets.QSpinBox()
        self.autorange_low_spin = QtWidgets.QSpinBox()
        for spin in (
            self.start_range_spin,
            self.autorange_high_spin,
            self.autorange_low_spin,
        ):
            spin.setRange(-15, 5)
            spin.valueChanged.connect(self._update_detector_range_label)
        self.start_range_spin.setValue(-7)
        self.autorange_high_spin.setValue(-7)
        self.autorange_low_spin.setValue(-13)
        grid.addWidget(QtWidgets.QLabel("Start range"), 2, 0)
        grid.addWidget(self.start_range_spin, 2, 1)
        grid.addWidget(QtWidgets.QLabel("Autorange high"), 3, 0)
        grid.addWidget(self.autorange_high_spin, 3, 1)
        grid.addWidget(QtWidgets.QLabel("Autorange low"), 4, 0)
        grid.addWidget(self.autorange_low_spin, 4, 1)

        self.settle_spin = QtWidgets.QSpinBox()
        self.dwell_spin = QtWidgets.QSpinBox()
        for spin in (self.settle_spin, self.dwell_spin):
            spin.setRange(0, 100)
            spin.setValue(100)
            spin.setSuffix(" %")
        grid.addWidget(QtWidgets.QLabel("Settle"), 2, 2)
        grid.addWidget(self.settle_spin, 2, 3)
        grid.addWidget(QtWidgets.QLabel("Dwell"), 3, 2)
        grid.addWidget(self.dwell_spin, 3, 3)

        self.relative_sensitivity_spin = QtWidgets.QDoubleSpinBox()
        self.relative_gain_spin = QtWidgets.QDoubleSpinBox()
        for spin in (self.relative_sensitivity_spin, self.relative_gain_spin):
            spin.setRange(0.0001, 1_000_000.0)
            spin.setDecimals(4)
            spin.setValue(1.0)
        grid.addWidget(QtWidgets.QLabel("Relative sensitivity"), 2, 4)
        grid.addWidget(self.relative_sensitivity_spin, 2, 5)
        grid.addWidget(QtWidgets.QLabel("Relative SEM"), 3, 4)
        grid.addWidget(self.relative_gain_spin, 3, 5)
        grid.setRowStretch(5, 1)
        return tab

    def _build_advanced_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        form = QtWidgets.QFormLayout()
        self.options_edit = QtWidgets.QLineEdit()
        self.environment_changes_edit = QtWidgets.QPlainTextEdit()
        self.environment_changes_edit.setMaximumHeight(120)
        self.environment_changes_edit.setPlaceholderText(
            "Raw legacy sset env payload; normally leave empty"
        )
        form.addRow("Options", self.options_edit)
        form.addRow("Changes to environment parameters", self.environment_changes_edit)
        layout.addLayout(form)
        note = QtWidgets.QLabel(
            "These map directly to the two legacy scan fields. Local environment "
            "commands use \"lset parameter value\", separated by commas. They are "
            "saved now and transmitted only when a guarded Hiden acquisition starts."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return tab

    def _scan_type_changed(self, scan_type: str) -> None:
        linear = scan_type == "linear"
        self.scan_start_relation_label.setText("from" if linear else "at")
        for widget in (
            self.scan_stop_relation_label,
            self.scan_stop_spin,
            self.scan_step_relation_label,
            self.scan_step_spin,
        ):
            widget.setVisible(linear)

    def _continuous_scan_changed(self, continuous: bool) -> None:
        self.acquisition_cycles_spin.setEnabled(not continuous)
        if continuous:
            self.acquisition_cycles_spin.setValue(0)
        elif self.acquisition_cycles_spin.value() == 0:
            self.acquisition_cycles_spin.setValue(1)

    def _update_detector_range_label(self, ignored: int | None = None) -> None:
        del ignored
        self.detector_range_label.setText(
            f"Configured range {self.autorange_low_spin.value()} "
            f"to {self.autorange_high_spin.value()}"
        )

    def _load_scan_definition(self, scan: Mapping[str, Any]) -> None:
        definition = HidenScanDefinition.from_mapping(scan)
        self.scan_type_box.setCurrentText(
            "trend" if definition.is_single_point else "linear"
        )
        self.scan_start_spin.setValue(definition.start_value)
        self.scan_stop_spin.setValue(definition.stop_value)
        self.scan_step_spin.setValue(abs(definition.increment))
        mode_index = self.scan_mode_box.findData(definition.scan_mode)
        if mode_index >= 0:
            self.scan_mode_box.setCurrentIndex(mode_index)

        input_index = self.input_device_box.findText(definition.input_device)
        if input_index < 0:
            self.input_device_box.addItem(definition.input_device)
            input_index = self.input_device_box.count() - 1
        self.input_device_box.setCurrentIndex(input_index)
        self.autozero_check.setChecked(definition.use_autozero)
        self.autorange_high_spin.setValue(definition.autorange_high)
        self.autorange_low_spin.setValue(definition.autorange_low)
        self.start_range_spin.setValue(definition.start_range)
        self.dwell_spin.setValue(definition.dwell_percent)
        self.settle_spin.setValue(definition.settle_percent)
        self.relative_sensitivity_spin.setValue(definition.relative_sensitivity)
        self.relative_gain_spin.setValue(definition.relative_gain)
        self.minimum_cycle_time_spin.setValue(
            definition.minimum_cycle_time_seconds
        )
        continuous = definition.acquisition_cycles == 0
        self.continuous_scan_check.setChecked(continuous)
        if not continuous:
            self.acquisition_cycles_spin.setValue(definition.acquisition_cycles)
        self.options_edit.setText(definition.options)
        self.environment_changes_edit.setPlainText(definition.environment_changes)
        self._scan_type_changed(self.scan_type_box.currentText())
        self._update_detector_range_label()

    def scan_definition(self) -> dict[str, Any]:
        linear = self.scan_type_box.currentText() == "linear"
        start = self.scan_start_spin.value()
        stop = self.scan_stop_spin.value() if linear else start
        if linear and stop <= start:
            raise ValueError("A linear mass scan requires 'to' to be greater than 'from'")
        return new_hiden_mass_scan(
            start,
            stop_mass=stop,
            increment=self.scan_step_spin.value() if linear else 1.0,
            scan_mode=int(self.scan_mode_box.currentData()),
            input_device=self.input_device_box.currentText(),
            use_autozero=self.autozero_check.isChecked(),
            autorange_high=self.autorange_high_spin.value(),
            autorange_low=self.autorange_low_spin.value(),
            start_range=self.start_range_spin.value(),
            dwell_percent=self.dwell_spin.value(),
            settle_percent=self.settle_spin.value(),
            relative_sensitivity=self.relative_sensitivity_spin.value(),
            relative_gain=self.relative_gain_spin.value(),
            options=self.options_edit.text(),
            environment_changes=self.environment_changes_edit.toPlainText(),
            acquisition_cycles=(
                0 if self.continuous_scan_check.isChecked()
                else self.acquisition_cycles_spin.value()
            ),
            minimum_cycle_time_seconds=self.minimum_cycle_time_spin.value(),
        )

    def _validate_and_accept(self) -> None:
        try:
            self.scan_definition()
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Invalid mass scan", str(exc))
            return
        self.accept()


class NewProgramDialog(QtWidgets.QDialog):
    """Choose a new or empty folder for a complete SpecMass program."""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        initial_directory: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create new SpecMass program")
        self.setMinimumWidth(620)
        layout = QtWidgets.QVBoxLayout(self)
        explanation = QtWidgets.QLabel(
            "Choose a new or empty folder. Stage1.msdef, ScanSettings.msdef, "
            "and future simulation output files will be stored there."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        path_row = QtWidgets.QHBoxLayout()
        self.path_edit = QtWidgets.QLineEdit(
            str((initial_directory or Path.cwd()).resolve())
        )
        path_row.addWidget(self.path_edit, 1)
        browse_button = QtWidgets.QPushButton("Browse…")
        browse_button.clicked.connect(self._browse)
        path_row.addWidget(browse_button)
        layout.addLayout(path_row)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Create program")
        buttons.accepted.connect(self._accept_if_path_present)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def program_path(self) -> Path:
        return Path(self.path_edit.text().strip()).expanduser()

    def _browse(self) -> None:
        current = self.program_path()
        starting_directory = current if current.is_dir() else current.parent
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select new or empty program folder",
            str(starting_directory),
        )
        if chosen:
            self.path_edit.setText(chosen)

    def _accept_if_path_present(self) -> None:
        if not self.path_edit.text().strip():
            QtWidgets.QMessageBox.warning(
                self, "Program path required", "Choose a folder for the new program."
            )
            return
        self.accept()


class SpecMassWindow(QtWidgets.QMainWindow):
    TICK_MS = 100

    def __init__(
        self,
        *,
        initial_program: Path | None = None,
        monitor_backend: Adam4118MonitorBackend | ReadOnlyHardwareMonitorBackend | None = None,
        shadow_backend: HardwareShadowBackend | HidenHardwareShadowBackend | None = None,
        monitor_telemetry: TelemetryWriter | None = None,
        monitor_output: Path | None = None,
        builds_directory: Path | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Mass Spectrometer Station — SpecMass Python simulator")
        self.resize(1600, 900)
        self.setMinimumSize(1180, 720)

        self.program: ProcessProgram | None = None
        self.program_path: Path | None = None
        self.backend: (
            SimulatedBackend | HardwareShadowBackend | HidenHardwareShadowBackend | None
        ) = None
        self.controller: ProcessController | None = None
        self.runtime: SpecMassRuntime | None = None
        self.telemetry: TelemetryWriter | None = monitor_telemetry
        self.monitor_backend = monitor_backend
        self.shadow_backend = shadow_backend
        if self.monitor_backend is not None and self.shadow_backend is not None:
            raise ValueError("Monitor and shadow backends cannot be active together")
        self.monitor_output = monitor_output
        self.builds_directory = builds_directory
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
        self._scan_settings_working: dict[str, Any] | None = None
        self._scan_settings_dirty = False
        self._stage_paths: list[Path] = []
        self._stage_working: list[dict[str, Any]] = []
        self._removed_stage_paths: set[Path] = set()
        self._stage_settings_dirty = False
        self._stage_editor_loading = False
        self._hiden_mass_names = dict(DEFAULT_HIDEN_MASS_NAMES)
        self._hiden_environment_config: HidenEnvironmentConfig | None = None
        if builds_directory is not None:
            try:
                connection = load_hiden_connection(builds_directory)
            except (FileNotFoundError, OSError, TypeError, ValueError):
                pass
            else:
                self._hiden_mass_names.update(
                    {item.mass: item.name for item in connection.masses}
                )
            try:
                self._hiden_environment_config = load_hiden_environment_config(
                    find_hiden_environment_config(builds_directory)
                )
            except (FileNotFoundError, OSError, TypeError, ValueError):
                pass
        self._hiden_global_parameters = (
            MassScanDialog._make_environment_parameters(
                self._hiden_environment_config
            )
        )
        self._hiden_environment_working = {
            parameter.name: parameter.base_value
            for parameter in self._hiden_global_parameters
        }
        self._refreshing_hiden_parameter_table = False

        self._build_layout()
        self._apply_styles()
        if self.shadow_backend is not None:
            self._configure_shadow_mode()

        self.clock_timer = QtCore.QTimer(self)
        self.clock_timer.setInterval(1000)
        self.clock_timer.timeout.connect(self._update_clock)
        self.clock_timer.start()
        self._update_clock()

        self.tick_timer = QtCore.QTimer(self)
        self.tick_timer.setInterval(
            int(getattr(self.shadow_backend, "poll_interval_ms", self.TICK_MS))
        )
        self.tick_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.tick_timer.timeout.connect(self._tick)
        self.monitor_timer = QtCore.QTimer(self)
        self.monitor_timer.setInterval(500)
        self.monitor_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.monitor_timer.timeout.connect(self._monitor_tick)
        self._refresh_program_config()
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
            QListWidget, QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit, QTableWidget {{
                background: white; border: 1px solid #aab0b6; padding: 3px;
            }}
            QListWidget::item:selected {{ background: #087cc1; color: white; }}
            QPushButton#roundAction {{
                border: 3px solid #111111; border-radius: 30px; background: white;
                font-size: 27pt; font-weight: 400; padding: 0px;
            }}
            QPushButton#roundAction:hover {{ background: #e9f4fb; border-color: #087cc1; }}
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

        self.page_stack = QtWidgets.QStackedWidget()
        self.dashboard_page = self._build_dashboard_page()
        self.program_config_page = self._build_program_config_page()
        self.hiden_config_page = self._build_hiden_config_page()
        self.page_stack.addWidget(self.dashboard_page)
        self.page_stack.addWidget(self.program_config_page)
        self.page_stack.addWidget(self.hiden_config_page)
        root_layout.addWidget(self.page_stack, 1)

    def _build_dashboard_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        page_layout = QtWidgets.QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)

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
        page_layout.addWidget(workspace, 1)
        page_layout.addWidget(self._build_footer())
        return page

    @staticmethod
    def _stage_spin(*, suffix: str = "", decimals: int = 3) -> QtWidgets.QDoubleSpinBox:
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(-1_000_000.0, 1_000_000.0)
        spin.setDecimals(decimals)
        spin.setSuffix(suffix)
        return spin

    def _build_program_config_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        outer = QtWidgets.QHBoxLayout(page)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(28)

        left = QtWidgets.QFrame()
        left.setObjectName("panel")
        left.setMinimumWidth(430)
        left.setMaximumWidth(560)
        left_layout = QtWidgets.QVBoxLayout(left)
        toolbar = QtWidgets.QHBoxLayout()
        self.new_program_button = QtWidgets.QPushButton("New\nprogram")
        self.new_program_button.setMinimumSize(92, 58)
        self.new_program_button.clicked.connect(self._create_new_program)
        toolbar.addWidget(self.new_program_button)
        self.open_program_button = QtWidgets.QPushButton("Open\nprogram folder")
        self.open_program_button.setMinimumSize(108, 58)
        self.open_program_button.clicked.connect(self._choose_program)
        toolbar.addWidget(self.open_program_button)
        self.save_program_button = QtWidgets.QPushButton("Save\nprogram")
        self.save_program_button.setMinimumSize(92, 58)
        self.save_program_button.clicked.connect(self._save_stage_settings)
        toolbar.addWidget(self.save_program_button)
        self.stage_table_button = QtWidgets.QPushButton("Stage table")
        self.stage_table_button.setMinimumSize(88, 58)
        self.stage_table_button.clicked.connect(self._show_stages)
        toolbar.addWidget(self.stage_table_button)
        toolbar.addStretch(1)
        left_layout.addLayout(toolbar)
        left_layout.addWidget(self._section_label("Program stages"))
        self.config_program_path_label = QtWidgets.QLabel("No program loaded")
        self.config_program_path_label.setObjectName("muted")
        self.config_program_path_label.setWordWrap(True)
        left_layout.addWidget(self.config_program_path_label)
        self.stage_list = QtWidgets.QListWidget()
        self.stage_list.currentRowChanged.connect(self._populate_stage_editor)
        left_layout.addWidget(self.stage_list, 1)
        stage_actions = QtWidgets.QHBoxLayout()
        self.add_stage_button = QtWidgets.QPushButton("+  Add stage")
        self.add_stage_button.clicked.connect(self._add_stage)
        self.copy_stage_button = QtWidgets.QPushButton("Copy stage")
        self.copy_stage_button.clicked.connect(self._copy_stage)
        self.remove_stage_button = QtWidgets.QPushButton("−  Remove stage")
        self.remove_stage_button.clicked.connect(self._remove_stage)
        stage_actions.addWidget(self.add_stage_button)
        stage_actions.addWidget(self.copy_stage_button)
        stage_actions.addWidget(self.remove_stage_button)
        left_layout.addLayout(stage_actions)
        self.stage_editor_status = QtWidgets.QLabel(
            "Stage changes remain pending until Save program. Removed stage files "
            "are retained in a recovery backup when saved."
        )
        self.stage_editor_status.setObjectName("muted")
        self.stage_editor_status.setWordWrap(True)
        left_layout.addWidget(self.stage_editor_status)

        right = QtWidgets.QFrame()
        right.setObjectName("panel")
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.addWidget(self._section_label("Selected stage configuration"))

        temperature_grid = QtWidgets.QGridLayout()
        self.stage_temperature_mode = QtWidgets.QComboBox()
        self.stage_temperature_mode.addItems(("Isothermal", "Polythermal"))
        self.stage_duration = self._stage_spin(suffix=" s")
        self.stage_duration.setRange(0.0, 100_000_000.0)
        self.stage_start_temperature = self._stage_spin(suffix=" °C")
        self.stage_end_temperature = self._stage_spin(suffix=" °C")
        self.stage_temperature_rate = self._stage_spin(suffix=" °C/min")
        self.stage_auto_start = QtWidgets.QCheckBox("Auto start")
        self.stage_stabilize = QtWidgets.QCheckBox("Stabilize temperature")
        temperature_grid.addWidget(QtWidgets.QLabel("Temperature mode"), 0, 0)
        temperature_grid.addWidget(self.stage_temperature_mode, 1, 0)
        temperature_grid.addWidget(QtWidgets.QLabel("Duration"), 0, 1)
        temperature_grid.addWidget(self.stage_duration, 1, 1)
        temperature_grid.addWidget(QtWidgets.QLabel("Start temperature"), 2, 0)
        temperature_grid.addWidget(self.stage_start_temperature, 3, 0)
        temperature_grid.addWidget(QtWidgets.QLabel("End temperature"), 2, 1)
        temperature_grid.addWidget(self.stage_end_temperature, 3, 1)
        temperature_grid.addWidget(QtWidgets.QLabel("Temperature rise"), 2, 2)
        temperature_grid.addWidget(self.stage_temperature_rate, 3, 2)
        temperature_grid.addWidget(self.stage_auto_start, 4, 0)
        temperature_grid.addWidget(self.stage_stabilize, 4, 1)
        temperature_grid.setColumnStretch(3, 1)
        right_layout.addLayout(temperature_grid)
        right_layout.addWidget(self._separator())

        device_layout = QtWidgets.QGridLayout()
        device_layout.addWidget(self._section_label("Valves"), 0, 0)
        self.stage_valve_list = QtWidgets.QListWidget()
        self.stage_valve_list.currentRowChanged.connect(self._populate_stage_valve)
        device_layout.addWidget(self.stage_valve_list, 1, 0, 4, 1)
        self.stage_valve_state = QtWidgets.QCheckBox("State B (unchecked = A)")
        self.stage_valve_mode = QtWidgets.QComboBox()
        self.stage_valve_mode.addItems(("Constant", "Impulse"))
        self.stage_pulse_length = self._stage_spin(suffix=" s")
        self.stage_pulse_length.setRange(0.0, 100_000_000.0)
        self.stage_pulse_gap = self._stage_spin(suffix=" s")
        self.stage_pulse_gap.setRange(0.0, 100_000_000.0)
        device_layout.addWidget(self.stage_valve_state, 1, 1)
        device_layout.addWidget(QtWidgets.QLabel("Mode"), 2, 1)
        device_layout.addWidget(self.stage_valve_mode, 3, 1)
        device_layout.addWidget(QtWidgets.QLabel("Pulse length"), 2, 2)
        device_layout.addWidget(self.stage_pulse_length, 3, 2)
        device_layout.addWidget(QtWidgets.QLabel("Pulse gap"), 2, 3)
        device_layout.addWidget(self.stage_pulse_gap, 3, 3)

        device_layout.addWidget(self._section_label("Flow Controllers"), 5, 0)
        self.stage_flow_list = QtWidgets.QListWidget()
        self.stage_flow_list.currentRowChanged.connect(self._populate_stage_flow)
        device_layout.addWidget(self.stage_flow_list, 6, 0, 4, 1)
        self.stage_start_flow = self._stage_spin(suffix=" ml/min")
        self.stage_start_flow.setRange(0.0, 1_000_000.0)
        self.stage_end_flow = self._stage_spin(suffix=" ml/min")
        self.stage_end_flow.setRange(0.0, 1_000_000.0)
        self.stage_flow_rate = self._stage_spin(suffix=" ml/min²")
        self.stage_flow_rate.setRange(0.0, 1_000_000.0)
        device_layout.addWidget(QtWidgets.QLabel("Start flow"), 7, 1)
        device_layout.addWidget(self.stage_start_flow, 8, 1)
        device_layout.addWidget(QtWidgets.QLabel("End flow"), 7, 2)
        device_layout.addWidget(self.stage_end_flow, 8, 2)
        device_layout.addWidget(QtWidgets.QLabel("Flow rise"), 7, 3)
        device_layout.addWidget(self.stage_flow_rate, 8, 3)
        device_layout.setColumnStretch(0, 2)
        device_layout.setColumnStretch(4, 1)
        right_layout.addLayout(device_layout, 1)

        self.environment_scan_button = QtWidgets.QPushButton(
            "Environment and scan configuration"
        )
        self.environment_scan_button.setMinimumHeight(46)
        self.environment_scan_button.clicked.connect(self._show_hiden_config)
        right_layout.addWidget(self.environment_scan_button, alignment=QtCore.Qt.AlignHCenter)

        self.stage_temperature_mode.currentIndexChanged.connect(
            self._capture_stage_scalar_fields
        )
        for spin in (
            self.stage_duration,
            self.stage_start_temperature,
            self.stage_end_temperature,
            self.stage_temperature_rate,
        ):
            spin.valueChanged.connect(self._capture_stage_scalar_fields)
        self.stage_auto_start.toggled.connect(self._capture_stage_scalar_fields)
        self.stage_stabilize.toggled.connect(self._capture_stage_scalar_fields)
        self.stage_valve_state.toggled.connect(self._capture_stage_valve_fields)
        self.stage_valve_mode.currentIndexChanged.connect(
            self._capture_stage_valve_fields
        )
        self.stage_pulse_length.valueChanged.connect(self._capture_stage_valve_fields)
        self.stage_pulse_gap.valueChanged.connect(self._capture_stage_valve_fields)
        self.stage_start_flow.valueChanged.connect(self._capture_stage_flow_fields)
        self.stage_end_flow.valueChanged.connect(self._capture_stage_flow_fields)
        self.stage_flow_rate.valueChanged.connect(self._capture_stage_flow_fields)

        outer.addWidget(left)
        outer.addWidget(right, 1)
        return page

    def _build_hiden_config_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        outer = QtWidgets.QHBoxLayout(page)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.setSpacing(28)

        left = QtWidgets.QFrame()
        left.setObjectName("panel")
        left_layout = QtWidgets.QVBoxLayout(left)
        identity_row = QtWidgets.QHBoxLayout()
        identity_column = QtWidgets.QVBoxLayout()
        identity_column.addWidget(QtWidgets.QLabel("Hiden instrument"))
        self.hiden_identity = QtWidgets.QLineEdit(SAVED_HIDEN_IDENTITY)
        self.hiden_identity.setReadOnly(True)
        self.hiden_identity.setToolTip(
            "Saved result of the approved one-time identity query. "
            "Opening this screen never queries COM3."
        )
        identity_column.addWidget(self.hiden_identity)
        identity_row.addLayout(identity_column, 1)
        mode_column = QtWidgets.QVBoxLayout()
        mode_column.addWidget(QtWidgets.QLabel("Global mode"))
        self.hiden_global_mode = QtWidgets.QComboBox()
        self.hiden_global_mode.addItem("RGA")
        self.hiden_global_mode.setEnabled(False)
        mode_column.addWidget(self.hiden_global_mode)
        identity_row.addLayout(mode_column)
        filament_column = QtWidgets.QVBoxLayout()
        filament_column.addWidget(QtWidgets.QLabel("Filament for scan plan"))
        self.hiden_filament = QtWidgets.QComboBox()
        self.hiden_filament.addItems(("F1", "F2", ""))
        self.hiden_filament.currentTextChanged.connect(self._hiden_filament_changed)
        filament_column.addWidget(self.hiden_filament)
        identity_row.addLayout(filament_column)
        left_layout.addLayout(identity_row)
        left_layout.addWidget(self._section_label("Program global environment parameters"))
        self.hiden_parameter_table = QtWidgets.QTableWidget(
            len(self._hiden_global_parameters), 3
        )
        self.hiden_parameter_table.setHorizontalHeaderLabels(
            ("Parameter", "Value", "Description")
        )
        self.hiden_parameter_table.setEditTriggers(
            QtWidgets.QAbstractItemView.DoubleClicked
            | QtWidgets.QAbstractItemView.EditKeyPressed
        )
        self.hiden_parameter_table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows
        )
        for row, parameter in enumerate(self._hiden_global_parameters):
            values = (
                parameter.name,
                parameter.render(parameter.base_value),
                parameter.description,
            )
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if column != 1:
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
                self.hiden_parameter_table.setItem(row, column, item)
        self.hiden_parameter_table.setCurrentCell(0, 0)
        self.hiden_parameter_table.currentCellChanged.connect(
            self._hiden_global_parameter_selected
        )
        self.hiden_parameter_table.itemChanged.connect(
            self._hiden_global_table_item_changed
        )
        self.hiden_parameter_table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.Stretch
        )
        self.hiden_parameter_table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeToContents
        )
        self.hiden_parameter_table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.Stretch
        )
        left_layout.addWidget(self.hiden_parameter_table, 1)
        global_change_row = QtWidgets.QHBoxLayout()
        global_change_row.addWidget(QtWidgets.QLabel("New value"))
        self.hiden_global_new_value = QtWidgets.QDoubleSpinBox()
        global_change_row.addWidget(self.hiden_global_new_value, 1)
        self.hiden_global_change_button = QtWidgets.QPushButton("Change")
        self.hiden_global_change_button.clicked.connect(
            self._change_hiden_global_value
        )
        self.hiden_global_new_value.lineEdit().returnPressed.connect(
            self._change_hiden_global_value
        )
        global_change_row.addWidget(self.hiden_global_change_button)
        left_layout.addLayout(global_change_row)
        self.hiden_environment_path = QtWidgets.QLabel(
            HIDEN_ENVIRONMENT_SETTINGS_NAME
        )
        self.hiden_environment_path.setObjectName("muted")
        self.hiden_environment_path.setWordWrap(True)
        left_layout.addWidget(self.hiden_environment_path)
        self.hiden_autozero_supported = QtWidgets.QCheckBox("Autozero supported")
        self.hiden_autozero_supported.setChecked(True)
        self.hiden_autozero_supported.setEnabled(False)
        left_layout.addWidget(self.hiden_autozero_supported)
        self.hiden_upload_button = QtWidgets.QPushButton("Upload to device")
        self.hiden_upload_button.setEnabled(False)
        self.hiden_upload_button.setToolTip(
            "Direct upload remains disabled. Saved global values and scans are "
            "uploaded only by a guarded Hiden acquisition START."
        )
        left_layout.addWidget(self.hiden_upload_button)

        right = QtWidgets.QFrame()
        right.setObjectName("panel")
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.addWidget(self._section_label("Scans"))
        self.hiden_scan_path = QtWidgets.QLabel("No ScanSettings.msdef loaded")
        self.hiden_scan_path.setObjectName("muted")
        self.hiden_scan_path.setWordWrap(True)
        right_layout.addWidget(self.hiden_scan_path)
        scan_row = QtWidgets.QHBoxLayout()
        self.hiden_scan_list = QtWidgets.QListWidget()
        self.hiden_scan_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection
        )
        self.hiden_scan_list.currentRowChanged.connect(self._hiden_scan_selected)
        self.hiden_scan_list.itemDoubleClicked.connect(
            lambda ignored: self._edit_hiden_scan()
        )
        scan_row.addWidget(self.hiden_scan_list, 1)
        actions = QtWidgets.QVBoxLayout()
        self.add_mass_button = QtWidgets.QPushButton("+")
        self.add_mass_button.setObjectName("roundAction")
        self.add_mass_button.setFixedSize(64, 64)
        self.add_mass_button.setToolTip("Add a trend or linear mass scan")
        self.add_mass_button.clicked.connect(self._add_hiden_mass)
        self.edit_mass_button = QtWidgets.QPushButton("Edit")
        self.edit_mass_button.setFixedSize(64, 42)
        self.edit_mass_button.setToolTip("Edit the selected scan")
        self.edit_mass_button.clicked.connect(self._edit_hiden_scan)
        self.remove_mass_button = QtWidgets.QPushButton("−")
        self.remove_mass_button.setObjectName("roundAction")
        self.remove_mass_button.setFixedSize(64, 64)
        self.remove_mass_button.setToolTip("Remove the selected scan")
        self.remove_mass_button.clicked.connect(self._remove_hiden_scan)
        actions.addWidget(self.add_mass_button)
        actions.addSpacing(14)
        actions.addWidget(self.edit_mass_button)
        actions.addSpacing(14)
        actions.addWidget(self.remove_mass_button)
        actions.addStretch(1)
        scan_row.addLayout(actions)
        right_layout.addLayout(scan_row, 1)

        self.hiden_editor_status = QtWidgets.QLabel(
            "Offline editor — no Hiden command is sent"
        )
        self.hiden_editor_status.setObjectName("muted")
        right_layout.addWidget(self.hiden_editor_status)
        button_row = QtWidgets.QHBoxLayout()
        self.save_scan_settings_button = QtWidgets.QPushButton(
            "Save environment + scans"
        )
        self.save_scan_settings_button.clicked.connect(self._save_hiden_settings)
        self.back_to_stage_button = QtWidgets.QPushButton("Back to stage config")
        self.back_to_stage_button.clicked.connect(self._show_program_config)
        button_row.addWidget(self.save_scan_settings_button)
        button_row.addStretch(1)
        button_row.addWidget(self.back_to_stage_button)
        right_layout.addLayout(button_row)

        outer.addWidget(left, 1)
        outer.addWidget(right, 1)
        return page

    def _build_header(self) -> QtWidgets.QFrame:
        header = QtWidgets.QFrame()
        header.setObjectName("header")
        layout = QtWidgets.QGridLayout(header)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setHorizontalSpacing(10)

        self.load_button = QtWidgets.QPushButton("Monitor\nscreen")
        self.load_button.setMinimumSize(88, 58)
        self.load_button.setToolTip("Return to the live front panel")
        self.load_button.clicked.connect(self._show_dashboard)
        layout.addWidget(self.load_button, 0, 0, 2, 1)

        self.details_button = QtWidgets.QPushButton("Config\nscreen")
        self.details_button.setMinimumSize(88, 58)
        self.details_button.setToolTip("Open program and scan configuration")
        self.details_button.clicked.connect(self._show_program_config)
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
        self.wait_for_cooling_check = QtWidgets.QCheckBox("Wait for cooling")
        self.wait_for_cooling_check.setChecked(True)
        self.wait_for_cooling_check.setToolTip(
            "Keep monitoring and logging with safe outputs until the furnace "
            "reaches the cooling threshold. This choice is captured at START."
        )
        self.cooling_spin = QtWidgets.QDoubleSpinBox()
        self.cooling_spin.setRange(-50.0, 1200.0)
        self.cooling_spin.setValue(50.0)
        self.cooling_spin.setSuffix(" °C")
        self.wait_for_cooling_check.toggled.connect(self.cooling_spin.setEnabled)
        layout.addWidget(run_label)
        layout.addWidget(self.output_label, 1)
        layout.addWidget(self.follow_check)
        layout.addSpacing(18)
        layout.addWidget(self.wait_for_cooling_check)
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

    def _show_dashboard(self, checked: bool = False) -> None:
        del checked
        if (
            self.page_stack.currentWidget() is self.hiden_config_page
            and not self._confirm_leave_hiden_editor()
        ):
            return
        if (
            self.page_stack.currentWidget() is self.program_config_page
            and not self._confirm_stage_changes()
        ):
            return
        self.page_stack.setCurrentWidget(self.dashboard_page)
        self._update_controls()

    def _show_program_config(self, checked: bool = False) -> None:
        del checked
        if self.monitor_backend is not None:
            return
        if (
            self.page_stack.currentWidget() is self.hiden_config_page
            and not self._confirm_leave_hiden_editor()
        ):
            return
        self._refresh_program_config()
        self.page_stack.setCurrentWidget(self.program_config_page)
        self._update_controls()

    def _refresh_program_config(self) -> None:
        selected_row = self.stage_list.currentRow()
        self.stage_list.blockSignals(True)
        self.stage_list.clear()
        if self.program is None or self.program_path is None:
            self.config_program_path_label.setText("No program loaded")
            self.config_program_path_label.setToolTip("")
            self.stage_list.blockSignals(False)
            self._populate_stage_editor(-1)
            self.environment_scan_button.setEnabled(False)
            self.stage_table_button.setEnabled(False)
            self.save_program_button.setEnabled(False)
            return
        resolved = str(self.program_path.resolve())
        self.config_program_path_label.setText(self.program_path.name)
        self.config_program_path_label.setToolTip(resolved)
        self.stage_list.addItems(
            [str(data.get("Name", path.stem)) for path, data in self._stage_documents()]
        )
        self.stage_list.blockSignals(False)
        self.environment_scan_button.setEnabled(not self._running)
        self.stage_table_button.setEnabled(True)
        if self._stage_working:
            self.stage_list.setCurrentRow(
                min(max(0, selected_row), len(self._stage_working) - 1)
            )
        else:
            self._populate_stage_editor(-1)
        self._update_stage_editor_controls()

    def _stage_documents(self) -> tuple[tuple[Path, dict[str, Any]], ...]:
        return tuple(zip(self._stage_paths, self._stage_working, strict=True))

    def _selected_stage_data(self) -> dict[str, Any] | None:
        row = self.stage_list.currentRow()
        if not 0 <= row < len(self._stage_working):
            return None
        return self._stage_working[row]

    def _load_stage_editor_documents(self) -> None:
        if self.program_path is None:
            self._stage_paths = []
            self._stage_working = []
        else:
            documents = load_stage_documents(self.program_path)
            self._stage_paths = [path for path, _ in documents]
            self._stage_working = [copy.deepcopy(data) for _, data in documents]
        self._removed_stage_paths.clear()
        self._stage_settings_dirty = False

    @staticmethod
    def _mapping_values(data: Mapping[str, Any], key: str) -> list[Any]:
        value = data.get(key, [])
        return list(value) if isinstance(value, (list, tuple)) else []

    def _populate_stage_editor(self, row: int) -> None:
        self._stage_editor_loading = True
        data = self._stage_working[row] if 0 <= row < len(self._stage_working) else None
        if data is None:
            self.stage_temperature_mode.setCurrentIndex(0)
            for spin in (
                self.stage_duration,
                self.stage_start_temperature,
                self.stage_end_temperature,
                self.stage_temperature_rate,
            ):
                spin.setValue(0.0)
            self.stage_auto_start.setChecked(False)
            self.stage_stabilize.setChecked(False)
            self.stage_valve_list.clear()
            self.stage_flow_list.clear()
            self._populate_stage_valve(-1)
            self._populate_stage_flow(-1)
            self._stage_editor_loading = False
            self._update_stage_editor_controls()
            return
        mode = TemperatureMode.parse(data.get("TempMode", 0))
        self.stage_temperature_mode.setCurrentIndex(
            0 if mode is TemperatureMode.ISOTHERMAL else 1
        )
        self.stage_duration.setValue(float(data.get("Duration", 0.0)))
        self.stage_start_temperature.setValue(float(data.get("StartTemp", 0.0)))
        self.stage_end_temperature.setValue(float(data.get("EndTemp", 0.0)))
        self.stage_temperature_rate.setValue(float(data.get("TempA", 0.0)))
        self.stage_auto_start.setChecked(bool(data.get("AutoStart", False)))
        self.stage_stabilize.setChecked(bool(data.get("StabilizeTemp", False)))

        valve_states = self._mapping_values(data, "ValveStates")
        self.stage_valve_list.clear()
        self.stage_valve_list.addItems(
            [f"VICIActuator/{index + 1}" for index in range(len(valve_states))]
        )
        if valve_states:
            self.stage_valve_list.setCurrentRow(0)
        else:
            self._populate_stage_valve(-1)

        start_flows = self._mapping_values(data, "StartFlow")
        self.stage_flow_list.clear()
        self.stage_flow_list.addItems(
            [f"Brooks1/{index}" for index in range(len(start_flows))]
        )
        if start_flows:
            self.stage_flow_list.setCurrentRow(0)
        else:
            self._populate_stage_flow(-1)
        self._stage_editor_loading = False
        self._populate_stage_valve(self.stage_valve_list.currentRow())
        self._populate_stage_flow(self.stage_flow_list.currentRow())
        self._update_stage_editor_controls()

    def _populate_stage_valve(self, row: int) -> None:
        was_loading = self._stage_editor_loading
        self._stage_editor_loading = True
        data = self._selected_stage_data()
        states = self._mapping_values(data or {}, "ValveStates")
        if data is None or not 0 <= row < len(states):
            self.stage_valve_state.setChecked(False)
            self.stage_valve_mode.setCurrentIndex(0)
            self.stage_pulse_length.setValue(0.0)
            self.stage_pulse_gap.setValue(0.0)
            self._stage_editor_loading = was_loading
            return
        modes = self._mapping_values(data, "ValveMode")
        lengths = self._mapping_values(data, "ValvePulseLength")
        gaps = self._mapping_values(data, "ValvePulseGap")
        self.stage_valve_state.setChecked(bool(states[row]))
        mode = ValveMode.parse(modes[row]) if row < len(modes) else ValveMode.CONSTANT
        self.stage_valve_mode.setCurrentIndex(0 if mode is ValveMode.CONSTANT else 1)
        length = float(lengths[row]) if row < len(lengths) else 0.0
        gap = float(gaps[row]) if row < len(gaps) else 0.0
        self.stage_pulse_length.setValue(length)
        self.stage_pulse_gap.setValue(gap)
        self._stage_editor_loading = was_loading

    def _populate_stage_flow(self, row: int) -> None:
        was_loading = self._stage_editor_loading
        self._stage_editor_loading = True
        data = self._selected_stage_data()
        starts = self._mapping_values(data or {}, "StartFlow")
        if data is None or not 0 <= row < len(starts):
            self.stage_start_flow.setValue(0.0)
            self.stage_end_flow.setValue(0.0)
            self.stage_flow_rate.setValue(0.0)
            self._stage_editor_loading = was_loading
            return
        ends = self._mapping_values(data, "EndFlow")
        rates = self._mapping_values(data, "FlowA")
        self.stage_start_flow.setValue(float(starts[row]))
        self.stage_end_flow.setValue(float(ends[row]) if row < len(ends) else 0.0)
        rate = float(rates[row]) if row < len(rates) else 0.0
        self.stage_flow_rate.setValue(rate)
        self._stage_editor_loading = was_loading

    def _capture_stage_scalar_fields(self, ignored: object = None) -> None:
        del ignored
        if self._stage_editor_loading:
            return
        data = self._selected_stage_data()
        if data is None:
            return
        data.update(
            {
                "TempMode": (
                    "Isothermal"
                    if self.stage_temperature_mode.currentIndex() == 0
                    else "Polythermal"
                ),
                "Duration": self.stage_duration.value(),
                "StartTemp": self.stage_start_temperature.value(),
                "EndTemp": self.stage_end_temperature.value(),
                "TempA": self.stage_temperature_rate.value(),
                "AutoStart": self.stage_auto_start.isChecked(),
                "StabilizeTemp": self.stage_stabilize.isChecked(),
            }
        )
        self._set_stage_dirty()

    @staticmethod
    def _set_sequence_item(
        data: dict[str, Any],
        key: str,
        row: int,
        value: Any,
        *,
        default: Any,
        minimum_length: int = 0,
    ) -> None:
        values = SpecMassWindow._mapping_values(data, key)
        while len(values) < max(row + 1, minimum_length):
            values.append(default)
        values[row] = value
        data[key] = values

    def _capture_stage_valve_fields(self, ignored: object = None) -> None:
        del ignored
        if self._stage_editor_loading:
            return
        data = self._selected_stage_data()
        row = self.stage_valve_list.currentRow()
        if data is None or row < 0:
            return
        valve_count = len(self._mapping_values(data, "ValveStates"))
        self._set_sequence_item(
            data,
            "ValveStates",
            row,
            int(self.stage_valve_state.isChecked()),
            default=0,
            minimum_length=valve_count,
        )
        self._set_sequence_item(
            data,
            "ValveMode",
            row,
            "Const" if self.stage_valve_mode.currentIndex() == 0 else "Impulse",
            default="Const",
            minimum_length=valve_count,
        )
        self._set_sequence_item(
            data,
            "ValvePulseLength",
            row,
            self.stage_pulse_length.value(),
            default=0.0,
            minimum_length=valve_count,
        )
        self._set_sequence_item(
            data,
            "ValvePulseGap",
            row,
            self.stage_pulse_gap.value(),
            default=0.0,
            minimum_length=valve_count,
        )
        self._set_stage_dirty()

    def _capture_stage_flow_fields(self, ignored: object = None) -> None:
        del ignored
        if self._stage_editor_loading:
            return
        data = self._selected_stage_data()
        row = self.stage_flow_list.currentRow()
        if data is None or row < 0:
            return
        flow_count = len(self._mapping_values(data, "StartFlow"))
        self._set_sequence_item(
            data,
            "StartFlow",
            row,
            self.stage_start_flow.value(),
            default=0.0,
            minimum_length=flow_count,
        )
        self._set_sequence_item(
            data,
            "EndFlow",
            row,
            self.stage_end_flow.value(),
            default=0.0,
            minimum_length=flow_count,
        )
        self._set_sequence_item(
            data,
            "FlowA",
            row,
            self.stage_flow_rate.value(),
            default=0.0,
            minimum_length=flow_count,
        )
        self._set_stage_dirty()

    def _set_stage_dirty(self) -> None:
        self._stage_settings_dirty = True
        self._update_stage_editor_controls()

    def _next_stage_path(self) -> Path:
        assert self.program_path is not None
        numbers = []
        for path in (*self._stage_paths, *self._removed_stage_paths):
            stem = path.stem
            suffix = stem[5:] if stem.lower().startswith("stage") else ""
            if suffix.isdigit():
                numbers.append(int(suffix))
        return self.program_path / f"Stage{max(numbers, default=0) + 1}.msdef"

    def _add_stage(self) -> None:
        if self.program_path is None:
            return
        selected = self._selected_stage_data()
        temperature = float(selected.get("EndTemp", 20.0)) if selected else 20.0
        flow_count = len(self._mapping_values(selected or {}, "StartFlow")) or 4
        valve_count = len(self._mapping_values(selected or {}, "ValveStates")) or 2
        path = self._next_stage_path()
        data = default_stage_mapping(
            path.stem,
            temperature=temperature,
            flow_channels=flow_count,
            valve_channels=valve_count,
        )
        self._stage_paths.append(path)
        self._stage_working.append(data)
        self._set_stage_dirty()
        self._refresh_program_config()
        self.stage_list.setCurrentRow(len(self._stage_working) - 1)

    def _copy_stage(self) -> None:
        selected = self._selected_stage_data()
        if selected is None or self.program_path is None:
            return
        path = self._next_stage_path()
        copied = copy.deepcopy(selected)
        copied["Name"] = path.stem
        self._stage_paths.append(path)
        self._stage_working.append(copied)
        self._set_stage_dirty()
        self._refresh_program_config()
        self.stage_list.setCurrentRow(len(self._stage_working) - 1)

    def _remove_stage(self) -> None:
        row = self.stage_list.currentRow()
        if not 0 <= row < len(self._stage_working):
            return
        if len(self._stage_working) == 1:
            QtWidgets.QMessageBox.warning(
                self, "Cannot remove stage", "A program must contain at least one stage."
            )
            return
        name = str(self._stage_working[row].get("Name", self._stage_paths[row].stem))
        answer = QtWidgets.QMessageBox.question(
            self,
            "Remove stage",
            f"Remove {name} from this program?\n\nThe file is retained in a recovery "
            "backup after Save program.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        path = self._stage_paths.pop(row)
        self._stage_working.pop(row)
        if path.exists():
            self._removed_stage_paths.add(path)
        self._set_stage_dirty()
        self._refresh_program_config()
        self.stage_list.setCurrentRow(min(row, len(self._stage_working) - 1))

    def _update_stage_editor_controls(self) -> None:
        has_program = self.program_path is not None and bool(self._stage_working)
        selected = self.stage_list.currentRow() >= 0
        self.save_program_button.setEnabled(has_program and self._stage_settings_dirty)
        self.add_stage_button.setEnabled(has_program and not self._running)
        self.copy_stage_button.setEnabled(has_program and selected and not self._running)
        self.remove_stage_button.setEnabled(
            has_program and selected and len(self._stage_working) > 1 and not self._running
        )
        if self._stage_settings_dirty:
            self.stage_editor_status.setText(
                "Unsaved program changes — Save program writes stage files atomically."
            )
        elif has_program:
            self.stage_editor_status.setText(
                "Stage files saved. Add, copy, remove, or edit values to make changes."
            )
        else:
            self.stage_editor_status.setText("Create or open a program folder to begin.")

        editor_enabled = has_program and selected and not self._running
        for widget in (
            self.stage_temperature_mode,
            self.stage_duration,
            self.stage_start_temperature,
            self.stage_end_temperature,
            self.stage_temperature_rate,
            self.stage_auto_start,
            self.stage_stabilize,
            self.stage_valve_list,
            self.stage_valve_state,
            self.stage_valve_mode,
            self.stage_pulse_length,
            self.stage_pulse_gap,
            self.stage_flow_list,
            self.stage_start_flow,
            self.stage_end_flow,
            self.stage_flow_rate,
        ):
            widget.setEnabled(editor_enabled)

    def _save_stage_settings(
        self,
        checked: bool = False,
        *,
        announce: bool = True,
    ) -> bool:
        del checked
        if self.program_path is None or not self._stage_working:
            return False
        try:
            validated_stages = tuple(
                ProcessStage.from_mapping(data, default_name=path.stem)
                for path, data in self._stage_documents()
            )
            flow_counts = tuple(
                len(stage.start_flows) for stage in validated_stages
            )
            flow_count = max(flow_counts, default=0)
            backend = self._backend_for_program(flow_counts)
        except (KeyError, TypeError, ValueError) as exc:
            QtWidgets.QMessageBox.critical(self, "Cannot save program", str(exc))
            return False

        backup_root: Path | None = None
        existing_paths = [path for path in self._stage_paths if path.exists()]
        removed_paths = sorted(
            (path for path in self._removed_stage_paths if path.exists()),
            key=lambda path: path.name.lower(),
        )
        try:
            if existing_paths or removed_paths:
                stamp = time.strftime("%Y%m%d_%H%M%S")
                backup_root = (
                    self.program_path
                    / ".specmass-backup"
                    / f"{stamp}_{time.time_ns() % 1_000_000_000:09d}"
                )
                before_directory = backup_root / "before-save"
                removed_directory = backup_root / "removed"
                before_directory.mkdir(parents=True)
                for path in existing_paths:
                    shutil.copy2(path, before_directory / path.name)
                if removed_paths:
                    removed_directory.mkdir(parents=True)
                    for path in removed_paths:
                        shutil.move(str(path), str(removed_directory / path.name))

            for path, data in self._stage_documents():
                save_legacy_json(path, data)
            updated_program = load_program(self.program_path)
            updated_documents = load_stage_documents(self.program_path)
        except (OSError, TypeError, ValueError) as exc:
            recovery = f"\nRecovery copy: {backup_root}" if backup_root else ""
            QtWidgets.QMessageBox.critical(
                self,
                "Cannot save program",
                f"{exc}{recovery}",
            )
            return False

        self.program = updated_program
        self._stage_paths = [path for path, _ in updated_documents]
        self._stage_working = [copy.deepcopy(data) for _, data in updated_documents]
        self._removed_stage_paths.clear()
        self._stage_settings_dirty = False
        self.controller = ProcessController()
        self.controller.load(updated_program)
        self.backend = backend
        self.runtime = None
        self._program_duration = sum(
            stage.effective_duration_seconds() for stage in updated_program.stages
        )
        self._mass_names = tuple(
            mass_stimuli_from_scan_settings(updated_program.scan_settings)
        )
        self.flow_plot.configure_series(
            tuple(f"Ch{index}" for index in range(flow_count)), FLOW_COLORS
        )
        self.mass_plot.configure_series(self._mass_names, MASS_COLORS)
        self._build_flow_channels(flow_count)
        self._update_filament(updated_program)
        self.status_label.setText("Ready For Start")
        self.setpoint_label.setText(
            f"{updated_program.stages[0].start_temperature:.2f}"
        )
        self.remaining_label.setText(_format_elapsed(self._program_duration))
        self._refresh_program_config()
        self._update_device_states()
        self._update_controls()
        if announce:
            message = "Stage files saved using atomic replacement."
            if backup_root is not None:
                message += f" Recovery copy: {backup_root}"
            self.stage_editor_status.setText(message)
        return True

    def _confirm_stage_changes(self) -> bool:
        if not self._stage_settings_dirty:
            return True
        answer = QtWidgets.QMessageBox.warning(
            self,
            "Unsaved stage settings",
            "Save the program stage changes before leaving this screen?",
            QtWidgets.QMessageBox.Save
            | QtWidgets.QMessageBox.Discard
            | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Save,
        )
        if answer == QtWidgets.QMessageBox.Save:
            return self._save_stage_settings(announce=False)
        if answer == QtWidgets.QMessageBox.Discard:
            try:
                self._load_stage_editor_documents()
            except (OSError, TypeError, ValueError) as exc:
                QtWidgets.QMessageBox.critical(
                    self, "Cannot discard program changes", str(exc)
                )
                return False
            self._refresh_program_config()
            return True
        return False

    def _show_hiden_config(self) -> None:
        if self.program is None or self.program_path is None or self._running:
            return
        if not self._confirm_stage_changes():
            return
        try:
            self._load_hiden_editor()
        except (OSError, TypeError, ValueError) as exc:
            QtWidgets.QMessageBox.critical(
                self, "Cannot load Hiden settings", str(exc)
            )
            return
        self.page_stack.setCurrentWidget(self.hiden_config_page)
        self._update_controls()

    def _load_hiden_editor(self) -> None:
        assert self.program is not None
        assert self.program_path is not None
        settings = copy.deepcopy(dict(self.program.scan_settings))
        raw_scans = settings.get("ScansParameters", [])
        if not isinstance(raw_scans, list):
            raw_scans = list(raw_scans) if isinstance(raw_scans, tuple) else []
        settings["ScansParameters"] = raw_scans
        settings.setdefault("Filament", "F1")
        self._scan_settings_working = settings
        self._scan_settings_dirty = False

        self.hiden_filament.blockSignals(True)
        filament = str(settings.get("Filament", "F1")).upper()
        index = self.hiden_filament.findText(filament)
        self.hiden_filament.setCurrentIndex(max(0, index))
        self.hiden_filament.blockSignals(False)
        scan_path = self.program_path / "ScanSettings.msdef"
        self.hiden_scan_path.setText(str(scan_path))
        self.hiden_scan_path.setToolTip(str(scan_path.resolve()))
        self._load_hiden_environment_editor()
        self._refresh_hiden_scan_list()
        self._update_hiden_editor_controls()

    def _load_hiden_environment_editor(self) -> None:
        assert self.program_path is not None
        values = {
            parameter.name: parameter.base_value
            for parameter in self._hiden_global_parameters
        }
        if self._hiden_environment_config is not None:
            settings = load_hiden_environment_settings(
                self.program_path,
                self._hiden_environment_config,
            )
        else:
            path = self.program_path / HIDEN_ENVIRONMENT_SETTINGS_NAME
            if path.is_file():
                raw = load_legacy_json(path)
                if not isinstance(raw, Mapping):
                    raise ValueError(
                        f"Hiden environment settings must be an object: {path}"
                    )
                settings = HidenEnvironmentSettings.from_mapping(raw)
            else:
                settings = HidenEnvironmentSettings(mode="RGA", values=())
        known_names = {name.casefold(): name for name in values}
        for name, value in settings.values:
            canonical = known_names.get(name.casefold())
            if canonical is not None:
                values[canonical] = value
        self._hiden_environment_working = values
        path = self.program_path / HIDEN_ENVIRONMENT_SETTINGS_NAME
        self.hiden_environment_path.setText(str(path))
        self.hiden_environment_path.setToolTip(str(path.resolve()))
        self._refresh_hiden_global_table()

    def _current_hiden_environment_settings(self) -> HidenEnvironmentSettings:
        return HidenEnvironmentSettings(
            mode="RGA",
            values=tuple(
                (
                    parameter.name,
                    self._hiden_environment_working[parameter.name],
                )
                for parameter in self._hiden_global_parameters
            ),
        )

    def _working_hiden_environment_config(
        self,
    ) -> HidenEnvironmentConfig | None:
        if self._hiden_environment_config is None:
            return None
        return apply_hiden_environment_settings(
            self._hiden_environment_config,
            self._current_hiden_environment_settings(),
        )

    def _refresh_hiden_global_table(self) -> None:
        self._refreshing_hiden_parameter_table = True
        try:
            for row, parameter in enumerate(self._hiden_global_parameters):
                value = self._hiden_environment_working.get(
                    parameter.name, parameter.base_value
                )
                item = self.hiden_parameter_table.item(row, 1)
                item.setText(parameter.render(value))
                changed = value != parameter.base_value
                font = item.font()
                font.setBold(changed)
                item.setFont(font)
                if changed:
                    item.setBackground(QtGui.QColor("#fff2b3"))
                    item.setToolTip(
                        "Program global value differs from the Builds snapshot; "
                        "double-click to edit"
                    )
                else:
                    item.setBackground(QtGui.QBrush())
                    item.setToolTip(
                        "Program global value; double-click to edit"
                    )
        finally:
            self._refreshing_hiden_parameter_table = False
        self._hiden_global_parameter_selected()

    def _hiden_global_parameter_selected(self, *ignored: int) -> None:
        del ignored
        row = self.hiden_parameter_table.currentRow()
        has_editor = self._scan_settings_working is not None
        if not 0 <= row < len(self._hiden_global_parameters):
            self.hiden_global_new_value.setEnabled(False)
            self.hiden_global_change_button.setEnabled(False)
            return
        parameter = self._hiden_global_parameters[row]
        minimum = (
            parameter.minimum
            if parameter.minimum is not None
            else -1_000_000_000.0
        )
        maximum = (
            parameter.maximum
            if parameter.maximum is not None
            else 1_000_000_000.0
        )
        self.hiden_global_new_value.blockSignals(True)
        self.hiden_global_new_value.setDecimals(
            0 if parameter.format_string == "%d" else 2
        )
        self.hiden_global_new_value.setRange(minimum, maximum)
        if parameter.resolution is not None:
            self.hiden_global_new_value.setSingleStep(parameter.resolution)
        self.hiden_global_new_value.setValue(
            self._hiden_environment_working.get(
                parameter.name, parameter.base_value
            )
        )
        self.hiden_global_new_value.blockSignals(False)
        self.hiden_global_new_value.setEnabled(has_editor)
        self.hiden_global_change_button.setEnabled(has_editor)

    def _hiden_global_table_item_changed(
        self, item: QtWidgets.QTableWidgetItem
    ) -> None:
        if self._refreshing_hiden_parameter_table or item.column() != 1:
            return
        parameter = self._hiden_global_parameters[item.row()]
        try:
            value = float(item.text())
        except ValueError:
            QtWidgets.QMessageBox.warning(
                self,
                "Invalid global environment value",
                f"{parameter.name} requires a numeric value.",
            )
            self._refresh_hiden_global_table()
            return
        if not self._set_hiden_global_value(parameter, value):
            self._refresh_hiden_global_table()

    def _change_hiden_global_value(self) -> None:
        row = self.hiden_parameter_table.currentRow()
        if not 0 <= row < len(self._hiden_global_parameters):
            return
        self._set_hiden_global_value(
            self._hiden_global_parameters[row],
            self.hiden_global_new_value.value(),
        )

    def _set_hiden_global_value(
        self,
        parameter: _HidenEnvironmentParameter,
        value: float,
    ) -> bool:
        if parameter.minimum is not None and value < parameter.minimum:
            QtWidgets.QMessageBox.warning(
                self,
                "Invalid global environment value",
                f"{parameter.name} must be at least {parameter.minimum:g}.",
            )
            return False
        if parameter.maximum is not None and value > parameter.maximum:
            QtWidgets.QMessageBox.warning(
                self,
                "Invalid global environment value",
                f"{parameter.name} must be at most {parameter.maximum:g}.",
            )
            return False
        if parameter.format_string == "%d" and value != round(value):
            QtWidgets.QMessageBox.warning(
                self,
                "Invalid global environment value",
                f"{parameter.name} requires an integer value.",
            )
            return False
        rendered_value = (
            float(round(value))
            if parameter.format_string == "%d"
            else float(parameter.render(value))
        )
        if self._hiden_environment_working.get(parameter.name) == rendered_value:
            self._refresh_hiden_global_table()
            return True
        self._hiden_environment_working[parameter.name] = rendered_value
        self._set_hiden_dirty()
        self._refresh_hiden_global_table()
        return True

    def _refresh_hiden_scan_list(self, *, select_row: int | None = None) -> None:
        self.hiden_scan_list.clear()
        scans = self._working_scans()
        for index, scan in enumerate(scans):
            try:
                label = hiden_scan_label(scan, names_by_mass=self._hiden_mass_names)
            except (KeyError, TypeError, ValueError) as exc:
                label = f"Invalid scan {index + 1}: {exc}"
            item = QtWidgets.QListWidgetItem(f"{index + 1:02d}   {label}")
            item.setData(QtCore.Qt.UserRole, index)
            self.hiden_scan_list.addItem(item)
        if scans:
            requested = 0 if select_row is None else select_row
            self.hiden_scan_list.setCurrentRow(min(max(0, requested), len(scans) - 1))
        self._update_hiden_editor_controls()

    def _working_scans(self) -> list[Mapping[str, Any]]:
        if self._scan_settings_working is None:
            return []
        scans = self._scan_settings_working.setdefault("ScansParameters", [])
        if not isinstance(scans, list):
            raise TypeError("ScansParameters must be a list")
        return scans

    def _hiden_filament_changed(self, filament: str) -> None:
        if self._scan_settings_working is None:
            return
        self._scan_settings_working["Filament"] = filament
        self._set_hiden_dirty()

    def _hiden_scan_selected(self, row: int) -> None:
        selected = row >= 0 and bool(self._working_scans())
        self.edit_mass_button.setEnabled(selected)
        self.remove_mass_button.setEnabled(selected)

    def _add_hiden_mass(self) -> None:
        scans = self._working_scans()
        dialog = MassScanDialog(
            self,
            scan_number=len(scans) + 1,
            environment_config=self._working_hiden_environment_config(),
        )
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        scans.append(dialog.scan_definition())
        self._set_hiden_dirty()
        self._refresh_hiden_scan_list(select_row=len(scans) - 1)

    def _edit_hiden_scan(self) -> None:
        row = self.hiden_scan_list.currentRow()
        scans = self._working_scans()
        if not 0 <= row < len(scans):
            return
        original = scans[row]
        dialog = MassScanDialog(
            self,
            initial_scan=original,
            scan_number=row + 1,
            environment_config=self._working_hiden_environment_config(),
        )
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        updated = copy.deepcopy(dict(original))
        updated.update(dialog.scan_definition())
        scans[row] = updated
        self._set_hiden_dirty()
        self._refresh_hiden_scan_list(select_row=row)

    def _remove_hiden_scan(self) -> None:
        row = self.hiden_scan_list.currentRow()
        scans = self._working_scans()
        if not 0 <= row < len(scans):
            return
        label = self.hiden_scan_list.item(row).text()
        answer = QtWidgets.QMessageBox.question(
            self,
            "Remove scan",
            f"Remove this scan definition?\n\n{label}",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        del scans[row]
        self._set_hiden_dirty()
        self._refresh_hiden_scan_list(select_row=min(row, len(scans) - 1))

    def _set_hiden_dirty(self) -> None:
        self._scan_settings_dirty = True
        self._update_hiden_editor_controls()

    def _update_hiden_editor_controls(self) -> None:
        has_editor = self._scan_settings_working is not None
        scans = self._working_scans() if has_editor else []
        selected = self.hiden_scan_list.currentRow() >= 0
        self.add_mass_button.setEnabled(has_editor)
        self.edit_mass_button.setEnabled(has_editor and bool(scans) and selected)
        self.remove_mass_button.setEnabled(
            has_editor and bool(scans) and selected
        )
        self.hiden_parameter_table.setEnabled(has_editor)
        self._hiden_global_parameter_selected()
        self.save_scan_settings_button.setEnabled(
            has_editor and bool(scans) and self._scan_settings_dirty
        )
        if self._scan_settings_dirty:
            self.hiden_editor_status.setText(
                "Unsaved environment/scan changes; still offline and no "
                "device command has been sent"
            )
        elif has_editor:
            self.hiden_editor_status.setText(
                "Saved/offline editor; global values are applied only by "
                "a guarded acquisition START"
            )
        else:
            self.hiden_editor_status.setText("No scan settings loaded")

    def _save_hiden_settings(
        self,
        checked: bool = False,
        *,
        announce: bool = True,
    ) -> bool:
        del checked
        if self._scan_settings_working is None or self.program_path is None:
            return False
        try:
            HidenScanPlan.from_mapping(self._scan_settings_working)
            environment_settings = self._current_hiden_environment_settings()
            if self._hiden_environment_config is not None:
                validate_hiden_environment_settings(
                    environment_settings,
                    self._hiden_environment_config,
                )
            scan_path = self.program_path / "ScanSettings.msdef"
            environment_path = (
                self.program_path / HIDEN_ENVIRONMENT_SETTINGS_NAME
            )
            save_legacy_json(scan_path, self._scan_settings_working)
            save_legacy_json(
                environment_path,
                environment_settings.to_mapping(),
            )
        except (OSError, TypeError, ValueError) as exc:
            QtWidgets.QMessageBox.critical(
                self, "Cannot save Hiden settings", str(exc)
            )
            return False
        assert self.program is not None
        settings = copy.deepcopy(self._scan_settings_working)
        self.program = ProcessProgram(stages=self.program.stages, scan_settings=settings)
        self._mass_names = tuple(mass_stimuli_from_scan_settings(settings))
        self.mass_plot.configure_series(self._mass_names, MASS_COLORS)
        self._update_filament(self.program)
        self._scan_settings_dirty = False
        self._update_hiden_editor_controls()
        if announce:
            self.hiden_editor_status.setText(
                f"Saved {environment_path.name} and {scan_path.name}; "
                "no Hiden command sent"
            )
        return True

    def _confirm_leave_hiden_editor(self) -> bool:
        if not self._scan_settings_dirty:
            return True
        answer = QtWidgets.QMessageBox.warning(
            self,
            "Unsaved Hiden settings",
            "Save the environment and scan changes before leaving this screen?",
            QtWidgets.QMessageBox.Save
            | QtWidgets.QMessageBox.Discard
            | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Save,
        )
        if answer == QtWidgets.QMessageBox.Save:
            return self._save_hiden_settings(announce=False)
        if answer == QtWidgets.QMessageBox.Discard:
            self._scan_settings_working = None
            self._scan_settings_dirty = False
            return True
        return False

    def _choose_program(self) -> None:
        if not self._confirm_stage_changes():
            return
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select folder containing Stage*.msdef",
            str(self.program_path or Path.cwd()),
        )
        if chosen:
            self.load_program(Path(chosen))

    def _create_new_program(self) -> None:
        if self._running or not self._confirm_stage_changes():
            return
        parent = self.program_path.parent if self.program_path is not None else Path.cwd()
        dialog = NewProgramDialog(
            self,
            initial_directory=parent / "New SpecMass Program",
        )
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        try:
            root = create_program_directory(dialog.program_path())
        except (OSError, TypeError, ValueError) as exc:
            QtWidgets.QMessageBox.critical(self, "Cannot create program", str(exc))
            return
        self.load_program(root)
        if self.program_path == root:
            self._show_program_config()

    def _configure_shadow_mode(self) -> None:
        hiden_enabled = bool(
            getattr(self.shadow_backend, "hiden_commands_enabled", False)
        )
        if hiden_enabled:
            self.setWindowTitle(
                "Mass Spectrometer Station — SpecMass Hiden acquisition shadow run"
            )
            self.mode_banner.setText(
                "HIDEN ACQUISITION SHADOW RUN — COM3 SCAN/FILAMENT COMMANDS ENABLED; "
                "HEATER, VALVE, AND FLOW OUTPUTS DISABLED"
            )
        else:
            self.setWindowTitle(
                "Mass Spectrometer Station — SpecMass hardware shadow run"
            )
            self.mode_banner.setText(
                "HARDWARE SHADOW RUN — LIVE ADAM4118/BROOKS READS; "
                "HEATER, VALVE, FLOW, AND HIDEN WRITES DISABLED"
            )
        self.mode_banner.setStyleSheet(
            "background:#fff4cc; color:#714b00; font-weight:700; padding:4px;"
        )
        if hiden_enabled:
            self.wait_for_cooling_check.setToolTip(
                "Continue live monitoring, Hiden acquisition, and shadow logging until "
                "the measured furnace temperature reaches this threshold. Heater, "
                "valve, and flow outputs remain disabled."
            )
        else:
            self.wait_for_cooling_check.setToolTip(
                "Continue live read-only monitoring and shadow logging until the measured "
                "furnace temperature reaches this threshold. No output is sent."
            )

    def _backend_for_program(
        self, flow_counts: tuple[int, ...]
    ) -> SimulatedBackend | HardwareShadowBackend | HidenHardwareShadowBackend:
        flow_count = max(flow_counts, default=0)
        if self.shadow_backend is None:
            return SimulatedBackend(flow_channels=flow_count)
        observed_flow_count = len(self.shadow_backend.flow_channel_names)
        if not flow_counts or any(
            count != observed_flow_count for count in flow_counts
        ):
            raise ValueError(
                f"Every shadow stage must define exactly {observed_flow_count} "
                f"Brooks channels; stage channel counts are {flow_counts}."
            )
        return self.shadow_backend

    def _validate_hiden_program_binding(self, path: Path) -> None:
        if not getattr(self.shadow_backend, "hiden_commands_enabled", False):
            return
        assert self.shadow_backend is not None
        expected_directory = getattr(
            self.shadow_backend,
            "program_directory",
            None,
        )
        if expected_directory is not None and path.resolve() != expected_directory:
            raise ValueError(
                "Hiden acquisition is locked to the program selected on the command "
                "line. Close the application and restart it with the other program."
            )
        expected_plan = getattr(self.shadow_backend, "scan_plan", None)
        if expected_plan is not None and load_hiden_scan_plan(path) != expected_plan:
            raise ValueError(
                "ScanSettings.msdef changed after Hiden acquisition was prepared. "
                "Close and restart the application so the guarded COM3 driver uses "
                "the reviewed scan plan."
            )
        expected_environment = getattr(
            self.shadow_backend,
            "environment_settings",
            None,
        )
        if (
            expected_environment is not None
            and self._hiden_environment_config is not None
            and load_hiden_environment_settings(
                path,
                self._hiden_environment_config,
            )
            != expected_environment
        ):
            raise ValueError(
                f"{HIDEN_ENVIRONMENT_SETTINGS_NAME} changed after Hiden "
                "acquisition was prepared. Close and restart the application so "
                "the guarded COM3 driver uses the reviewed global environment."
            )

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
            self._validate_hiden_program_binding(path)
            flow_counts = tuple(len(stage.start_flows) for stage in program.stages)
            flow_count = max(flow_counts, default=0)
            backend = self._backend_for_program(flow_counts)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Cannot load program", str(exc))
            return

        self._close_telemetry()
        self.program = program
        self.program_path = path
        self.backend = backend
        self.controller = ProcessController()
        self.controller.load(program)
        self.runtime = None
        self._program_duration = sum(stage.effective_duration_seconds() for stage in program.stages)
        live_mass_stimuli = getattr(backend, "mass_stimuli", None)
        self._mass_names = tuple(
            live_mass_stimuli
            if live_mass_stimuli is not None
            else mass_stimuli_from_scan_settings(program.scan_settings)
        )
        self._scan_settings_working = None
        self._scan_settings_dirty = False
        try:
            self._load_stage_editor_documents()
        except (OSError, TypeError, ValueError) as exc:
            QtWidgets.QMessageBox.critical(
                self, "Cannot load editable stage files", str(exc)
            )
            self.program = None
            self.program_path = None
            self.backend = None
            self.controller = None
            return

        resolved = str(path.resolve())
        self.program_label.setText(path.name)
        self.program_label.setToolTip(resolved)
        self.status_label.setText("Ready For Start")
        self.stage_label.setText("—")
        self.temperature_label.setText(
            "Waiting for live read"
            if self.shadow_backend is not None
            else f"{self.backend.temperature:.2f} °C"
        )
        self.setpoint_label.setText(f"{program.stages[0].start_temperature:.2f}")
        self.heater_label.setText("0.0 %")
        self.remaining_label.setText(_format_elapsed(self._program_duration))
        self.process_time_label.setText(_format_elapsed(0.0))
        self.output_label.setText("No run log")
        self.output_label.setToolTip("")

        temperature_series = (
            (*self.shadow_backend.channel_names, "Setpoint")
            if self.shadow_backend is not None
            else ("Temperature", "Setpoint")
        )
        self.temperature_plot.configure_series(
            tuple(temperature_series),
            ("#111111", "#df3e3e", "#26b83f"),
        )
        self.flow_plot.configure_series(tuple(f"Ch{index}" for index in range(flow_count)), FLOW_COLORS)
        self.mass_plot.configure_series(self._mass_names, MASS_COLORS)
        self._build_flow_channels(flow_count)
        self._update_filament(program)
        self._update_device_states()
        self._refresh_program_config()
        self._update_controls()

    def _simulation_output_path(self, suffix: str) -> Path:
        if self.program_path is None:
            raise RuntimeError("No program folder is loaded")
        normalized_suffix = suffix if suffix.startswith(".") else f".{suffix}"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        if getattr(self.shadow_backend, "hiden_commands_enabled", False):
            prefix = "specmass_hiden_shadow"
        elif self.shadow_backend is not None:
            prefix = "specmass_shadow"
        else:
            prefix = "specmass_sim"
        candidate = self.program_path / f"{prefix}_{stamp}{normalized_suffix}"
        counter = 2
        while candidate.exists():
            candidate = (
                self.program_path
                / f"{prefix}_{stamp}_{counter}{normalized_suffix}"
            )
            counter += 1
        return candidate

    def _start(self) -> None:
        if (
            not self.program
            or self.program_path is None
            or not self.controller
            or not self.backend
        ):
            return
        hiden_enabled = bool(
            getattr(self.shadow_backend, "hiden_commands_enabled", False)
        )
        try:
            if hiden_enabled:
                self._validate_hiden_program_binding(self.program_path)
            self.controller.cooling_temperature = self._configured_cooling_temperature()
            self.controller.start(0.0)
            if hiden_enabled:
                assert self.shadow_backend is not None
                self.shadow_backend.start_acquisition()
        except Exception as exc:
            if hiden_enabled and self.shadow_backend is not None:
                self.shadow_backend.safe_shutdown()
            self.controller = ProcessController()
            self.controller.load(self.program)
            QtWidgets.QMessageBox.critical(self, "Cannot start", str(exc))
            return

        flow_count = max((len(stage.start_flows) for stage in self.program.stages), default=0)
        temperature_names = (
            tuple(self.shadow_backend.channel_names)
            if self.shadow_backend is not None
            else ("Temperature",)
        )
        mass_stimuli = dict(
            getattr(
                self.shadow_backend,
                "mass_stimuli",
                mass_stimuli_from_scan_settings(self.program.scan_settings),
            )
        )
        try:
            if importlib.util.find_spec("nptdms") is not None:
                output_path = self._simulation_output_path(".tdms")
                root_properties = (
                    {
                        "SpecMass_Mode": (
                            "HardwareShadowHidenAcquisition"
                            if hiden_enabled
                            else "HardwareShadowRun"
                        ),
                        "SpecMass_OutputCommandsEnabled": int(hiden_enabled),
                        "SpecMass_HidenScanCommandsEnabled": int(hiden_enabled),
                        "SpecMass_HeaterValveFlowWritesEnabled": 0,
                        "SpecMass_ReadDevices": (
                            "ADAM4118,Brooks1,MSDevTh"
                            if hiden_enabled
                            else "ADAM4118,Brooks1"
                        ),
                    }
                    if self.shadow_backend is not None
                    else None
                )
                self.telemetry = TdmsTelemetryWriter(
                    output_path,
                    flow_channels=flow_count,
                    mass_stimuli=mass_stimuli,
                    nominal_increment_seconds=self.tick_timer.interval() / 1000.0,
                    temperature_names=temperature_names,
                    root_properties=root_properties,
                )
            else:
                output_path = self._simulation_output_path(".csv")
                self.telemetry = CsvTelemetryWriter(
                    output_path,
                    flow_channels=flow_count,
                    mass_names=self._mass_names,
                    temperature_names=tuple(
                        name.lower() for name in temperature_names
                    ),
                )
        except Exception as exc:
            if hiden_enabled and self.shadow_backend is not None:
                self.shadow_backend.safe_shutdown()
            self._close_telemetry()
            self.controller = ProcessController()
            self.controller.load(self.program)
            QtWidgets.QMessageBox.critical(
                self, "Cannot create run log", str(exc)
            )
            return
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
        self._update_filament(self.program)
        self._update_device_states()
        self._update_controls()
        self.tick_timer.start()

    def _configured_cooling_temperature(self) -> float | None:
        if not self.wait_for_cooling_check.isChecked():
            return None
        return self.cooling_spin.value()

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
            if self.program is not None:
                self._update_filament(self.program)
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
        temperatures = frame.snapshot.temperatures or (frame.snapshot.temperature,)
        self.temperature_plot.append(
            timestamp,
            (*temperatures, command.temperature_setpoint),
        )
        self.flow_plot.append(timestamp, tuple(frame.snapshot.flows))
        masses = frame.snapshot.masses or {}
        self.mass_plot.append(timestamp, tuple(masses.get(name) for name in self._mass_names))
        self._update_flow_info(command, frame.snapshot.flows)
        self._update_controls()
        if status.completed:
            self._running = False
            self.tick_timer.stop()
            try:
                self._close_telemetry()
            finally:
                if self.shadow_backend is not None:
                    self.shadow_backend.safe_shutdown()
            self.status_label.setText("Ready")
            if self.program is not None:
                self._update_filament(self.program)
            self._update_device_states()
            self._update_controls()

    def _stop(self) -> None:
        if self.controller is not None:
            self.controller.stop()
        if self.backend is not None and self.shadow_backend is None:
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
        if self.shadow_backend is not None:
            self.flow_info_label.setText(
                f"Brooks1/{channel}: live measured input; program setpoint is "
                "calculated and logged, never sent."
            )
        else:
            self.flow_info_label.setText(
                f"Application Ch{channel} = physical Brooks channel {channel + 1}. "
                "Program writes only when the requested value changes."
            )

    def _apply_selected_flow(self) -> None:
        if self.controller is None or self.shadow_backend is not None:
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
        if self.shadow_backend is not None:
            if getattr(self.shadow_backend, "hiden_commands_enabled", False):
                filament = str(program.scan_settings.get("Filament", "")).upper()
                self.f1_lamp.set_state(
                    "running" if self._running and filament == "F1" else "disabled"
                )
                self.f2_lamp.set_state(
                    "running" if self._running and filament == "F2" else "disabled"
                )
                return
            self.f1_lamp.set_state("disabled")
            self.f2_lamp.set_state("disabled")
            return
        filament = str(program.scan_settings.get("Filament", "")).upper()
        self.f1_lamp.set_state("running" if filament == "F1" else "disabled")
        self.f2_lamp.set_state("running" if filament == "F2" else "disabled")

    def _update_device_states(self, *, error: bool = False) -> None:
        if self.shadow_backend is not None:
            monitored_devices = set(self.shadow_backend.monitored_devices)
            hiden_enabled = bool(
                getattr(self.shadow_backend, "hiden_commands_enabled", False)
            )
            for name, label in self._device_state_labels.items():
                if name in monitored_devices:
                    if error:
                        state = "Error"
                    elif hiden_enabled and name == "MSDevTh":
                        state = "Scanning" if self._running else "Standby / Closed"
                    elif self._running:
                        state = "Shadow Running"
                    else:
                        state = "Read Only"
                    lamp_state = "error" if error else "running"
                else:
                    state = "Disabled"
                    lamp_state = "disabled"
                label.setText(state)
                self._device_lamps[name].set_state(lamp_state)
            return
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
            self.load_button.setEnabled(True)
            self.details_button.setEnabled(False)
            self.new_program_button.setEnabled(False)
            self.open_program_button.setEnabled(False)
            self.save_program_button.setEnabled(False)
            self.stage_table_button.setEnabled(False)
            self.add_stage_button.setEnabled(False)
            self.copy_stage_button.setEnabled(False)
            self.remove_stage_button.setEnabled(False)
            self.environment_scan_button.setEnabled(False)
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.continue_button.setEnabled(False)
            self.apply_flows_button.setEnabled(False)
            self.flow_mode_box.setEnabled(False)
            self.flow_value_spin.setEnabled(False)
            self.wait_for_cooling_check.setEnabled(False)
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
        on_dashboard = self.page_stack.currentWidget() is self.dashboard_page
        self.load_button.setEnabled(True)
        self.details_button.setEnabled(not self._running)
        self.new_program_button.setEnabled(not self._running)
        self.open_program_button.setEnabled(not self._running)
        self.stage_table_button.setEnabled(self.program is not None and not self._running)
        self.environment_scan_button.setEnabled(
            bool(self.program and not self._running)
        )
        self.start_button.setEnabled(
            bool(
                self.program
                and state is MSMState.READY_FOR_START
                and not self._running
                and on_dashboard
            )
        )
        self.stop_button.setEnabled(self._running)
        self.continue_button.setEnabled(can_continue)
        manual_flow_controls = self.program is not None and self.shadow_backend is None
        self.apply_flows_button.setEnabled(manual_flow_controls)
        self.flow_mode_box.setEnabled(manual_flow_controls)
        self.flow_value_spin.setEnabled(manual_flow_controls)
        self.wait_for_cooling_check.setEnabled(not self._running)
        self.cooling_spin.setEnabled(
            not self._running and self.wait_for_cooling_check.isChecked()
        )
        self._update_stage_editor_controls()
        self._update_hiden_editor_controls()

    def _set_follow_live(self, enabled: bool) -> None:
        self.temperature_plot.set_follow_live(enabled)
        self.flow_plot.set_follow_live(enabled)
        self.mass_plot.set_follow_live(enabled)

    def _show_stages(self) -> None:
        if self.program is None or not self._stage_working:
            return
        try:
            stages = tuple(
                ProcessStage.from_mapping(data, default_name=path.stem)
                for path, data in self._stage_documents()
            )
        except (KeyError, TypeError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(
                self, "Invalid pending stage settings", str(exc)
            )
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Program details")
        dialog.resize(820, 380)
        layout = QtWidgets.QVBoxLayout(dialog)
        table = QtWidgets.QTableWidget(len(stages), 7)
        table.setHorizontalHeaderLabels(
            ("Stage", "Mode", "Start °C", "End °C", "Rate °C/min", "Duration s", "Auto")
        )
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        for row, stage in enumerate(stages):
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
        if not self._confirm_leave_hiden_editor():
            event.ignore()
            return
        if not self._confirm_stage_changes():
            event.ignore()
            return
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
    monitor_group.add_argument(
        "--shadow-run",
        action="store_true",
        help=(
            "execute a program over live ADAM4118/Brooks reads while calculating "
            "and logging, while masking heater, valve, and flow output commands"
        ),
    )
    parser.add_argument("--builds", type=Path, help="folder containing MassSpec.exe and data")
    parser.add_argument(
        "--monitor-output",
        type=Path,
        help="optional new .csv or .tdms file for read-only monitor samples",
    )
    parser.add_argument(
        "--monitor-duration",
        type=float,
        help="optional monitor duration in seconds before a normal automatic close",
    )
    parser.add_argument(
        "--allow-read-hardware",
        action="store_true",
        help="explicitly permit read-only ADAM4118 and/or Brooks serial queries",
    )
    parser.add_argument(
        "--hiden-acquisition",
        action="store_true",
        help=(
            "in shadow-run mode, configure COM3 and acquire the program's Hiden "
            "single-mass trend scans"
        ),
    )
    parser.add_argument(
        "--allow-hiden-control",
        action="store_true",
        help=(
            "explicitly permit Hiden scan, filament, operating-mode, and stop commands"
        ),
    )
    args = parser.parse_args()
    monitor_requested = args.temperature_monitor or args.hardware_monitor
    hardware_requested = monitor_requested or args.shadow_run
    if hardware_requested and args.builds is None:
        parser.error("a hardware monitor or shadow run requires --builds")
    if hardware_requested and not args.allow_read_hardware:
        parser.error("a hardware monitor or shadow run requires --allow-read-hardware")
    if monitor_requested and args.program is not None:
        parser.error("a hardware monitor cannot be combined with --program")
    if args.shadow_run and args.program is None:
        parser.error("a shadow run requires --program")
    if args.hiden_acquisition and not args.shadow_run:
        parser.error("--hiden-acquisition requires --shadow-run")
    if args.hiden_acquisition and not args.allow_hiden_control:
        parser.error("--hiden-acquisition requires --allow-hiden-control")
    if args.allow_hiden_control and not args.hiden_acquisition:
        parser.error("--allow-hiden-control requires --hiden-acquisition")
    if args.monitor_output is not None and not monitor_requested:
        parser.error("--monitor-output requires --temperature-monitor or --hardware-monitor")
    if args.monitor_duration is not None:
        if not monitor_requested:
            parser.error("--monitor-duration requires a hardware monitor mode")
        if args.monitor_duration <= 0:
            parser.error("--monitor-duration must be positive")

    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    pg.setConfigOptions(antialias=True, background=PANEL_BACKGROUND, foreground=TEXT_COLOR)
    application = QtWidgets.QApplication(sys.argv[:1])
    application.setApplicationName("SpecMass Python")
    application.setStyle("Fusion")
    monitor_backend = None
    shadow_backend = None
    monitor_telemetry = None
    if hardware_requested:
        try:
            if args.shadow_run:
                if args.hiden_acquisition:
                    shadow_backend = _create_hiden_hardware_shadow(
                        args.builds,
                        args.program,
                    )
                else:
                    shadow_backend = HardwareShadowBackend(
                        _create_hardware_monitor(args.builds)
                    )
            elif args.hardware_monitor:
                monitor_backend = _create_hardware_monitor(args.builds)
            else:
                monitor_backend = _create_adam4118_monitor(args.builds)
            if args.monitor_output is not None:
                monitor_telemetry = _create_monitor_telemetry(
                    args.monitor_output,
                    monitor_backend,
                )
        except Exception as exc:
            if shadow_backend is not None:
                shadow_backend.safe_shutdown()
            elif monitor_backend is not None:
                monitor_backend.safe_shutdown()
            QtWidgets.QMessageBox.critical(
                None, "Cannot configure hardware read mode", str(exc)
            )
            return 2
    window = SpecMassWindow(
        initial_program=args.program,
        monitor_backend=monitor_backend,
        shadow_backend=shadow_backend,
        monitor_telemetry=monitor_telemetry,
        monitor_output=args.monitor_output,
        builds_directory=args.builds,
    )
    window.show()
    if args.monitor_duration is not None:
        QtCore.QTimer.singleShot(max(1, int(args.monitor_duration * 1000)), window.close)
    return int(application.exec_())


if __name__ == "__main__":
    raise SystemExit(main())
