#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Golden-scene smoke manifest and runner for the Goo EEVEE pipeline.

This host-side script has two deliberately separate modes:

``--spec``
    Validates and writes the machine-readable *specification* for golden
    scenes (scene ids, expected checks, reference-hash policy, result schema).
    It never renders and never claims that a scene file exists.  This is the
    fast, deterministic ``goo_golden_manifest`` CTest entry.

``--run``
    Renders every golden ``.blend`` found in ``--golden-dir`` (or listed in its
    ``manifest.json``) through ``goo_golden_render.py``.  Missing Blender,
    missing GPU/EGL/Metal support or an empty golden directory result in exit
    code 77 (*skip*) with an explicit reason.  A rendered black frame is a
    failure, never a pass.  Scenes without a reference ``pixel_sha256`` are
    marked ``smoke_only`` in the result instead of pretending to be a golden
    comparison.

Exit codes: 0 = pass, 1 = fail, 77 = skip.  See ``README-goo-tests.md``.
"""

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import goo_test_util as util  # noqa: E402

SPEC_SCHEMA = 'goo-golden-smoke-spec/v1'

# The canonical smoke set.  Files are not shipped in-tree; a golden directory
# must provide them.  The spec below is the contract those files are checked
# against, not a claim that the files exist.
SPEC_SCENES = [
    {
        'id': 'goo_light_groups_reopen',
        'description': 'Reopened light-group fixture keeps masks and the default group.',
        'checks': ['engine_is_eevee', 'resolution_matches', 'non_degenerate',
                   'pixel_sha256_matches_reference'],
    },
    {
        'id': 'goo_bsdf_light_groups_shadow',
        'description': 'BSDF receiver with a selected light group and ignored shadows.',
        'checks': ['engine_is_eevee', 'resolution_matches', 'non_degenerate',
                   'pixel_sha256_matches_reference'],
    },
    {
        'id': 'goo_screenspace_planes',
        'description': 'Screenspace refraction planes: deferred reference frame.',
        'checks': ['engine_is_eevee', 'resolution_matches', 'non_degenerate',
                   'pixel_sha256_matches_reference'],
    },
    {
        'id': 'goo_screenspace_runtime_view',
        'description': 'Runtime camera move over the screenspace fixture.',
        'checks': ['engine_is_eevee', 'resolution_matches', 'non_degenerate',
                   'pixel_sha256_matches_reference'],
    },
]


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--spec', action='store_true',
                      help='validate/write the golden-scene specification only')
    mode.add_argument('--run', action='store_true',
                      help='render and compare golden scenes (needs Blender + GPU)')
    parser.add_argument('--output-dir', type=Path, required=True,
                        help='directory for spec.json, result.json and renders/')
    parser.add_argument('--blender', type=Path,
                        help='Blender binary (required with --run)')
    parser.add_argument('--golden-dir', type=Path,
                        default=REPO_ROOT / 'tests' / 'files' / 'goo_golden',
                        help='directory containing *.blend scenes and optional manifest.json')
    parser.add_argument('--resolution', type=int, default=64,
                        help='default render resolution for scenes without an override')
    parser.add_argument('--samples', type=int, default=16,
                        help='default EEVEE TAA render samples')
    parser.add_argument('--timeout', type=float, default=1740.0,
                        help='internal budget in seconds (keep below the CTest TIMEOUT)')
    return parser.parse_args()


def scene_reference(golden_dir, scene):
    """Return (reference_dict, source) from the manifest or a sidecar file."""
    reference = scene.get('reference')
    if isinstance(reference, dict) and reference.get('pixel_sha256'):
        return reference, 'manifest'
    sidecar = Path(golden_dir) / '{}.reference.json'.format(scene['id'])
    data = util.read_json(sidecar)
    if isinstance(data, dict) and data.get('pixel_sha256'):
        data = dict(data)
        data.setdefault('format', 'pixel_sha256')
        return data, str(sidecar)
    return None, None


def load_scenes(golden_dir, default_resolution, default_samples):
    """Load scenes from manifest.json, else from *.blend, keeping order stable."""
    golden_dir = Path(golden_dir)
    scenes = []
    manifest_path = golden_dir / 'manifest.json'
    manifest = util.read_json(manifest_path)
    if isinstance(manifest, dict) and isinstance(manifest.get('scenes'), list):
        for index, raw in enumerate(manifest['scenes']):
            if not isinstance(raw, dict):
                continue
            scene_id = str(raw.get('id') or 'scene_{}'.format(index)).strip()
            blend = raw.get('blend') or '{}.blend'.format(scene_id)
            scenes.append({
                'id': scene_id,
                'blend': (golden_dir / blend).resolve(),
                'resolution': int(raw.get('resolution', default_resolution)),
                'samples': int(raw.get('samples', default_samples)),
                'reference': raw.get('reference'),
                'required': bool(raw.get('required', True)),
            })
        return scenes, str(manifest_path)
    if golden_dir.is_dir():
        for blend in sorted(golden_dir.glob('*.blend')):
            scenes.append({
                'id': blend.stem,
                'blend': blend.resolve(),
                'resolution': default_resolution,
                'samples': default_samples,
                'reference': None,
                'required': True,
            })
    return scenes, None


def write_spec(output_dir, golden_dir, args):
    golden_dir = Path(golden_dir)
    golden_exists = golden_dir.is_dir()
    available = {}
    if golden_exists:
        available = {path.stem: path for path in golden_dir.glob('*.blend')}
    spec = {
        'schema': SPEC_SCHEMA,
        'generated_utc': util.utc_now(),
        'engine': 'BLENDER_EEVEE',
        'resolution_default': args.resolution,
        'samples_default': args.samples,
        'golden_dir': str(golden_dir),
        'golden_dir_exists': golden_exists,
        'result': {
            'path': str(Path(output_dir) / 'result.json'),
            'schema_version': util.RESULT_SCHEMA,
            'per_scene_fields': ['id', 'status', 'image', 'pixel_sha256', 'luminance',
                                 'engine', 'reference_used', 'smoke_only', 'failure_reason'],
        },
        'reference_policy': {
            'format': 'pixel_sha256',
            'sidecar': '<scene-id>.reference.json',
            'manifest_field': 'reference.pixel_sha256',
            'missing_reference': 'smoke_only (explicitly not a golden-pass)',
            'mismatch': 'fail',
        },
        'scenes': [],
        'notes': [
            'Spec-only: no .blend is created, opened or rendered by --spec.',
            'supply scene files in golden_dir; without them --run skips, never passes.',
        ],
    }
    for scene in SPEC_SCENES:
        entry = dict(scene)
        entry['blend'] = str(golden_dir / '{}.blend'.format(scene['id']))
        entry['resolution'] = args.resolution
        entry['samples'] = args.samples
        entry['reference_required'] = True
        entry['scene_present'] = scene['id'] in available
        entry['status'] = 'spec-only'
        spec['scenes'].append(entry)
    spec_path = Path(output_dir) / 'spec.json'
    util.write_json_atomic(spec_path, spec)
    return spec, spec_path, available


def check_spec(report, spec, spec_path, available):
    util.add_artifact(report, 'spec', spec_path)
    util.add_check(report, 'spec_schema', spec.get('schema') == SPEC_SCHEMA,
                   'schema={}'.format(spec.get('schema')))
    ids = [scene.get('id') for scene in spec.get('scenes', [])]
    util.add_check(report, 'unique_scene_ids', len(ids) == len(set(ids)),
                   '{} scene ids'.format(len(ids)))
    checks_ok = all(scene.get('checks') for scene in spec.get('scenes', []))
    util.add_check(report, 'scenes_have_checks', checks_ok,
                   'every spec scene declares expected checks')
    util.add_check(report, 'reference_policy', 'pixel_sha256' in str(spec.get('reference_policy')),
                   'reference hash policy documented')
    missing = [scene['id'] for scene in spec.get('scenes', []) if not scene.get('scene_present')]
    report['summary'] = {
        'mode': 'spec',
        'spec': str(spec_path),
        'scene_files_present': sorted(available),
        'missing_scene_files': missing,
        'rendered': False,
    }
    if missing:
        util.add_note(report,
                      'golden scene files absent for: {} (--run will skip); '
                      'no render was performed'.format(', '.join(missing)))
    util.add_limitation(report, 'spec-only run: golden pixels were not rendered or compared')


def run_scenes(report, args, test_root, work):
    blender_path = Path(args.blender) if args.blender else None
    if blender_path is None:
        return util.finish_skip(report, test_root / 'result.json',
                                '--run requires --blender')
    blender = util.blender_version(blender_path)
    report['blender'] = {key: blender.get(key) for key in
                         ('binary', 'available', 'version_text', 'version_file', 'build_hash', 'error')}
    util.add_check(report, 'blender_available', blender['available'],
                   blender.get('error') or blender.get('version_text') or '')
    if not blender['available']:
        return util.finish_skip(report, test_root / 'result.json',
                                'no usable Blender at {}: {}'.format(blender_path, blender.get('error')))
    if (blender.get('version_file') or 0) < 500:
        return util.finish_skip(report, test_root / 'result.json',
                                'golden scenes target Goo/Blender 5.x, found {}'.format(
                                    blender.get('version_text')))

    gpu = util.probe_gpu(blender_path, HERE / 'goo_gpu_probe.py',
                         test_root / 'gpu_probe.json',
                         timeout=min(180.0, max(30.0, args.timeout * 0.25)))
    report['gpu'] = {key: gpu.get(key) for key in
                     ('available', 'reason', 'backend', 'renderer', 'vendor', 'device_type')}
    util.add_artifact(report, 'gpu_probe', test_root / 'gpu_probe.json')
    util.add_check(report, 'gpu_headless_eevee', gpu['available'], gpu.get('reason') or '')
    if not gpu['available']:
        return util.finish_skip(report, test_root / 'result.json',
                                'GPU/headless EEVEE unavailable: {}'.format(gpu.get('reason')))

    golden_dir = Path(args.golden_dir)
    scenes, manifest_path = load_scenes(golden_dir, args.resolution, args.samples)
    if not scenes:
        return util.finish_skip(
            report, test_root / 'result.json',
            'no golden .blend scenes in {} (provide manifest.json or <scene>.blend)'.format(golden_dir))
    if manifest_path:
        util.add_artifact(report, 'golden_manifest', manifest_path)

    deadline = time.monotonic() + max(60.0, args.timeout - 30.0)
    results = []
    smoke_only = []
    for scene in scenes:
        scene_id = util.safe_name(scene['id'])
        scene_dir = work / scene_id
        render_result = scene_dir / 'render.json'
        reference, reference_source = scene_reference(golden_dir, scene)
        entry = {
            'id': scene_id,
            'status': 'fail',
            'blend': str(scene['blend']),
            'image': None,
            'pixel_sha256': None,
            'luminance': None,
            'engine': None,
            'reference_used': reference_source,
            'smoke_only': False,
            'failure_reason': None,
        }
        if not scene['blend'].is_file():
            entry['failure_reason'] = 'scene file missing: {}'.format(scene['blend'])
            util.add_check(report, '{}:scene_present'.format(scene_id), False,
                           entry['failure_reason'])
            results.append(entry)
            continue
        if not util.is_within(scene['blend'], golden_dir):
            entry['failure_reason'] = 'scene file escapes golden dir: {}'.format(scene['blend'])
            util.add_check(report, '{}:scene_path'.format(scene_id), False,
                           entry['failure_reason'])
            results.append(entry)
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 30.0:
            entry['failure_reason'] = 'internal timeout budget exhausted'
            util.add_check(report, '{}:budget'.format(scene_id), False, entry['failure_reason'])
            results.append(entry)
            continue
        command = [
            str(blender_path),
            '--background',
            '--factory-startup',
            '--python-exit-code',
            '1',
            '--python',
            str(HERE / 'goo_golden_render.py'),
            '--',
            '--blend',
            str(scene['blend']),
            '--output-dir',
            str(scene_dir),
            '--scene-id',
            scene_id,
            '--resolution',
            str(scene['resolution']),
            '--samples',
            str(scene['samples']),
            '--result',
            str(render_result),
        ]
        report['commands'].append(command)
        run = util.run_command(command, timeout=max(60.0, remaining),
                               log_path=scene_dir / 'render.log', cwd=test_root)
        util.add_artifact(report, 'log', run['log'])
        util.add_artifact(report, 'render_result', render_result)
        rendered = util.read_json(render_result)
        entry['render_exit'] = run['returncode']
        if not isinstance(rendered, dict):
            entry['failure_reason'] = 'renderer produced no result.json (exit {})'.format(
                run['returncode'])
            util.add_check(report, '{}:render_json'.format(scene_id), False,
                           entry['failure_reason'])
            results.append(entry)
            continue

        entry['image'] = rendered.get('image')
        entry['pixel_sha256'] = rendered.get('pixel_sha256')
        entry['luminance'] = rendered.get('luminance')
        entry['engine'] = rendered.get('engine')
        render_ok = run['returncode'] == 0 and rendered.get('rendered') is True
        util.add_check(report, '{}:rendered'.format(scene_id), render_ok,
                       rendered.get('error') or 'rendered={}'.format(rendered.get('rendered')))
        luminance = rendered.get('luminance') if isinstance(rendered.get('luminance'), dict) else {}
        util.add_check(report, '{}:non_degenerate'.format(scene_id),
                       bool(luminance.get('non_degenerate')),
                       'mean={} std={} max={}'.format(luminance.get('mean'),
                                                      luminance.get('std'),
                                                      luminance.get('max')))
        util.add_check(report, '{}:resolution'.format(scene_id),
                       rendered.get('resolution') == [scene['resolution'], scene['resolution']],
                       'resolution={}'.format(rendered.get('resolution')))
        util.add_check(report, '{}:engine_is_eevee'.format(scene_id),
                       'EEVEE' in str(rendered.get('engine') or ''),
                       'engine={}'.format(rendered.get('engine')))

        if not render_ok or not luminance.get('non_degenerate'):
            entry['failure_reason'] = 'render failed or frame is degenerate/black'
            results.append(entry)
            continue

        if reference:
            expected = reference.get('pixel_sha256')
            matches = bool(expected) and rendered.get('pixel_sha256') == expected
            util.add_check(report, '{}:pixel_sha256_matches_reference'.format(scene_id),
                           matches,
                           'expected={} actual={}'.format(expected, rendered.get('pixel_sha256')))
            if not matches:
                entry['failure_reason'] = 'pixel hash differs from reference'
                results.append(entry)
                continue
            entry['status'] = 'pass'
        else:
            entry['status'] = 'pass'
            entry['smoke_only'] = True
            smoke_only.append(scene_id)
            util.add_note(report,
                          '{}: no reference pixel_sha256; smoke-only pass'.format(scene_id))
        results.append(entry)

    failed = [entry for entry in results if entry['status'] != 'pass']
    report['summary'] = {
        'mode': 'run',
        'scenes': results,
        'failed': [entry['id'] for entry in failed],
        'smoke_only': smoke_only,
        'golden_dir': str(golden_dir),
    }
    if failed:
        return util.finish(report, test_root / 'result.json')
    if smoke_only:
        util.add_limitation(report,
                            'scenes without reference hashes passed smoke checks only: {}'.format(
                                ', '.join(smoke_only)))
    return util.finish(report, test_root / 'result.json')


def main():
    args = arguments()
    if args.run and args.blender is None:
        print('GOO_TEST_FAIL: --run requires --blender', file=sys.stderr)
        return 2
    output_dir = Path(args.output_dir)
    try:
        test_root, work = util.prepare_work_dir(output_dir.parent, output_dir.name)
    except (OSError, ValueError, RuntimeError) as exc:
        print('GOO_TEST_FAIL: cannot prepare output directory: {}'.format(exc), file=sys.stderr)
        return 1
    report = util.make_report('goo_golden_smoke' if args.run else 'goo_golden_manifest')
    report['commands'] = []
    report['summary'] = {}

    if args.spec:
        spec, spec_path, available = write_spec(test_root, args.golden_dir, args)
        check_spec(report, spec, spec_path, available)
        return util.finish(report, test_root / 'result.json')

    return run_scenes(report, args, test_root, work)


if __name__ == '__main__':
    sys.exit(main())
