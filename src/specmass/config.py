from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .legacy import load_configuration
from .pid import PIDGains


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    pid_gains: PIDGains = PIDGains(kc=20.0, ti_seconds=1.5, td_seconds=0.0)
    pid_period_seconds: float = 0.5
    relay_window_seconds: float = 5.0
    data_directory: Path = Path("data")
    hardware_enabled: bool = False

    @classmethod
    def from_legacy(cls, configuration: Mapping[str, Any]) -> "RuntimeSettings":
        pid = _mapping(configuration.get("PIDTh", {}))
        dev_mgr = _mapping(configuration.get("DevMgrTh", {}))
        gains = _mapping(pid.get("PIDGains", pid.get("PID", {})))
        timeout_ms = float(pid.get("HLTimeout", 500.0))
        ratio = float(pid.get("SignalRatio", 10.0))
        data_dir = dev_mgr.get("DataDir", "data")
        return cls(
            pid_gains=PIDGains(
                kc=float(gains.get("Kc", pid.get("Kc", 20.0))),
                ti_seconds=float(gains.get("Ti", pid.get("Ti", 1.5))),
                td_seconds=float(gains.get("Td", pid.get("Td", 0.0))),
            ),
            pid_period_seconds=timeout_ms / 1000.0,
            relay_window_seconds=max(timeout_ms / 1000.0, ratio * timeout_ms / 1000.0),
            data_directory=Path(str(data_dir)),
            hardware_enabled=False,
        )


def load_runtime_settings(directory: str | Path) -> RuntimeSettings:
    return RuntimeSettings.from_legacy(load_configuration(directory))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}

