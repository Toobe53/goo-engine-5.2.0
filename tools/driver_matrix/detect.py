#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Goo Engine Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Best-effort host capability detection for the Goo Engine driver matrix.

Writes a JSON document describing the OS, architecture, GPUs, driver versions
and the OpenGL / Vulkan / Metal backends that appear to be available.

The detector is intentionally non-fatal: every probe is wrapped, missing tools
are reported as warnings, and the process exits 0 even when information is
incomplete. Use ``--strict`` to make "no usable GPU" exit with code 2.

Usage::

    python tools/driver_matrix/detect.py
    python tools/driver_matrix/detect.py --compact
    python tools/driver_matrix/detect.py --out host.json
"""

from __future__ import annotations

import argparse
import ctypes.util
import datetime
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys

SCHEMA_VERSION = "1.0"
TOOL_NAME = "tools/driver_matrix/detect.py"

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

VENDOR_IDS = {
    "0x10de": "nvidia",
    "0x1002": "amd",
    "0x1022": "amd",
    "0x8086": "intel",
    "0x106b": "apple",
    "0x13b5": "arm",
    "0x5143": "qualcomm",
    "0x1414": "microsoft",
}

VIRTUAL_HINTS = (
    "virtual",
    "vmware",
    "virtualbox",
    "vbox",
    "hyper-v",
    "parallels",
    "citrix",
    "remote display",
    "remote desktop",
    "rdp",
    "iddriver",
    "usbmmidd",
    "parsec",
    "spacedesk",
    "oray",
    "todesk",
    "sunshine",
    "displaylink",
    "meta display",
)

SOFTWARE_HINTS = (
    "microsoft basic",
    "basic render",
    "basic display",
    "llvmpipe",
    "softpipe",
    "swiftshader",
    "swrast",
    "lavapipe",
    "software rasterizer",
    "gdi generic",
)

CI_ENV_VARS = (
    ("GITHUB_ACTIONS", "github-actions"),
    ("GITLAB_CI", "gitlab"),
    ("CIRCLECI", "circleci"),
    ("JENKINS_URL", "jenkins"),
    ("BUILDKITE", "buildkite"),
    ("TF_BUILD", "azure-pipelines"),
    ("APPVEYOR", "appveyor"),
    ("TEAMCITY_VERSION", "teamcity"),
)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime | None = None) -> str:
    dt = dt or _utcnow()
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(cmd, timeout: float = 12.0):
    """Run a command, never raise. Returns (ok, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "", "command not found: %s" % cmd[0]
    except subprocess.TimeoutExpired:
        return False, "", "timeout after %.0fs: %s" % (timeout, " ".join(cmd))
    except OSError as ex:
        return False, "", "failed to run %s: %s" % (" ".join(cmd), ex)
    return proc.returncode == 0, proc.stdout or "", proc.stderr or ""


def _read_text(path: str | os.PathLike) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _norm_arch(machine: str) -> str:
    machine = (machine or "").strip().lower()
    mapping = {
        "amd64": "x64",
        "x86_64": "x64",
        "x64": "x64",
        "i386": "x86",
        "i686": "x86",
        "x86": "x86",
        "arm64": "arm64",
        "aarch64": "arm64",
        "armv8l": "arm64",
    }
    return mapping.get(machine, machine or "unknown")


def _version_tuple(value):
    """Extract a comparable tuple from a version-ish string, else None."""
    if not value:
        return None
    match = re.search(r"\d+(?:\.\d+)*", str(value))
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split(".") if part.isdigit())


def _version_ge(host: str | None, minimum: str | None) -> str:
    """Return 'met', 'below' or 'unknown' for two version strings."""
    host_t = _version_tuple(host)
    min_t = _version_tuple(minimum)
    if host_t is None or min_t is None:
        return "unknown"
    width = max(len(host_t), len(min_t))
    host_t = host_t + (0,) * (width - len(host_t))
    min_t = min_t + (0,) * (width - len(min_t))
    return "met" if host_t >= min_t else "below"


def _nvidia_windows_branch(raw: str | None) -> str | None:
    """Convert an NVIDIA Windows driver version (31.0.15.3623) to 536.23.

    NVIDIA encodes the marketing branch in the last two components. Anything
    that does not look like a five digit branch is rejected rather than guessed.
    """
    if not raw:
        return None
    parts = [part for part in str(raw).split(".") if part.isdigit()]
    if len(parts) < 2:
        return None
    digits = parts[-2] + parts[-1]
    if len(digits) < 5:
        return None
    last_five = digits[-5:]
    return "%s.%s" % (last_five[:3], last_five[3:])


def _classify_vendor(*values: str) -> str:
    haystack = " ".join(value for value in values if value).lower()
    if "ven_10de" in haystack or "nvidia" in haystack or "geforce" in haystack or "quadro" in haystack:
        return "nvidia"
    if (
        "ven_1002" in haystack
        or "amd" in haystack
        or "radeon" in haystack
        or "ati " in haystack
        or "advanced micro devices" in haystack
    ):
        return "amd"
    if "ven_8086" in haystack or "intel" in haystack:
        return "intel"
    if "ven_106b" in haystack or "apple" in haystack:
        return "apple"
    if "ven_5143" in haystack or "qualcomm" in haystack or "adreno" in haystack:
        return "qualcomm"
    if "ven_1414" in haystack or "microsoft" in haystack:
        return "microsoft"
    if "arm" in haystack or "mali" in haystack:
        return "arm"
    return "unknown"


def _gpu_kind(name: str, vendor: str) -> str:
    lowered = (name or "").lower()
    if any(hint in lowered for hint in SOFTWARE_HINTS):
        return "software"
    if any(hint in lowered for hint in VIRTUAL_HINTS):
        return "virtual"
    if vendor == "apple":
        return "integrated"
    if vendor == "intel":
        if "arc" in lowered:
            return "discrete"
        return "integrated"
    if vendor == "amd":
        if any(token in lowered for token in ("rx ", "radeon pro", "firepro", "instinct")):
            return "discrete"
        if "radeon" in lowered and ("graphics" in lowered or "vega" in lowered):
            return "integrated"
        return "unknown"
    if vendor == "nvidia":
        return "discrete"
    if vendor in ("qualcomm", "arm"):
        return "integrated"
    return "unknown"


def _gpu_record(
    name: str,
    vendor: str,
    *,
    driver_version: str | None = None,
    driver_scheme: str | None = None,
    driver_normalized: str | None = None,
    device_id: str | None = None,
    kind: str | None = None,
    source: str = "",
) -> dict:
    kind = kind or _gpu_kind(name, vendor)
    record = {
        "name": name or "unknown",
        "vendor": vendor,
        "kind": kind,
        "driver_version": driver_version,
        "driver_scheme": driver_scheme,
        "driver_normalized": driver_normalized,
        "device_id": device_id,
        "source": source,
        "is_primary": False,
        "is_virtual": kind == "virtual",
        "is_software": kind == "software",
    }
    return record


def _pick_primary(gpus: list) -> dict | None:
    """Pick a representative physical GPU; prefer discrete, then integrated."""
    if not gpus:
        return None
    rank = {"discrete": 3, "integrated": 2, "unknown": 1, "virtual": 0, "software": 0}

    def score(item):
        return (rank.get(item.get("kind"), 0), -item.get("index", 0))

    ordered = sorted(gpus, key=score, reverse=True)
    primary = ordered[0]
    for gpu in gpus:
        gpu["is_primary"] = gpu is primary
    return primary


# ---------------------------------------------------------------------------
# OS / CI
# ---------------------------------------------------------------------------


def detect_os(warnings: list) -> dict:
    os_info = {
        "family": "unknown",
        "name": platform.system() or "unknown",
        "release": "",
        "version": platform.version() or "",
        "build": "",
        "kernel": platform.release() or "",
        "arch": _norm_arch(platform.machine()),
        "is_wsl": False,
    }
    try:
        if IS_WINDOWS:
            os_info["family"] = "windows"
            version = sys.getwindowsversion()
            build = int(getattr(version, "build", 0) or 0)
            os_info["build"] = str(build)
            os_info["release"] = "11" if build >= 22000 else "10"
            os_info["name"] = "Windows %s" % os_info["release"]
            os_info["version"] = platform.version()
            try:
                edition = platform.win32_edition()
                if edition:
                    os_info["edition"] = str(edition)
            except (AttributeError, OSError):
                pass
            os_info["kernel"] = platform.version()
        elif IS_MACOS:
            os_info["family"] = "macos"
            mac_version = platform.mac_ver()[0] or ""
            os_info["version"] = mac_version
            os_info["release"] = mac_version.split(".")[0] if mac_version else ""
            os_info["name"] = "macOS %s" % (mac_version or "unknown")
            ok, out, _err = _run(["sw_vers", "-buildVersion"], timeout=5)
            if ok and out.strip():
                os_info["build"] = out.strip()
            os_info["kernel"] = platform.release()
        elif IS_LINUX:
            os_info["family"] = "linux"
            pretty = ""
            release_id = ""
            name_id = ""
            for candidate in ("/etc/os-release", "/usr/lib/os-release"):
                text = _read_text(candidate)
                if not text:
                    continue
                for line in text.splitlines():
                    if "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    value = value.strip().strip('"').strip("'")
                    if key == "PRETTY_NAME":
                        pretty = value
                    elif key == "VERSION_ID":
                        release_id = value
                    elif key == "NAME":
                        name_id = value
                break
            else:
                warnings.append("os-release not found; Linux distribution is unknown")
            os_info["name"] = pretty or name_id or platform.platform()
            os_info["release"] = release_id
            os_info["version"] = pretty or platform.version()
            os_info["kernel"] = platform.release()
            release_lower = platform.release().lower()
            os_info["is_wsl"] = "microsoft" in release_lower or bool(os.environ.get("WSL_DISTRO_NAME"))
            if os_info["is_wsl"]:
                warnings.append("running inside WSL; GPU passthrough is not a supported matrix target")
    except Exception as ex:  # noqa: BLE001 - detection must never crash
        warnings.append("OS detection failed: %s" % ex)
    return os_info


def detect_ci() -> dict:
    provider = None
    for env_var, name in CI_ENV_VARS:
        if os.environ.get(env_var):
            provider = name
            break
    return {
        "detected": provider is not None,
        "provider": provider,
        "runner_name": os.environ.get("RUNNER_NAME"),
        "runner_os": os.environ.get("RUNNER_OS"),
        "workflow": os.environ.get("GITHUB_WORKFLOW"),
        "job": os.environ.get("GITHUB_JOB"),
        "github_hosted": bool(os.environ.get("GITHUB_ACTIONS")) and os.environ.get("RUNNER_ENVIRONMENT", "") == "github-hosted",
    }


def detect_display_server(warnings: list) -> str | None:
    try:
        if IS_WINDOWS:
            return "windows"
        if IS_MACOS:
            return "quartz"
        if IS_LINUX:
            session = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
            if session in ("x11", "wayland", "mir"):
                return session
            if os.environ.get("WAYLAND_DISPLAY"):
                return "wayland"
            if os.environ.get("DISPLAY"):
                return "x11"
            return None
    except Exception as ex:  # noqa: BLE001
        warnings.append("display server detection failed: %s" % ex)
    return None


# ---------------------------------------------------------------------------
# GPU probes
# ---------------------------------------------------------------------------


def _windows_video_controllers(warnings: list) -> list:
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-CimInstance -ClassName Win32_VideoController |"
        " Select-Object Name,PNPDeviceID,DriverVersion,AdapterCompatibility,VideoProcessor,Status |"
        " ConvertTo-Json -Compress"
    )
    for exe in ("powershell", "pwsh"):
        if not shutil.which(exe):
            continue
        ok, out, err = _run(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            timeout=25,
        )
        if not ok or not out.strip():
            continue
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            warnings.append("could not parse %s GPU output" % exe)
            continue
        if isinstance(payload, dict):
            payload = [payload]
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        warnings.append("unexpected %s GPU output shape" % exe)
    warnings.append("could not query Win32_VideoController")
    return []


def detect_gpus_windows(warnings: list) -> list:
    gpus = []
    for index, item in enumerate(_windows_video_controllers(warnings)):
        name = str(item.get("Name") or "unknown")
        pnp = str(item.get("PNPDeviceID") or "")
        compatibility = str(item.get("AdapterCompatibility") or "")
        vendor = _classify_vendor(name, pnp, compatibility)
        raw_driver = str(item.get("DriverVersion") or "") or None
        scheme = None
        normalized = raw_driver
        if vendor == "nvidia":
            scheme = "nvidia-windows"
            branch = _nvidia_windows_branch(raw_driver)
            if branch:
                normalized = branch
            else:
                warnings.append("could not derive NVIDIA branch from driver version %r" % raw_driver)
        elif vendor == "amd":
            scheme = "amd-windows-raw"
        elif vendor == "intel":
            scheme = "intel-windows-raw"
        elif vendor not in ("unknown", "microsoft"):
            scheme = "windows-generic"
        gpu = _gpu_record(
            name,
            vendor,
            driver_version=raw_driver,
            driver_scheme=scheme,
            driver_normalized=normalized,
            device_id=pnp or None,
            source="Win32_VideoController",
        )
        gpu["index"] = index
        gpus.append(gpu)
    return gpus


def _linux_lspci_map() -> dict:
    """Map 'vendor:device' to a human readable GPU name from lspci."""
    mapping = {}
    if not shutil.which("lspci"):
        return mapping
    ok, out, _err = _run(["lspci", "-nn"], timeout=10)
    if not ok:
        return mapping
    for line in out.splitlines():
        lowered = line.lower()
        if not any(token in lowered for token in ("vga compatible", "3d controller", "display controller")):
            continue
        match = re.search(r"\[([0-9a-f]{4}):([0-9a-f]{4})\]", line, re.IGNORECASE)
        if not match:
            continue
        key = "%s:%s" % (match.group(1).lower(), match.group(2).lower())
        mapping[key] = line.split(": ", 1)[-1].strip()
    return mapping


def _linux_mesa_version(warnings: list) -> tuple:
    """Return (opengl_version, mesa_version, renderer, source)."""
    glx = shutil.which("glxinfo")
    if glx:
        ok, out, _err = _run([glx, "-B"], timeout=15)
        if ok:
            opengl_version = None
            mesa_version = None
            renderer = None
            for line in out.splitlines():
                stripped = line.strip()
                if stripped.startswith("OpenGL version string:") or stripped.startswith(
                    "OpenGL core profile version string:"
                ):
                    value = stripped.split(":", 1)[1].strip()
                    match = re.match(r"(\d+(?:\.\d+)+)", value)
                    if match and not opengl_version:
                        opengl_version = match.group(1)
                    mesa = re.search(r"Mesa\s+(\d+(?:\.\d+)+)", value)
                    if mesa and not mesa_version:
                        mesa_version = mesa.group(1)
                elif stripped.startswith("OpenGL renderer string:"):
                    renderer = stripped.split(":", 1)[1].strip()
            if opengl_version or mesa_version:
                return opengl_version, mesa_version, renderer, "glxinfo"
    # Fallback: Mesa version from the Vulkan summary if vulkaninfo exists.
    vk = shutil.which("vulkaninfo")
    if vk:
        ok, out, _err = _run([vk, "--summary"], timeout=25)
        if ok:
            mesa = re.search(r"driverInfo\s*=\s*Mesa\s+(\d+(?:\.\d+)+)", out)
            if mesa:
                return None, mesa.group(1), None, "vulkaninfo"
    warnings.append("Mesa/OpenGL version not detected (glxinfo/vulkaninfo unavailable)")
    return None, None, None, None


def _linux_kernel_modules() -> dict:
    modules = {}
    for name in ("nvidia", "amdgpu", "i915", "xe", "nouveau", "radeon"):
        version = _read_text("/sys/module/%s/version" % name).strip()
        if version:
            modules[name] = version
    return modules


def _linux_nvidia_driver() -> tuple:
    version = None
    text = _read_text("/proc/driver/nvidia/version")
    if text:
        match = re.search(r"(\d+\.\d+(?:\.\d+)?)", text)
        if match:
            version = match.group(1)
    if not version and shutil.which("nvidia-smi"):
        ok, out, _err = _run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            timeout=15,
        )
        if ok and out.strip():
            version = out.strip().splitlines()[0].strip()
    return version


def detect_gpus_linux(warnings: list) -> list:
    gpus = []
    lspci_names = _linux_lspci_map()
    mesa_version = _linux_mesa_version(warnings)[1]
    nvidia_version = _linux_nvidia_driver()
    modules = _linux_kernel_modules()
    cards = sorted(glob.glob("/sys/class/drm/card[0-9]*"))
    for index, card in enumerate(cards):
        device_dir = os.path.join(card, "device")
        if not os.path.isdir(device_dir):
            continue
        vendor_id = _read_text(os.path.join(device_dir, "vendor")).strip().lower()
        uevent = _read_text(os.path.join(device_dir, "uevent"))
        pci_id = None
        driver_name = None
        for line in uevent.splitlines():
            if line.startswith("PCI_ID="):
                pci_id = line.split("=", 1)[1].strip().lower()
            elif line.startswith("DRIVER="):
                driver_name = line.split("=", 1)[1].strip()
        vendor = VENDOR_IDS.get(vendor_id, "unknown")
        name = lspci_names.get(pci_id or "", "")
        if not name:
            name = "%s GPU (%s)" % (vendor.upper(), pci_id or os.path.basename(card))
        scheme = None
        normalized = None
        display_version = None
        if vendor == "nvidia":
            scheme = "nvidia-linux"
            normalized = nvidia_version
            display_version = nvidia_version
        elif vendor in ("amd", "intel"):
            scheme = "mesa"
            normalized = mesa_version
            display_version = mesa_version
        gpu = _gpu_record(
            name,
            vendor,
            driver_version=display_version,
            driver_scheme=scheme,
            driver_normalized=normalized,
            device_id=pci_id,
            source="sysfs:drm",
        )
        gpu["index"] = index
        gpu["kernel_driver"] = driver_name
        gpus.append(gpu)
    if not gpus:
        warnings.append("no DRM card devices found (headless container or missing GPU)")
    return gpus


def detect_gpus_macos(warnings: list, os_info: dict) -> list:
    gpus = []
    ok, out, err = _run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=30)
    payload = {}
    if ok and out.strip():
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            warnings.append("could not parse system_profiler display output")
    else:
        warnings.append("system_profiler display query failed: %s" % (err.strip() or "unknown error"))
    entries = payload.get("SPDisplaysDataType") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        entries = []
    os_version = os_info.get("version") or ""
    build = os_info.get("build") or ""
    driver_display = ("macOS %s%s" % (os_version, (" (%s)" % build) if build else "")).strip()
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            continue
        model = str(item.get("sppci_model") or item.get("_name") or "unknown")
        vendor_text = str(item.get("spdisplays_vendor") or "")
        device_id = str(item.get("spdisplays_device-id") or "") or None
        vendor = _classify_vendor(model, vendor_text, device_id or "")
        metal = str(item.get("spdisplays_metal") or "")
        gpu = _gpu_record(
            model,
            vendor,
            driver_version=driver_display,
            driver_scheme="apple-os",
            driver_normalized=os_version,
            device_id=device_id,
            source="system_profiler",
        )
        gpu["index"] = index
        gpu["metal_reported"] = metal or None
        gpus.append(gpu)
    if not gpus:
        # Apple silicon always has an integrated GPU, expose it even if
        # system_profiler returned nothing so the matrix can still be selected.
        chip = ""
        ok, out, _err = _run(["sysctl", "-n", "machdep.cpu.brand_string"], timeout=5)
        if ok:
            chip = out.strip()
        gpus.append(
            _gpu_record(
                chip or "Apple GPU",
                "apple",
                driver_version=driver_display,
                driver_scheme="apple-os",
                driver_normalized=os_version,
                source="fallback",
            )
        )
        gpus[-1]["index"] = 0
        warnings.append("GPU details unavailable; reporting a generic Apple GPU entry")
    return gpus


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


def detect_backends(os_info: dict, gpus: list, warnings: list) -> dict:
    backends = {}
    physical = [gpu for gpu in gpus if gpu["kind"] not in ("virtual", "software")]

    # OpenGL -----------------------------------------------------------------
    opengl = {
        "available": None,
        "version": None,
        "renderer": None,
        "source": None,
        "deprecated": False,
        "notes": [],
    }
    try:
        if IS_WINDOWS:
            dll = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "opengl32.dll")
            opengl["available"] = os.path.exists(dll)
            opengl["source"] = "opengl32.dll"
            opengl["notes"].append("OpenGL version requires a live context; run the smoke probe to confirm 4.3+.")
        elif IS_MACOS:
            opengl["available"] = bool(physical)
            opengl["version"] = "4.1"
            opengl["deprecated"] = True
            opengl["source"] = "OS-bundled (deprecated)"
            opengl["notes"].append("Apple deprecated OpenGL and caps it at 4.1.")
        elif IS_LINUX:
            opengl_version, _mesa, renderer, source = _linux_mesa_version(warnings)
            opengl["version"] = opengl_version
            opengl["renderer"] = renderer
            opengl["source"] = source
            opengl["available"] = bool(opengl_version) or bool(shutil.which("glxinfo"))
            if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
                opengl["notes"].append("No DISPLAY/WAYLAND_DISPLAY; version probing may be incomplete.")
        else:
            opengl["notes"].append("Unsupported platform for OpenGL probing.")
    except Exception as ex:  # noqa: BLE001
        warnings.append("OpenGL detection failed: %s" % ex)
    backends["opengl"] = opengl

    # Vulkan -----------------------------------------------------------------
    vulkan = {
        "available": None,
        "version": None,
        "icd_count": 0,
        "icds": [],
        "loader": None,
        "source": None,
        "notes": [],
    }
    try:
        if IS_WINDOWS:
            dll = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "vulkan-1.dll")
            vulkan["loader"] = dll if os.path.exists(dll) else None
            icds = []
            try:
                import winreg  # noqa: PLC0415 - Windows only

                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Khronos\Vulkan\Drivers") as key:
                    index = 0
                    while True:
                        try:
                            name, _value, _kind = winreg.EnumValue(key, index)
                        except OSError:
                            break
                        icds.append(name)
                        index += 1
            except OSError:
                pass
            env_icd = os.environ.get("VK_ICD_FILENAMES") or os.environ.get("VK_DRIVER_FILES")
            if env_icd:
                icds.extend(part for part in re.split(r"[;:]", env_icd) if part)
            vulkan["icds"] = icds
            vulkan["icd_count"] = len(icds)
            vulkan["available"] = bool(vulkan["loader"]) and (bool(icds) or bool(env_icd))
            vulkan["source"] = "vulkan-1.dll + registry"
            if not vulkan["loader"]:
                vulkan["notes"].append("vulkan-1.dll is missing; install the Vulkan runtime.")
        elif IS_LINUX:
            loader = ctypes.util.find_library("vulkan")
            icds = []
            for icd_dir in ("/usr/share/vulkan/icd.d", "/etc/vulkan/icd.d", "/usr/local/share/vulkan/icd.d"):
                icds.extend(sorted(glob.glob(os.path.join(icd_dir, "*.json"))))
            env_icd = os.environ.get("VK_ICD_FILENAMES") or os.environ.get("VK_DRIVER_FILES")
            if env_icd:
                icds.extend(part for part in re.split(r"[;:]", env_icd) if part)
            vulkan["loader"] = loader
            vulkan["icds"] = icds
            vulkan["icd_count"] = len(icds)
            vulkan["available"] = bool(loader) and bool(icds)
            vulkan["source"] = "libvulkan + ICD manifests"
            if shutil.which("vulkaninfo"):
                ok, out, _err = _run(["vulkaninfo", "--summary"], timeout=25)
                if ok:
                    match = re.search(r"apiVersion\s*=\s*(\d+\.\d+\.\d+)", out)
                    if match:
                        vulkan["version"] = match.group(1)
        elif IS_MACOS:
            candidates = [
                "/usr/local/lib/libvulkan.dylib",
                "/opt/homebrew/lib/libvulkan.dylib",
                os.path.expanduser("~/VulkanSDK"),
            ]
            found = [path for path in candidates if os.path.exists(path)]
            vulkan["available"] = bool(found) or bool(
                os.environ.get("VK_ICD_FILENAMES") or os.environ.get("VK_DRIVER_FILES")
            )
            vulkan["icds"] = found
            vulkan["icd_count"] = len(found)
            vulkan["source"] = "MoltenVK (unsupported)"
            vulkan["notes"].append("MoltenVK is not a supported production backend for Goo 5.2.")
        else:
            vulkan["notes"].append("Unsupported platform for Vulkan probing.")
    except Exception as ex:  # noqa: BLE001
        warnings.append("Vulkan detection failed: %s" % ex)
    backends["vulkan"] = vulkan

    # Metal ------------------------------------------------------------------
    metal = {
        "available": False,
        "version": None,
        "source": None,
        "notes": [],
    }
    try:
        if IS_MACOS:
            major = 0
            try:
                major = int((os_info.get("version") or "0").split(".")[0])
            except ValueError:
                major = 0
            metal["available"] = major >= 13
            metal["version"] = "3" if major >= 13 else ("2" if major >= 10 else None)
            metal["source"] = "OS-bundled"
            if not physical:
                metal["notes"].append("No physical GPU found; Metal is only listed as OS capability.")
            if os_info.get("build", "").lower().startswith(("vm",)) or os.environ.get("CI"):
                metal["notes"].append("Virtualized/CI hosts may not expose a Metal device; confirm with the smoke probe.")
        else:
            metal["notes"].append("Metal is macOS only.")
    except Exception as ex:  # noqa: BLE001
        warnings.append("Metal detection failed: %s" % ex)
    backends["metal"] = metal
    return backends


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


def detect_host() -> dict:
    warnings: list = []
    errors: list = []
    document = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "collected_at": _iso(),
        "host": {
            "hostname": platform.node(),
            "os": {},
            "arch": _norm_arch(platform.machine()),
            "python": platform.python_version(),
            "machine": platform.machine(),
        },
        "ci": {},
        "display_server": None,
        "gpus": [],
        "primary_gpu": None,
        "drivers": {},
        "backends": {},
        "warnings": warnings,
        "errors": errors,
    }
    try:
        os_info = detect_os(warnings)
    except Exception as ex:  # noqa: BLE001
        errors.append("OS detection raised: %s" % ex)
        os_info = {"family": "unknown", "name": "unknown", "release": "", "version": "", "arch": "unknown"}
    document["host"]["os"] = os_info
    document["host"]["arch"] = os_info.get("arch") or document["host"]["arch"]
    document["ci"] = detect_ci()
    document["display_server"] = detect_display_server(warnings)

    try:
        if IS_WINDOWS:
            gpus = detect_gpus_windows(warnings)
        elif IS_MACOS:
            gpus = detect_gpus_macos(warnings, os_info)
        elif IS_LINUX:
            gpus = detect_gpus_linux(warnings)
        else:
            gpus = []
            warnings.append("Unsupported platform: GPU detection skipped")
    except Exception as ex:  # noqa: BLE001
        errors.append("GPU detection raised: %s" % ex)
        gpus = []
    for index, gpu in enumerate(gpus):
        gpu["index"] = index
    virtual = [gpu for gpu in gpus if gpu["kind"] == "virtual"]
    if virtual:
        warnings.append(
            "ignored virtual display adapter(s): %s" % ", ".join(gpu["name"] for gpu in virtual)
        )
    document["gpus"] = gpus
    document["primary_gpu"] = _pick_primary(gpus)

    drivers = {}
    try:
        if IS_WINDOWS:
            primary = document["primary_gpu"]
            if primary and primary.get("driver_scheme"):
                drivers[primary["vendor"]] = {
                    "version": primary.get("driver_normalized") or primary.get("driver_version"),
                    "raw_version": primary.get("driver_version"),
                    "scheme": primary.get("driver_scheme"),
                    "source": "Win32_VideoController",
                }
        elif IS_LINUX:
            modules = _linux_kernel_modules()
            if modules:
                drivers["kernel_modules"] = modules
            nvidia_version = _linux_nvidia_driver()
            if nvidia_version:
                drivers["nvidia"] = {
                    "version": nvidia_version,
                    "scheme": "nvidia-linux",
                    "source": "/proc/driver/nvidia/version",
                }
            opengl_version, mesa_version, renderer, source = _linux_mesa_version(warnings)
            if mesa_version:
                drivers["mesa"] = {
                    "version": mesa_version,
                    "opengl_version": opengl_version,
                    "renderer": renderer,
                    "scheme": "mesa",
                    "source": source,
                }
        elif IS_MACOS:
            drivers["apple"] = {
                "version": os_info.get("version"),
                "build": os_info.get("build"),
                "scheme": "apple-os",
                "source": "sw_vers",
                "note": "Apple bundles GPU drivers with the OS.",
            }
    except Exception as ex:  # noqa: BLE001
        errors.append("driver summary failed: %s" % ex)
    document["drivers"] = drivers
    try:
        document["backends"] = detect_backends(os_info, gpus, warnings)
    except Exception as ex:  # noqa: BLE001
        errors.append("backend detection raised: %s" % ex)
        document["backends"] = {}

    if not gpus:
        warnings.append("no GPU detected; matrix selection will not match any GPU row")
    document["warnings"] = list(dict.fromkeys(warnings))
    return document


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect OS/GPU/driver/backend capabilities for the Goo Engine driver matrix."
    )
    parser.add_argument("--compact", action="store_true", help="Print one-line JSON instead of pretty JSON.")
    parser.add_argument("--out", metavar="PATH", help="Also write the JSON document to PATH.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 2 if no physical GPU was detected (default: always exit 0 on partial data).",
    )
    args = parser.parse_args(argv)

    try:
        document = detect_host()
    except Exception as ex:  # noqa: BLE001 - last-resort guard, never traceback for users
        document = {
            "schema_version": SCHEMA_VERSION,
            "tool": TOOL_NAME,
            "collected_at": _iso(),
            "host": {"os": {"family": platform.system().lower(), "release": "", "arch": _norm_arch(platform.machine())}},
            "gpus": [],
            "primary_gpu": None,
            "drivers": {},
            "backends": {},
            "warnings": [],
            "errors": ["detection failed: %s" % ex],
        }

    text = json.dumps(document, indent=None if args.compact else 2, ensure_ascii=False, sort_keys=False)
    print(text)
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError as ex:
            print("warning: could not write %s: %s" % (args.out, ex), file=sys.stderr)

    if args.strict:
        physical = [
            gpu
            for gpu in document.get("gpus", [])
            if gpu.get("kind") not in ("virtual", "software")
        ]
        if not physical:
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
