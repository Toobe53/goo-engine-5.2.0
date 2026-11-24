#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Host-side CTest driver for the Goo Python tests.

The four Goo scripts in this directory cannot be registered directly:

* ``goo_light_groups.py`` is a *host* runner that itself spawns Blender once per
  matrix case (and therefore needs ``--blender``, ``--output``, ``--startup``),
* ``goo_bsdf_light_groups.py`` and ``goo_screenspace.py`` must run *inside*
  Blender and refuse an existing ``--output`` directory,
* ``goo_bsdf_light_groups_cases.py`` is a library with no ``main``; its cases are
  reached through ``goo_bsdf_light_groups.py --case <name>``.

This wrapper prepares a fresh output directory below ``--output-root``, runs a
small headless-EEVEE GPU probe first (a missing GPU/EGL/Metal produces exit
code 77 = *skip*, never a pass), invokes the real entry point, and writes an
auditable ``result.json`` (see ``README-goo-tests.md``).

Exit codes: 0 = pass, 1 = fail, 77 = skip.
"""

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import goo_test_util as util  # noqa: E402

TEST_SPECS = {
    'light-groups': {
        'script': HERE / 'goo_light_groups.py',
        'kind': 'host',
        'startup': True,
        'cases': None,
    },
    'bsdf-light-groups': {
        'script': HERE / 'goo_bsdf_light_groups.py',
        'kind': 'blender',
        'startup': True,
        'cases': ['default', 'groups', 'slots', 'shadows', 'self', 'contact'],
    },
    'bsdf-light-groups-cases': {
        'script': HERE / 'goo_bsdf_light_groups.py',
        'kind': 'blender',
        'startup': True,
        'cases': ['high_bits', 'table_growth', 'linking', 'world',
                  'probe_plane', 'probe_sphere', 'volume_probe'],
    },
    'screenspace': {
        'script': HERE / 'goo_screenspace.py',
        'kind': 'blender',
        'startup': False,
        'cases': [None],
    },
}


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test', choices=sorted(TEST_SPECS), required=True)
    parser.add_argument('--blender', type=Path, required=True,
                        help='Blender binary to test (usually ${TEST_BLENDER_EXE})')
    parser.add_argument('--startup', type=Path,
                        help='source scripts/startup directory containing goo_engine_light_groups.py')
    parser.add_argument('--output-root', type=Path,
                        default=Path.cwd() / 'goo_test_output',
                        help='root for the per-test output and result.json')
    parser.add_argument('--timeout', type=float, default=1740.0,
                        help='internal budget in seconds (keep below the CTest TIMEOUT)')
    parser.add_argument('--cases', default='',
                        help='comma separated override of the case list (Blender tests only)')
    return parser.parse_args()


def make_blender_command(blender, script, output_dir, startup, case):
    command = [
        str(blender),
        '--background',
        '--factory-startup',
        '--python-exit-code',
        '1',
        '--python',
        str(script),
        '--',
        '--output',
        str(output_dir),
    ]
    if startup is not None:
        command.extend(['--startup', str(startup)])
    if case is not None:
        command.extend(['--case', case])
    return command


def check_host_matrix(report, run, matrix_dir):
    manifest_path = Path(matrix_dir) / 'manifest.json'
    manifest = util.read_json(manifest_path)
    util.add_artifact(report, 'manifest', manifest_path)
    util.add_artifact(report, 'log', run['log'])

    util.add_check(report, 'exit_code', run['returncode'] == 0,
                   'exit {} (timeout={})'.format(run['returncode'], run['timed_out']))
    util.add_check(report, 'manifest_json', isinstance(manifest, dict),
                   'readable manifest at {}'.format(manifest_path))
    if not isinstance(manifest, dict):
        report['summary'] = {'mode': 'host-matrix', 'log_tail': util.tail_file(run['log'])}
        return

    util.add_check(report, 'version_exit_code', manifest.get('version_exit_code') == 0,
                   'version_exit_code={}'.format(manifest.get('version_exit_code')))
    util.add_check(report, 'manifest_pass', manifest.get('pass') is True,
                   'manifest pass={}'.format(manifest.get('pass')))

    cases = manifest.get('cases')
    items = []
    if isinstance(cases, dict):
        items = list(cases.items())
    elif isinstance(cases, list):
        for index, value in enumerate(cases):
            name = (value.get('case') or value.get('name') or 'case_{}'.format(index)
                    if isinstance(value, dict) else 'case_{}'.format(index))
            items.append((name, value))
    for name, value in items:
        ok = bool(value.get('pass')) if isinstance(value, dict) else bool(value)
        detail = ''
        if isinstance(value, dict):
            detail = 'exit={}'.format(value.get('exit_code', value.get('returncode')))
        util.add_check(report, 'case:{}'.format(name), ok, detail)
    report['summary'] = {
        'mode': 'host-matrix',
        'case_count': len(items),
        'manifest': str(manifest_path),
        'log_tail': util.tail_file(run['log']) if run['returncode'] != 0 else '',
    }


def check_blender_case(report, case, run, case_dir, is_screenspace):
    result_path = Path(case_dir) / 'result.json'
    result = util.read_json(result_path)
    util.add_artifact(report, 'log', run['log'])
    util.add_artifact(report, 'script_result', result_path)

    label = case or 'default'
    util.add_check(report, '{}:exit'.format(label), run['returncode'] == 0,
                   'exit {} (timeout={})'.format(run['returncode'], run['timed_out']))
    util.add_check(report, '{}:result_json'.format(label), isinstance(result, dict),
                   'result at {}'.format(result_path))
    if not isinstance(result, dict):
        return {
            'case': label,
            'status': 'fail',
            'log_tail': util.tail_file(run['log']),
        }

    if is_screenspace:
        records = result.get('records') if isinstance(result.get('records'), dict) else {}
        actual = records.get('actual') if isinstance(records.get('actual'), dict) else {}
        ok = run['returncode'] == 0 and bool(actual.get('pixel_sha256'))
        util.add_check(report, '{}:rendered_actual'.format(label), ok,
                       'actual pixel_sha256={}'.format(actual.get('pixel_sha256')))
        return {'case': label, 'status': 'pass' if ok else 'fail'}

    checks = result.get('checks') if isinstance(result.get('checks'), dict) else {}
    failed = sorted(name for name, value in checks.items() if not value)
    pass_flag = result.get('pass') is True
    util.add_check(report, '{}:script_pass'.format(label), pass_flag,
                   'script pass={}'.format(result.get('pass')))
    if failed:
        util.add_check(report, '{}:checks'.format(label), False,
                       'failed: {}'.format(', '.join(failed[:8])))
    else:
        util.add_check(report, '{}:checks'.format(label), True,
                       '{} checks passed'.format(len(checks)))
    status = 'pass' if (run['returncode'] == 0 and pass_flag and not failed) else 'fail'
    entry = {'case': label, 'status': status, 'check_count': len(checks)}
    if failed:
        entry['failed_checks'] = failed[:20]
    if status == 'fail':
        entry['log_tail'] = util.tail_file(run['log'])
    return entry


def main():
    args = arguments()
    spec = TEST_SPECS[args.test]
    test_name = 'goo_{}'.format(args.test.replace('-', '_'))

    try:
        test_root, work = util.prepare_work_dir(args.output_root, args.test)
    except (OSError, ValueError, RuntimeError) as exc:
        print('GOO_TEST_FAIL: {}: cannot prepare output directory: {}'.format(test_name, exc))
        return 1
    result_path = test_root / 'result.json'
    report = util.make_report(test_name)
    report['commands'] = []
    report['summary'] = {}

    blender = util.blender_version(args.blender)
    report['blender'] = {
        key: blender.get(key)
        for key in ('binary', 'available', 'version_text', 'major', 'minor', 'patch',
                    'version_file', 'build_hash', 'error')
    }
    util.add_check(report, 'blender_available', blender['available'],
                   blender.get('error') or blender.get('version_text') or '')
    if not blender['available']:
        return util.finish_skip(
            report, result_path,
            'no usable Blender binary at {}: {}'.format(args.blender, blender.get('error')),
        )

    gpu = util.probe_gpu(
        args.blender,
        HERE / 'goo_gpu_probe.py',
        test_root / 'gpu_probe.json',
        timeout=min(180.0, max(30.0, args.timeout * 0.25)),
    )
    report['gpu'] = {
        key: gpu.get(key)
        for key in ('available', 'reason', 'backend', 'renderer', 'vendor',
                    'device_type', 'returncode')
    }
    util.add_artifact(report, 'gpu_probe', test_root / 'gpu_probe.json')
    util.add_check(report, 'gpu_headless_eevee', gpu['available'], gpu.get('reason') or '')
    if not gpu['available']:
        return util.finish_skip(
            report, result_path,
            'GPU/headless EEVEE unavailable: {}'.format(gpu.get('reason') or 'unknown reason'),
        )

    deadline = time.monotonic() + max(60.0, args.timeout - 30.0)

    def remaining():
        return max(30.0, deadline - time.monotonic())

    if spec['kind'] == 'host':
        matrix_dir = work / 'matrix'
        command = [
            sys.executable or 'python',
            str(spec['script']),
            '--blender',
            str(args.blender),
            '--output',
            str(matrix_dir),
        ]
        if args.startup is not None:
            command.extend(['--startup', str(args.startup)])
        report['commands'].append([str(part) for part in command])
        run = util.run_command(command, timeout=remaining(),
                               log_path=work / 'light_groups.log', cwd=test_root)
        check_host_matrix(report, run, matrix_dir)
    else:
        cases = spec['cases']
        if args.cases.strip():
            cases = [case.strip() for case in args.cases.split(',') if case.strip()]
        results = []
        for case in cases:
            if time.monotonic() >= deadline:
                util.add_check(report, '{}:budget'.format(case or 'default'), False,
                               'internal timeout budget exhausted before this case')
                results.append({'case': case or 'default', 'status': 'fail',
                                'reason': 'timeout budget exhausted'})
                continue
            case_dir = work / ('case_{}'.format(case) if case else 'screenspace')
            command = make_blender_command(
                args.blender, spec['script'], case_dir,
                args.startup if spec['startup'] else None, case,
            )
            report['commands'].append([str(part) for part in command])
            log_path = work / '{}_run.log'.format(case or 'screenspace')
            run = util.run_command(command, timeout=remaining(),
                                   log_path=log_path, cwd=test_root)
            entry = check_blender_case(report, case, run, case_dir,
                                       is_screenspace=(args.test == 'screenspace'))
            results.append(entry)
        report['summary'] = {
            'mode': 'blender',
            'cases': results,
            'failed': [entry['case'] for entry in results if entry['status'] == 'fail'],
        }

    return util.finish(report, result_path)


if __name__ == '__main__':
    sys.exit(main())
