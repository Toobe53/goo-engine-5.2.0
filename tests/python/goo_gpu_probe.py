# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless EEVEE probe used by the Goo CTest gate.

This script runs *inside* Blender.  It reports whether this host can actually
execute a tiny hardware/GPU EEVEE render.  A listed backend (EGL/Metal/Vulkan)
is not treated as proof: ``available`` is only true after a real 8x8 render
produced non-zero pixels.

Usage (from ``goo_test_util.probe_gpu``)::

    blender --background --factory-startup --python-exit-code 1 \
        --python goo_gpu_probe.py -- --output <probe.json>

The script always tries to write the JSON report and then exits.  If Blender
crashes or cannot start a GPU context before Python runs, the host side sees a
missing JSON file and treats the GPU as unavailable (skip, never pass).
"""

import argparse
import json
import sys
import traceback
from pathlib import Path

import bpy


def arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--resolution', type=int, default=8)
    argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else None
    return parser.parse_args(argv)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf8')
    tmp.replace(path)


def platform_info():
    info = {}
    try:
        import gpu

        for key, getter in (
            ('vendor', 'vendor_get'),
            ('renderer', 'renderer_get'),
            ('version', 'version_get'),
            ('backend_type', 'backend_type_get'),
        ):
            try:
                info[key] = getattr(gpu.platform, getter)()
            except Exception as exc:  # noqa: BLE001 - probe must never abort on reporting
                info[key + '_error'] = '{}: {}'.format(type(exc).__name__, exc)
    except Exception as exc:  # noqa: BLE001
        info['import_error'] = '{}: {}'.format(type(exc).__name__, exc)
    return info


def main():
    args = arguments()
    report = {
        'schema': 'goo-gpu-probe/v1',
        'available': False,
        'reason': None,
        'blender_version': bpy.app.version_string,
        'blender_version_file': bpy.app.version[0] * 100 + bpy.app.version[1],
        'background': bpy.app.background,
        'platform': platform_info(),
        'render': {
            'requested_resolution': args.resolution,
            'engine': None,
            'samples': None,
            'image': None,
            'pixel_sum': None,
            'error': None,
        },
    }
    scene = bpy.context.scene
    engine = 'BLENDER_EEVEE'
    try:
        scene.render.engine = engine
    except TypeError:
        engine = 'BLENDER_EEVEE_NEXT'
        try:
            scene.render.engine = engine
        except TypeError:
            engine = None
    report['render']['engine'] = engine

    output_dir = args.output.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    exr = output_dir / 'gpu_probe.exr'

    try:
        if engine is None:
            raise RuntimeError('no EEVEE render engine is available in this Blender build')
        scene.render.resolution_x = args.resolution
        scene.render.resolution_y = args.resolution
        scene.render.resolution_percentage = 100
        scene.render.film_transparent = False
        scene.render.image_settings.file_format = 'OPEN_EXR'
        scene.render.image_settings.color_mode = 'RGBA'
        scene.render.image_settings.color_depth = '32'
        scene.render.filepath = str(exr)
        try:
            scene.eevee.taa_render_samples = 1
            report['render']['samples'] = 1
        except AttributeError:
            pass
        bpy.ops.render.render(write_still=True)

        candidates = [exr, output_dir / 'gpu_probe.exr0001']
        candidates.extend(sorted(output_dir.glob('gpu_probe*.exr')))
        image_path = next((path for path in candidates if path.is_file()), None)
        if image_path is None:
            raise RuntimeError('render finished but no EXR was written to {}'.format(output_dir))
        report['render']['image'] = str(image_path)

        image = bpy.data.images.load(str(image_path), check_existing=False)
        try:
            pixels = [float(value) for value in image.pixels]
        finally:
            bpy.data.images.remove(image)
        if not pixels:
            raise RuntimeError('loaded EXR has no pixel data')
        report['render']['pixel_sum'] = float(sum(pixels))
        if not any(value != 0.0 for value in pixels):
            raise RuntimeError('headless EEVEE render produced all-zero pixels')
        report['available'] = True
        report['reason'] = 'headless EEVEE render succeeded'
    except Exception as exc:  # noqa: BLE001 - any failure means "skip", not "pass"
        report['reason'] = 'headless EEVEE render failed: {}: {}'.format(type(exc).__name__, exc)
        report['render']['error'] = traceback.format_exc(limit=6)

    try:
        write_json(args.output, report)
    except Exception as exc:  # noqa: BLE001
        print('GOO_GPU_PROBE_WRITE_ERROR: {}: {}'.format(type(exc).__name__, exc), flush=True)
        return 1
    if report['available']:
        print('GOO_GPU_PROBE_AVAILABLE', flush=True)
    else:
        print('GOO_GPU_PROBE_UNAVAILABLE: {}'.format(report['reason']), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
