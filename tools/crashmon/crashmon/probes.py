"""System, GPU, and Blender probes. All functions return data; none raise.

Probes are best-effort and deliberately shallow: enough context to triage a
crash (OS build, CPU count, memory, GPU driver) without collecting a full
hardware inventory. GPU model/driver is device-level, not user-level data.
"""

from __future__ import annotations

import json
import os
import platform
import re
from pathlib import Path
from typing import Any, Optional

from .util import (
    python_info,
    read_text_limited,
    run_command,
    which,
)

_BLENDER_VERSION_RE = re.compile(r"Blender\s+(v?[0-9][0-9A-Za-z.\-+]*)")
_BUILD_HASH_RE = re.compile(r"build hash:\s*([0-9a-fA-F]+)")
_BUILD_DATE_RE = re.compile(r"build date:\s*(.+)")
_BUILD_PLATFORM_RE = re.compile(r"build platform:\s*(.+)")


def _parse_json_blob(text: str) -> Any:
    """Parse JSON that may be preceded by warning lines (PowerShell/WER noise)."""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    for index, char in enumerate(stripped):
        if char in "[{":
            try:
                return json.loads(stripped[index:])
            except json.JSONDecodeError:
                continue
    return None


def probe_system() -> dict[str, Any]:
    errors: list[str] = []
    uname = platform.uname()
    os_info: dict[str, Any] = {
        "name": uname.system,
        "release": uname.release,
        "version": uname.version,
        "platform": platform.platform(),
    }
    if uname.system == "Windows":
        try:
            release, version, service_pack, _ptype = platform.win32_ver()
            os_info.update({"windows_release": release, "windows_version": version, "service_pack": service_pack})
        except Exception as exc:  # pragma: no cover - Windows specific
            errors.append("win32_ver: {}".format(exc))
    elif uname.system == "Darwin":
        try:
            version, _dev, machine = platform.mac_ver()
            os_info.update({"mac_version": version, "mac_machine": machine})
        except Exception as exc:  # pragma: no cover - macOS specific
            errors.append("mac_ver: {}".format(exc))

    cpu_model, cpu_error = probe_cpu_model(uname.system)
    if cpu_error:
        errors.append(cpu_error)
    memory_bytes, memory_source, memory_error = probe_memory_bytes(uname.system)
    if memory_error:
        errors.append(memory_error)

    return {
        "os": os_info,
        "arch": platform.machine() or os.environ.get("PROCESSOR_ARCHITECTURE", "unknown"),
        "node_arch": uname.machine,
        "cpu": {
            "logical_cores": os.cpu_count(),
            "model": cpu_model or "unknown",
        },
        "memory": {
            "total_bytes": memory_bytes,
            "source": memory_source,
        },
        "python": python_info(),
        "errors": errors,
    }


def probe_cpu_model(system: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    system = system or platform.system()
    try:
        if system == "Windows":
            try:
                import winreg  # noqa: PLC0415 - Windows only

                key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                )
                try:
                    value, _kind = winreg.QueryValueEx(key, "ProcessorNameString")
                    return str(value).strip(), None
                finally:
                    winreg.CloseKey(key)
            except OSError as exc:
                return (
                    os.environ.get("PROCESSOR_IDENTIFIER", "").strip() or None,
                    "cpu model registry probe failed: {}".format(exc),
                )
        if system == "Linux":
            text = read_text_limited("/proc/cpuinfo", limit=65536)
            for line in text.splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip(), None
            return None, "cpu model not found in /proc/cpuinfo"
        if system == "Darwin":
            result = run_command(["sysctl", "-n", "machdep.cpu.brand_string"], timeout=5)
            if result["returncode"] == 0 and result["stdout"].strip():
                return result["stdout"].strip(), None
            return None, "sysctl cpu brand probe failed: {}".format(result["error"] or result["stderr"].strip())
        return platform.processor() or None, None
    except Exception as exc:  # pragma: no cover - defensive
        return None, "cpu probe: {}".format(exc)


def probe_memory_bytes(system: Optional[str] = None) -> tuple[Optional[int], str, Optional[str]]:
    system = system or platform.system()
    try:
        if system == "Windows":
            import ctypes
            from ctypes import wintypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys), "GlobalMemoryStatusEx", None
            return None, "GlobalMemoryStatusEx", "GlobalMemoryStatusEx returned false"
        if system == "Linux":
            text = read_text_limited("/proc/meminfo", limit=65536)
            match = re.search(r"^MemTotal:\s+(\d+)\s+kB", text, re.MULTILINE)
            if match:
                return int(match.group(1)) * 1024, "/proc/meminfo", None
            return None, "/proc/meminfo", "MemTotal not found"
        if system == "Darwin":
            result = run_command(["sysctl", "-n", "hw.memsize"], timeout=5)
            if result["returncode"] == 0:
                try:
                    return int(result["stdout"].strip()), "sysctl hw.memsize", None
                except ValueError:
                    return None, "sysctl hw.memsize", "non-integer output"
            return None, "sysctl hw.memsize", result["error"] or result["stderr"].strip()
        return None, "unsupported", "memory probe unsupported on {}".format(system)
    except Exception as exc:  # pragma: no cover - defensive
        return None, "error", "memory probe: {}".format(exc)


def probe_gpu(system: Optional[str] = None) -> dict[str, Any]:
    system = system or platform.system()
    result: dict[str, Any] = {
        "probe_system": system,
        "method": None,
        "adapters": [],
        "opengl": {},
        "drm_devices": [],
        "notes": [],
        "errors": [],
    }
    try:
        if system == "Windows":
            _probe_gpu_windows(result)
        elif system == "Linux":
            _probe_gpu_linux(result)
        elif system == "Darwin":
            _probe_gpu_macos(result)
        else:
            result["notes"].append("GPU probe not implemented for {}".format(system))
    except Exception as exc:  # pragma: no cover - defensive
        result["errors"].append("gpu probe: {}".format(exc))
    return result


def _probe_gpu_windows(result: dict[str, Any]) -> None:
    script = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,DriverVersion,AdapterRAM,VideoProcessor | "
        "ConvertTo-Json -Compress"
    )
    last_error = None
    for executable in ("powershell.exe", "pwsh.exe"):
        path = which(executable)
        if not path:
            continue
        command = run_command(
            [path, "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=25,
        )
        if command["returncode"] == 0:
            payload = _parse_json_blob(command["stdout"])
            if isinstance(payload, dict):
                payload = [payload]
            if isinstance(payload, list):
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("Name") or item.get("VideoProcessor") or "unknown GPU").strip()
                    driver = str(item.get("DriverVersion") or "").strip() or None
                    ram = item.get("AdapterRAM")
                    try:
                        ram_value = int(ram) if ram is not None else None
                    except (TypeError, ValueError):
                        ram_value = None
                    result["adapters"].append(
                        {
                            "name": name,
                            "driver_version": driver,
                            "adapter_ram_bytes": ram_value,
                            "source": "Win32_VideoController",
                        }
                    )
                if result["adapters"]:
                    result["method"] = "powershell-cim"
                    return
            last_error = "no adapters parsed from PowerShell output"
        else:
            last_error = command["error"] or command["stderr"].strip() or "exit {}".format(command["returncode"])
    # Fallback for older Windows images that still have wmic.
    wmic = which("wmic")
    if wmic:
        command = run_command(
            [wmic, "path", "win32_VideoController", "get", "Name,DriverVersion,AdapterRAM", "/format:list"],
            timeout=20,
        )
        if command["returncode"] == 0:
            current: dict[str, Any] = {}
            for line in command["stdout"].splitlines():
                line = line.strip()
                if not line:
                    if current.get("Name"):
                        result["adapters"].append(
                            {
                                "name": current.get("Name"),
                                "driver_version": current.get("DriverVersion"),
                                "adapter_ram_bytes": _int_or_none(current.get("AdapterRAM")),
                                "source": "wmic",
                            }
                        )
                    current = {}
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    current[key.strip()] = value.strip()
            if current.get("Name"):
                result["adapters"].append(
                    {
                        "name": current.get("Name"),
                        "driver_version": current.get("DriverVersion"),
                        "adapter_ram_bytes": _int_or_none(current.get("AdapterRAM")),
                        "source": "wmic",
                    }
                )
            if result["adapters"]:
                result["method"] = "wmic"
                return
    if last_error:
        result["errors"].append("Windows GPU probe failed: {}".format(last_error))
    result["notes"].append("GPU probe uses read-only CIM/WMI queries; it does not load a graphics driver.")


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _probe_gpu_linux(result: dict[str, Any]) -> None:
    lspci = which("lspci")
    if lspci:
        command = run_command([lspci, "-nn"], timeout=10)
        if command["returncode"] == 0:
            for line in command["stdout"].splitlines():
                if not any(token in line for token in ("VGA compatible controller", "3D controller", "Display controller")):
                    continue
                ids = re.search(r"\[([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\]", line)
                name = line.split(": ", 1)[1] if ": " in line else line
                name = re.sub(r"\s*\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]\s*$", "", name).strip()
                result["adapters"].append(
                    {
                        "name": name or "unknown GPU",
                        "vendor_id": ids.group(1).lower() if ids else None,
                        "device_id": ids.group(2).lower() if ids else None,
                        "source": "lspci",
                    }
                )
            if result["adapters"]:
                result["method"] = "lspci"
        else:
            result["errors"].append("lspci failed: {}".format(command["error"] or command["stderr"].strip()))

    nvidia_smi = which("nvidia-smi")
    if nvidia_smi:
        command = run_command(
            [
                nvidia_smi,
                "--query-gpu=name,driver_version,memory.total",
                "--format=csv,noheader",
            ],
            timeout=10,
        )
        if command["returncode"] == 0:
            for line in command["stdout"].splitlines():
                parts = [part.strip() for part in line.split(",")]
                if parts and parts[0]:
                    result["adapters"].append(
                        {
                            "name": parts[0],
                            "driver_version": parts[1] if len(parts) > 1 else None,
                            "memory": parts[2] if len(parts) > 2 else None,
                            "source": "nvidia-smi",
                        }
                    )
            result["method"] = result["method"] or "nvidia-smi"
        else:
            result["errors"].append("nvidia-smi failed: {}".format(command["error"] or command["stderr"].strip()))

    proc_version = Path("/proc/driver/nvidia/version")
    if proc_version.exists():
        text = read_text_limited(proc_version, limit=4096).strip().splitlines()
        result["nvidia_kernel_module"] = text[0].strip() if text else None

    drm_root = Path("/sys/class/drm")
    if drm_root.is_dir():
        try:
            for card in sorted(drm_root.glob("card[0-9]*")):
                device = card / "device"
                if not device.is_dir():
                    continue
                vendor = read_text_limited(device / "vendor", limit=32).strip()
                device_id = read_text_limited(device / "device", limit=32).strip()
                if vendor or device_id:
                    result["drm_devices"].append(
                        {"card": card.name, "vendor_id": vendor or None, "device_id": device_id or None}
                    )
        except OSError as exc:
            result["errors"].append("drm sysfs: {}".format(exc))

    glxinfo = which("glxinfo")
    if glxinfo and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        command = run_command([glxinfo, "-B"], timeout=10)
        if command["returncode"] == 0:
            for line in command["stdout"].splitlines():
                for prefix, key in (
                    ("OpenGL renderer string:", "renderer"),
                    ("OpenGL version string:", "version"),
                    ("OpenGL core profile version string:", "core_version"),
                ):
                    if line.strip().startswith(prefix):
                        result["opengl"][key] = line.split(":", 1)[1].strip()
    if not result["adapters"] and result["drm_devices"]:
        result["notes"].append("PCI listing unavailable; adapter details limited to sysfs vendor/device IDs.")
    result["notes"].append("Linux GPU probe never installs tools or loads drivers.")


def _probe_gpu_macos(result: dict[str, Any]) -> None:
    profiler = which("system_profiler") or "/usr/sbin/system_profiler"
    if not Path(profiler).exists() and not which("system_profiler"):
        result["errors"].append("system_profiler not found")
        return
    command = run_command([profiler, "SPDisplaysDataType", "-json"], timeout=30)
    if command["returncode"] == 0:
        payload = _parse_json_blob(command["stdout"])
        displays = payload.get("SPDisplaysDataType") if isinstance(payload, dict) else None
        if isinstance(displays, list):
            for item in displays:
                if not isinstance(item, dict):
                    continue
                name = (
                    item.get("sppci_model")
                    or item.get("_name")
                    or item.get("spdisplays_chipset_model")
                    or "unknown GPU"
                )
                result["adapters"].append(
                    {
                        "name": str(name),
                        "vendor": _first_text(item, ("spdisplays_vendor", "sppci_vendor")),
                        "vram": _first_text(item, ("spdisplays_vram", "sppci_vram")),
                        "metal": _first_text(item, ("spdisplays_metal", "sppci_metal")),
                        "source": "system_profiler-json",
                    }
                )
            if result["adapters"]:
                result["method"] = "system_profiler-json"
    if not result["adapters"]:
        # Text fallback for older systems.
        command = run_command([profiler, "SPDisplaysDataType"], timeout=30)
        if command["returncode"] == 0:
            current: dict[str, Any] = {}
            for line in command["stdout"].splitlines():
                stripped = line.strip()
                if stripped.startswith("Chipset Model:"):
                    current["name"] = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("Vendor:"):
                    current["vendor"] = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("VRAM"):
                    current["vram"] = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("Metal") and ":" in stripped:
                    current["metal"] = stripped.split(":", 1)[1].strip()
                elif not stripped and current.get("name"):
                    result["adapters"].append({"source": "system_profiler-text", **current})
                    current = {}
            if current.get("name"):
                result["adapters"].append({"source": "system_profiler-text", **current})
            if result["adapters"]:
                result["method"] = "system_profiler-text"
    if not result["adapters"]:
        result["errors"].append("no display adapters parsed from system_profiler")
    result["notes"].append("macOS GPU probe is read-only and may be slow on first run.")


def _first_text(item: dict[str, Any], keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return None


def probe_blender(blender_path: Optional[os.PathLike[str] | str] = None, timeout: float = 30.0) -> dict[str, Any]:
    """Probe a Blender executable with ``--version`` (bounded, read-only)."""
    info: dict[str, Any] = {
        "path": str(blender_path) if blender_path else None,
        "resolved_path": None,
        "exists": False,
        "executable": False,
        "version": None,
        "version_line": None,
        "build_hash": None,
        "build_date": None,
        "build_platform": None,
        "returncode": None,
        "error": None,
    }
    if not blender_path:
        info["error"] = "no Blender path supplied"
        return info
    candidate = Path(str(blender_path)).expanduser()
    resolved: Optional[Path] = None
    if candidate.is_file():
        resolved = candidate
    elif not candidate.is_absolute():
        found = which(str(blender_path))
        if found:
            resolved = Path(found)
    info["exists"] = resolved is not None
    if resolved is None:
        info["error"] = "Blender executable not found: {}".format(blender_path)
        return info
    info["resolved_path"] = str(resolved.resolve())
    info["executable"] = os.access(str(resolved), os.X_OK)
    command = run_command([str(resolved), "--version"], timeout=timeout)
    info["returncode"] = command["returncode"]
    if command["returncode"] != 0:
        info["error"] = command["error"] or command["stderr"].strip() or "blender --version exit {}".format(
            command["returncode"]
        )
        return info
    lines = [line.strip() for line in command["stdout"].splitlines() if line.strip()]
    text = "\n".join(lines)
    if lines:
        info["version_line"] = lines[0]
    match = _BLENDER_VERSION_RE.search(text)
    if match:
        info["version"] = match.group(1)
    hash_match = _BUILD_HASH_RE.search(text)
    if hash_match:
        info["build_hash"] = hash_match.group(1).lower()
    date_match = _BUILD_DATE_RE.search(text)
    if date_match:
        info["build_date"] = date_match.group(1).strip()
    platform_match = _BUILD_PLATFORM_RE.search(text)
    if platform_match:
        info["build_platform"] = platform_match.group(1).strip()
    if not info["version"]:
        info["error"] = "could not parse Blender version from --version output"
    return info


def probe_environment_allowlist(keys: tuple[str, ...]) -> dict[str, Any]:
    """Collect only explicit allowlisted environment values (never the full env)."""
    values: dict[str, str] = {}
    for key in keys:
        if key in os.environ:
            values[key] = os.environ[key]
    return {
        "scope": "allowlist",
        "considered_keys": list(keys),
        "values": values,
        "present_count": len(values),
        "excluded_key_count": max(0, len(os.environ) - len(values)),
        "note": "Only the allowlisted keys above are stored; the full environment is never captured.",
    }
