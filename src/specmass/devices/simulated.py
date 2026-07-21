from __future__ import annotations

from dataclasses import replace
from math import isfinite

from .base import ControlCommand, SensorSnapshot


class SimulatedBackend:
    """Small deterministic thermal/flow model; it never opens hardware ports."""

    def __init__(
        self,
        *,
        ambient_temperature: float = 20.0,
        flow_channels: int = 4,
        heating_rate_per_second: float = 5.0,
        cooling_coefficient: float = 0.005,
        flow_response_per_second: float = 2.0,
        flow_write_on_change: bool = True,
    ) -> None:
        self.ambient_temperature = float(ambient_temperature)
        self.temperature = float(ambient_temperature)
        self.flows = [0.0] * flow_channels
        self.flow_targets = [0.0] * flow_channels
        # The LabVIEW Brooks driver initializes PrevData to zero and writes
        # only when a requested setpoint differs from that cached value.
        self.flow_command_cache = [0.0] * flow_channels
        self.last_flow_writes = (False,) * flow_channels
        self.valves: tuple[bool, ...] = ()
        self.heating_rate_per_second = float(heating_rate_per_second)
        self.cooling_coefficient = float(cooling_coefficient)
        self.flow_response_per_second = float(flow_response_per_second)
        self.flow_write_on_change = bool(flow_write_on_change)
        self.last_command = ControlCommand.safe(flow_channels)

    def read(self, timestamp: float) -> SensorSnapshot:
        return SensorSnapshot(
            timestamp=timestamp,
            temperature=self.temperature,
            flows=tuple(self.flows),
            masses={},
        )

    def apply(self, command: ControlCommand, dt_seconds: float) -> None:
        heater = min(100.0, max(0.0, command.heater_percent)) / 100.0
        thermal_change = (
            heater * self.heating_rate_per_second
            - self.cooling_coefficient * (self.temperature - self.ambient_temperature)
        ) * dt_seconds
        self.temperature += thermal_change
        response = min(1.0, max(0.0, self.flow_response_per_second * dt_seconds))
        writes = [False] * len(self.flows)
        for index in range(min(len(self.flows), len(command.flow_setpoints))):
            if command.flow_write_enabled and not command.flow_write_enabled[index]:
                continue
            requested = command.flow_setpoints[index]
            if not self.flow_write_on_change or requested != self.flow_command_cache[index]:
                self.flow_command_cache[index] = requested
                self.flow_targets[index] = requested
                writes[index] = True
        for index in range(len(self.flows)):
            self.flows[index] += (self.flow_targets[index] - self.flows[index]) * response
        self.last_flow_writes = tuple(writes)
        self.valves = command.valve_states
        self.last_command = replace(command, heater_percent=heater * 100.0)

    def safe_shutdown(self) -> None:
        write_enabled = self.last_command.flow_write_enabled or (True,) * len(self.flows)
        self.last_command = ControlCommand.safe(
            len(self.flows),
            len(self.valves),
            flow_write_enabled=write_enabled,
        )
        self.valves = self.last_command.valve_states

    def set_external_flow(self, channel: int, value: float) -> None:
        self.set_front_panel_flow(channel, value)

    def set_front_panel_flow(self, channel: int, value: float) -> None:
        """Simulate a local device change without altering the app-side cache."""
        if channel < 0 or channel >= len(self.flows):
            raise ValueError(f"Flow channel {channel} is outside the simulated backend")
        value = float(value)
        if not isfinite(value) or value < 0:
            raise ValueError("Front-panel flow must be finite and non-negative")
        self.flows[channel] = value
        self.flow_targets[channel] = value
