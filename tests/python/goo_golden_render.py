# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Render one golden .blend inside Blender for ``goo_golden_smoke.py``.

The host-side smoke runner passes an explicit scene file and output directory;:

    blender --background --factory-startup --python-exit-code 1 \
        --python goo_golden_render.py -- \
        --blend <scene.blend> --output-dir <dir> --scene-id <id> \
        --resolution 64 --samples 16 --result <result.json>

It writes the EXR, a pixel SHA-256 over the RGBA float buffer and simple
luminance statistics.  A black/degenerate image is *reported*, not hidden: the
host runner decides whether that is a failure.  The script writes its JSON even
when rendering raises and exits non-zero in that case.
"""

import argparse
import hashlib
import json
import struct
import sys
import time
import traceback
from pathlib import Path

import bpy
import numpy as np


def arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blend', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--scene-id', required=True)
    parser.add_argument('--resolution', type=int, default=64)
    parser.add_argument('--samples', type=int, default=16)
    parser.add_argument('--result', type=Path, required=True)
    argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else None
    return parser.parse_args(argv)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf8')
    tmp.replace(path)


def set_engine(scene):
    last_error = None
    for engine in ('BLENDER_EEVEE', 'BLENDER_EEVEE_NEXT'):
        try:
            scene.render.engine = engine
            return engine, None
        except TypeError as exc:
            last_error = '{}: {}'.format(type(exc).__name__, exc)
    return None, last_error


def pixel_digest(pixels, width, height):
    flat = np.ascontiguousarray(pixels, dtype=np.float32).ravel()
    digest = hashlib.sha256()
    digest.update(struct.pack('<II', int(width), int(height)))
    digest.update(flat.tobytes())
    return digest.hexdigest()


def main():
    args = arguments()
    started = time.monotonic()
    report = {
        'schema': 'goo-golden-render/v1',
        'scene_id': args.scene_id,
        'blend': str(args.blend),
        'rendered': False,
        'engine': None,
        'resolution': [args.resolution, args.resolution],
        'samples': args.samples,
        'image': None,
        'pixel_sha256': None,
        'luminance': None,
        'blender_version': bpy.app.version_string,
        'blender_version_file': bpy.app.version[0] * 100 + bpy.app.version[1],
        'error': None,
        'duration_seconds': None,
    }
    try:
        if not args.blend.is_file():
            raise FileNotFoundError('golden scene file not found: {}'.format(args.blend))
        bpy.ops.wm.open_mainfile(filepath=str(args.blend))
        scene = bpy.context.scene
        scene.frame_set(1)
        engine, engine_error = set_engine(scene)
        if engine is None:
            raise RuntimeError('no EEVEE engine available: {}'.format(engine_error))
        report['engine'] = engine
        scene.render.resolution_x = args.resolution
        scene.render.resolution_y = args.resolution
        scene.render.resolution_percentage = 100
        scene.render.film_transparent = False
        scene.render.image_settings.file_format = 'OPEN_EXR'
        scene.render.image_settings.color_mode = 'RGBA'
        scene.render.image_settings.color_depth = '32'
        try:
            scene.eevee.taa_render_samples = args.samples
            report['samples'] = args.samples
        except AttributeError:
            report['samples'] = None
        args.output_dir.mkdir(parents=True, exist_ok=True)
        exr = args.output_dir / '{}.exr'.format(args.scene_id)
        scene.render.filepath = str(exr)
        bpy.ops.render.render(write_still=True)

        candidates = [exr]
        candidates.extend(sorted(args.output_dir.glob('{}*.exr'.format(args.scene_id))))
        image_path = next((path for path in candidates if path.is_file()), None)
        if image_path is None:
            raise RuntimeError('render finished but no EXR exists in {}'.format(args.output_dir))
        report['image'] = str(image_path)

        image = bpy.data.images.load(str(image_path), check_existing=False)
        try:
            width, height = image.size[0], image.size[1]
            pixels = np.asarray(image.pixels[:], dtype=np.float32).reshape(height, width, 4)
        finally:
            bpy.data.images.remove(image)
        if width != args.resolution or height != args.resolution:
            raise RuntimeError('unexpected image size {}x{}'.format(width, height))

        report['pixel_sha256'] = pixel_digest(pixels, width, height)
        luminance = (0.2126 * pixels[..., 0] + 0.7152 * pixels[..., 1]
                     + 0.0722 * pixels[..., 2])
        stats = {
            'mean': float(luminance.mean()),
            'std': float(luminance.std()),
            'min': float(luminance.min()),
            'max': float(luminance.max()),
            'non_degenerate': bool(float(luminance.mean()) > 1e-5
                                   and float(luminance.std()) > 1e-6
                                   and float(luminance.max()) > 1e-4),
        }
        report['luminance'] = stats
        report['rendered'] = True
    except Exception as exc:  # noqa: BLE001 - report failures, host runner decides
        report['error'] = '{}: {}'.format(type(exc).__name__, exc)
        report['traceback'] = traceback.format_exc(limit=8)

    report['duration_seconds'] = round(time.monotonic() - started, 3)
    write_json(args.result, report)
    if report['rendered']:
        print('GOO_GOLDEN_RENDER_RENDERED: {}'.format(args.scene_id), flush=True)
        return 0
    print('GOO_GOLDEN_RENDER_FAILED: {}: {}'.format(args.scene_id, report['error']), flush=True)
    return 1


if __name__ == '__main__':
    sys.exit(main())
