from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from math import isfinite

from .devices.base import ControlCommand, SensorSnapshot
from .models import ProcessProgram, ProcessStage, TemperatureMode


class MSMState(IntEnum):
    UNINITIALIZED = 0
    IDLE = 1
    PRE_START = 2
    START = 3
    RUNNING_PROCESS = 4
    START_STAGE = 5
    START_TEMP_STABILIZATION = 6
    RUNNING_STAGE = 7
    END_TEMP_STABILIZATION = 8
    END_STAGE = 9
    PRE_STOP = 10
    STOP = 11
    ERROR = 12
    TERMINATING = 13
    READY_FOR_START = 14


@dataclass(frozen=True, slots=True)
class ControllerStatus:
    state: MSMState
    stage_index: int | None
    process_elapsed_seconds: float
    stage_elapsed_seconds: float
    waiting_for_confirmation: bool
    completed: bool


class ProcessController:
    """Deterministic translation of the legacy MSM thread's process behavior."""

    def __init__(
        self,
        *,
        temperature_tolerance: float = 1.0,
        stability_seconds: float = 3.0,
        cooling_temperature: float | None = None,
    ) -> None:
        if temperature_tolerance < 0 or stability_seconds < 0:
            raise ValueError("Temperature tolerance and stability time cannot be negative")
        self.temperature_tolerance = float(temperature_tolerance)
        self.stability_seconds = float(stability_seconds)
        self.cooling_temperature = cooling_temperature
        self.program: ProcessProgram | None = None
        self.state = MSMState.IDLE
        self.stage_index: int | None = None
        self._process_started_at: float | None = None
        self._stage_started_at: float | None = None
        self._stable_since: float | None = None
        self._force_continue = False
        self._waiting_for_confirmation = False
        self._completed = False
        self.fault: str | None = None
        self._manual_flow_overrides: dict[int, float] = {}
        self._external_flow_channels: set[int] = set()
        self._last_command = ControlCommand.safe()

    @property
    def current_stage(self) -> ProcessStage | None:
        if self.program is None or self.stage_index is None:
            return None
        return self.program.stages[self.stage_index]

    def load(self, program: ProcessProgram) -> None:
        if self.state not in (MSMState.IDLE, MSMState.READY_FOR_START, MSMState.STOP):
            raise RuntimeError(f"Cannot load a program while in {self.state.name}")
        self.program = program
        self.state = MSMState.READY_FOR_START
        self.stage_index = None
        self._reset_run_flags()

    def start(self, timestamp: float) -> None:
        if self.program is None or self.state is not MSMState.READY_FOR_START:
            raise RuntimeError("A loaded program in READY_FOR_START is required")
        # A completed run leaves the controller pointing at its final stage.
        # Reset only transient run progress here so the loaded program can be
        # started again without discarding manual/external flow configuration.
        self.stage_index = None
        self._process_started_at = timestamp
        self._stage_started_at = None
        self._stable_since = None
        self._force_continue = False
        self._waiting_for_confirmation = False
        self.fault = None
        self.state = MSMState.PRE_START
        self._completed = False

    def confirm_next_stage(self) -> None:
        if self.state is not MSMState.START or not self._waiting_for_confirmation:
            raise RuntimeError("The controller is not waiting for stage confirmation")
        self._waiting_for_confirmation = False
        self.state = MSMState.START_STAGE

    def force_continue_stage(self) -> None:
        if self.state is not MSMState.RUNNING_STAGE:
            raise RuntimeError("Force-continue is only valid while a stage is running")
        if self._require_stage().temperature_mode is not TemperatureMode.ISOTHERMAL:
            raise RuntimeError("The legacy application only permits force-continue for isothermal stages")
        self._force_continue = True

    @property
    def manual_flow_overrides(self) -> dict[int, float]:
        return dict(self._manual_flow_overrides)

    @property
    def external_flow_channels(self) -> frozenset[int]:
        return frozenset(self._external_flow_channels)

    def set_manual_flow_override(self, channel: int, value: float) -> None:
        channel_count = max(
            (len(stage.start_flows) for stage in self.program.stages),
            default=0,
        ) if self.program else 0
        if channel < 0 or channel >= channel_count:
            raise ValueError(f"Flow channel {channel} is outside the loaded program")
        value = float(value)
        if not isfinite(value) or value < 0:
            raise ValueError("Manual flow setpoint must be a finite non-negative number")
        self._external_flow_channels.discard(channel)
        self._manual_flow_overrides[channel] = value

    def clear_manual_flow_override(self, channel: int) -> None:
        self._manual_flow_overrides.pop(channel, None)

    def clear_all_manual_flow_overrides(self) -> None:
        self._manual_flow_overrides.clear()

    def set_flow_channel_external(self, channel: int, external: bool = True) -> None:
        channel_count = max(
            (len(stage.start_flows) for stage in self.program.stages),
            default=0,
        ) if self.program else 0
        if channel < 0 or channel >= channel_count:
            raise ValueError(f"Flow channel {channel} is outside the loaded program")
        if external:
            self._manual_flow_overrides.pop(channel, None)
            self._external_flow_channels.add(channel)
        else:
            self._external_flow_channels.discard(channel)

    def stop(self) -> None:
        if self.state in (
            MSMState.IDLE,
            MSMState.UNINITIALIZED,
            MSMState.READY_FOR_START,
            MSMState.STOP,
            MSMState.ERROR,
            MSMState.TERMINATING,
        ):
            return
        self.state = MSMState.PRE_STOP
        self._waiting_for_confirmation = False

    def trip(self, reason: str) -> None:
        self.fault = reason
        self.state = MSMState.ERROR
        self._waiting_for_confirmation = False
        self._last_command = self._safe_command()

    def tick(self, snapshot: SensorSnapshot) -> ControlCommand:
        timestamp = snapshot.timestamp
        if self.state is MSMState.PRE_START:
            self.state = MSMState.START_STAGE
        elif self.state is MSMState.START_STAGE:
            self._begin_next_stage(timestamp)
        elif self.state is MSMState.START_TEMP_STABILIZATION:
            self._last_command = self._stage_command(0.0)
            if self._temperature_ready(snapshot.temperature, timestamp, at_start=True):
                self._stage_started_at = timestamp
                self._stable_since = None
                self.state = MSMState.RUNNING_STAGE
        elif self.state is MSMState.RUNNING_STAGE:
            elapsed = self._stage_elapsed(timestamp)
            self._last_command = self._stage_command(elapsed)
            stage = self._require_stage()
            if self._force_continue or elapsed >= stage.effective_duration_seconds():
                self._force_continue = False
                self.state = MSMState.END_TEMP_STABILIZATION
                self._stable_since = None
        elif self.state is MSMState.END_TEMP_STABILIZATION:
            stage = self._require_stage()
            elapsed = stage.effective_duration_seconds()
            self._last_command = self._stage_command(elapsed)
            if not stage.stabilize_temperature or self._temperature_ready(
                snapshot.temperature, timestamp, at_start=False
            ):
                self.state = MSMState.END_STAGE
        elif self.state is MSMState.END_STAGE:
            self._finish_stage()
        elif self.state is MSMState.PRE_STOP:
            self._last_command = self._safe_command()
            if self.cooling_temperature is None or snapshot.temperature <= self.cooling_temperature:
                self.state = MSMState.STOP
        elif self.state is MSMState.STOP:
            self._last_command = self._safe_command()
            self.state = MSMState.READY_FOR_START if self.program else MSMState.IDLE
            self._completed = True
        elif self.state in (MSMState.ERROR, MSMState.TERMINATING, MSMState.IDLE, MSMState.READY_FOR_START):
            self._last_command = self._safe_command()
        return self._last_command

    def status(self, timestamp: float) -> ControllerStatus:
        process_elapsed = 0.0 if self._process_started_at is None else max(0.0, timestamp - self._process_started_at)
        return ControllerStatus(
            state=self.state,
            stage_index=self.stage_index,
            process_elapsed_seconds=process_elapsed,
            stage_elapsed_seconds=self._stage_elapsed(timestamp),
            waiting_for_confirmation=self._waiting_for_confirmation,
            completed=self._completed,
        )

    def _begin_next_stage(self, timestamp: float) -> None:
        if self.program is None:
            self.state = MSMState.ERROR
            return
        next_index = 0 if self.stage_index is None else self.stage_index + 1
        if next_index >= len(self.program.stages):
            self.state = MSMState.PRE_STOP
            return
        self.stage_index = next_index
        self._stage_started_at = None
        self._stable_since = None
        self._last_command = self._stage_command(0.0)
        self.state = MSMState.START_TEMP_STABILIZATION

    def _finish_stage(self) -> None:
        if self.program is None or self.stage_index is None:
            self.state = MSMState.ERROR
            return
        next_index = self.stage_index + 1
        if next_index >= len(self.program.stages):
            self.state = MSMState.PRE_STOP
            self._last_command = self._safe_command()
            return
        if self.program.stages[next_index].auto_start:
            self.state = MSMState.START_STAGE
        else:
            self.state = MSMState.START
            self._waiting_for_confirmation = True

    def _stage_command(self, elapsed_seconds: float) -> ControlCommand:
        stage = self._require_stage()
        flows = list(stage.flow_setpoints(elapsed_seconds))
        for channel, value in self._manual_flow_overrides.items():
            if channel < len(flows):
                flows[channel] = value
        return ControlCommand(
            temperature_setpoint=stage.temperature_setpoint(elapsed_seconds),
            flow_setpoints=tuple(flows),
            valve_states=stage.valve_states_at(elapsed_seconds),
            heater_percent=0.0,
            flow_write_enabled=tuple(
                channel not in self._external_flow_channels for channel in range(len(flows))
            ),
        )

    def _safe_command(self) -> ControlCommand:
        stage = self.current_stage
        flow_count = len(stage.start_flows) if stage else len(self._last_command.flow_setpoints)
        return ControlCommand.safe(
            flow_count,
            len(stage.valve_initial_states) if stage else len(self._last_command.valve_states),
            flow_write_enabled=tuple(
                channel not in self._external_flow_channels for channel in range(flow_count)
            ),
        )

    def _temperature_ready(self, measurement: float, timestamp: float, *, at_start: bool) -> bool:
        stage = self._require_stage()
        target = stage.start_temperature if at_start else stage.end_temperature
        within = abs(measurement - target) <= self.temperature_tolerance
        needs_stability = stage.stabilize_temperature
        if not within:
            self._stable_since = None
            return False
        if not needs_stability or self.stability_seconds == 0:
            return True
        if self._stable_since is None:
            self._stable_since = timestamp
        return timestamp - self._stable_since >= self.stability_seconds

    def _stage_elapsed(self, timestamp: float) -> float:
        return 0.0 if self._stage_started_at is None else max(0.0, timestamp - self._stage_started_at)

    def _require_stage(self) -> ProcessStage:
        stage = self.current_stage
        if stage is None:
            raise RuntimeError("No current process stage")
        return stage

    def _reset_run_flags(self) -> None:
        self._process_started_at = None
        self._stage_started_at = None
        self._stable_since = None
        self._force_continue = False
        self._waiting_for_confirmation = False
        self._completed = False
        self.fault = None
        self._manual_flow_overrides.clear()
        self._external_flow_channels.clear()
        self._last_command = ControlCommand.safe()
