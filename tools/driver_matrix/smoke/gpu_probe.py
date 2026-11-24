#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Goo Engine Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""In-application GPU smoke probe.

This script is executed *inside* Goo Engine / Blender via ``--python``. It is
the payload of the ``smoke-*`` test sets in ``matrix.json``: it asks the
application which GPU backend is actually active, renders a tiny Workbench
frame, and writes a machine readable JSON result.

It intentionally depends only on modules shipped with Goo Engine
(``bpy``, ``gpu``) plus the Python standard library, so it can run on a stock
self-hosted runner without installing anything.

Example::

    goo --background --factory-startup --gpu-backend opengl \
        --python tools/driver_matrix/smoke/gpu_probe.py -- \
        --expect-backend opengl --json-out probe-win11-intel.json

Exit code 0 means the requested backend is active and a render succeeded;
any other outcome exits non-zero (the runner also checks the JSON file).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import sys
import traceback
from pathlib import Path

SCHEMA_VERSION = "1.0"
BACKEND_ALIASES = {
    "opengl": "opengl",
    "open_gl": "opengl",
    "gl": "opengl",
    "vulkan": "vulkan",
    "vk": "vulkan",
    "metal": "metal",
}


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_backend(value) -> str | None:
    if not value:
        return None
    text = str(value).strip().lower().replace(" ", "").replace("-", "_")
    return BACKEND_ALIASES.get(text, text)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Goo Engine GPU smoke probe (runs inside the application).")
    parser.add_argument("--expect-backend", required=True, choices=["opengl", "vulkan", "metal"])
    parser.add_argument("--json-out", required=True, help="Path for the probe JSON document.")
    parser.add_argument("--render-size", type=int, default=64, help="Square render size in pixels.")
    parser.add_argument("--skip-render", action="store_true", help="Only query GPU/backend information.")
    args, _unknown = parser.parse_known_args(argv)
    return args


def safe(callable_obj, default=None):
    try:
        return callable_obj()
    except Exception:
        return default


def collect_gpu_info() -> dict:
    info = {
        "vendor": None,
        "renderer": None,
        "version": None,
        "device_type": None,
        "backend_type": None,
        "shader_platform": None,
        "source": "gpu.platform",
    }
    try:
        import gpu  # type: ignore  # noqa: F401  (provided by the application)
    except Exception as ex:
        info["source"] = "unavailable: %s" % ex
        return info
    platform_mod = getattr(gpu, "platform", None)
    if platform_mod is None:
        info["source"] = "gpu.platform missing"
        return info
    info["vendor"] = safe(platform_mod.vendor_get)
    info["renderer"] = safe(platform_mod.renderer_get)
    info["version"] = safe(platform_mod.version_get)
    info["device_type"] = safe(getattr(platform_mod, "device_type_get", lambda: None))
    info["backend_type"] = safe(getattr(platform_mod, "backend_type_get", lambda: None))
    info["shader_platform"] = safe(getattr(platform_mod, "shader_platform_get", lambda: None))
    return info


def collect_app_info() -> dict:
    info = {"version": None, "version_string": None, "build_hash": None, "build_platform": None, "binary_path": None}
    try:
        import bpy  # type: ignore  # noqa: F401  (provided by the application)
    except Exception as ex:
        info["error"] = str(ex)
        return info
    app = getattr(bpy, "app", None)
    if app is not None:
        info["version"] = list(safe(lambda: tuple(app.version), default=()) or ())
        info["version_string"] = safe(lambda: app.version_string)
        info["build_hash"] = safe(lambda: app.build_hash)
        info["build_platform"] = safe(lambda: app.build_platform)
        info["binary_path"] = safe(lambda: app.binary_path)
    return info


def collect_preferences_backend():
    try:
        import bpy  # type: ignore  # noqa: F401

        prefs = getattr(bpy.context, "preferences", None)
        system = getattr(prefs, "system", None) if prefs is not None else None
        return safe(lambda: system.gpu_backend)
    except Exception:
        return None


def run_render(size: int, out_dir: Path, entry_id: str) -> dict:
    result = {"attempted": False, "ok": False, "engine": None, "output": None, "error": None}
    try:
        import bpy  # type: ignore  # noqa: F401
    except Exception as ex:
        result["error"] = "bpy unavailable: %s" % ex
        return result

    result["attempted"] = True
    engines = ["BLENDER_WORKBENCH", "BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"]
    errors = []
    for engine in engines:
        try:
            bpy.ops.wm.read_factory_settings(use_empty=True)
            scene = bpy.context.scene
            scene.render.engine = engine
            bpy.ops.mesh.primitive_uv_sphere_add(location=(0, 0, 0))
            bpy.ops.object.camera_add(location=(4, -4, 3), rotation=(1.1, 0.0, 0.785))
            scene.camera = bpy.context.object
            bpy.ops.object.light_add(type="SUN", location=(2, -2, 5))
            scene.render.resolution_x = size
            scene.render.resolution_y = size
            scene.render.resolution_percentage = 100
            scene.render.image_settings.file_format = "PNG"
            out_dir.mkdir(parents=True, exist_ok=True)
            output = out_dir / ("probe-render-%s-%s.png" % (entry_id or "entry", engine.lower()))
            scene.render.filepath = str(output)
            bpy.ops.render.render(write_still=True)
            if output.is_file() and output.stat().st_size > 0:
                result.update({"ok": True, "engine": engine, "output": str(output), "error": None})
                return result
            errors.append("%s: render produced no file" % engine)
        except Exception as ex:
            errors.append("%s: %s" % (engine, ex))
    result["error"] = "; ".join(errors) if errors else "no render engine available"
    return result


def json_default(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return repr(value)


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
        # Blender keeps its own options in sys.argv; script arguments follow `--`.
        if "--" in argv:
            argv = argv[argv.index("--") + 1:]
    args = parse_args(argv)
    out_path = Path(args.json_out).expanduser()
    probe = {
        "schema_version": SCHEMA_VERSION,
        "tool": "goo_engine_gpu_probe",
        "generated_at": utc_now(),
        "host": {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version,
        },
        "app": collect_app_info(),
        "expected_backend": args.expect_backend,
        "detected_backend": None,
        "backend_match": False,
        "render": {"attempted": False},
        "errors": [],
        "warnings": [],
    }

    # The GPU module is not initialized until the application has drawn or
    # rendered something. Probe the backend *after* the render so that
    # --gpu-backend fallbacks are visible instead of the stored preference.
    if args.skip_render:
        probe["render"] = {"attempted": False, "ok": None, "skipped": True}
    else:
        try:
            probe["render"] = run_render(args.render_size, out_path.parent / "renders", args.expect_backend)
        except Exception:
            probe["render"] = {"attempted": True, "ok": False, "error": traceback.format_exc()}
        if not probe["render"].get("ok"):
            probe["errors"].append("render failed: %s" % (probe["render"].get("error") or "unknown error"))

    probe["gpu"] = collect_gpu_info()
    probe["preferences_gpu_backend"] = collect_preferences_backend()
    detected = normalize_backend(probe["gpu"].get("backend_type")) if probe["gpu"] else None
    if not detected:
        detected = normalize_backend(probe.get("preferences_gpu_backend"))
    probe["detected_backend"] = detected
    probe["backend_match"] = detected == args.expect_backend
    if not detected:
        probe["warnings"].append("application did not report an active GPU backend")
    elif not probe["backend_match"]:
        probe["errors"].append("active backend %r does not match expected %r" % (detected, args.expect_backend))

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(probe, fh, indent=2, ensure_ascii=False, default=json_default)
            fh.write("\n")
        os.replace(tmp_path, out_path)
        probe["json_out"] = str(out_path)
    except OSError as ex:
        probe["errors"].append("cannot write %s: %s" % (out_path, ex))

    print("gpu-probe: expected=%s detected=%s match=%s render=%s" % (
        args.expect_backend,
        detected or "unknown",
        probe["backend_match"],
        probe["render"].get("ok") if probe["render"].get("attempted") else "skipped",
    ))
    print("gpu-probe: vendor=%s renderer=%s version=%s" % (
        probe["gpu"].get("vendor"),
        probe["gpu"].get("renderer"),
        probe["gpu"].get("version"),
    ))
    for error in probe["errors"]:
        print("gpu-probe: error: %s" % error, file=sys.stderr)
    for warning in probe["warnings"]:
        print("gpu-probe: warning: %s" % warning, file=sys.stderr)
    if probe["errors"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
