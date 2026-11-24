#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Migration-fixture manifest for the Goo 5.2 file-format gate.

This is a *descriptor-level* test: it does not fabricate ``.blend`` binaries.
It encodes the versioning rules the 5.2 loader applies to:

* legacy EEVEE ``OPAQUE`` provenance (``MA_LEGACY_OPAQUE``) and the deprecated
  ``blend_method`` RNA mapping,
* legacy EEVEE Bloom -> Compositor Glare migration,
* light-group bitmasks (``light_group_bits`` / ``light_group_shadow_bits``) and
  the missing-DNA default group ``(0, 0, 0, 1)``.

Each row covers an official Blender file version (4.1, 4.2, 5.0, 5.1, 5.2.1) or
the Goo fork 4.4 layout.  The expected post-load state is computed from the
same rules and compared against the hand-written expectation in each case, so a
change in either the expectation table or the rule model fails the test.

Two CTest entries use this script:

* ``goo_migration_manifest`` runs with ``--descriptor-only``: it validates and
  writes the manifest, passes without Blender, and never claims that a file was
  loaded.
* ``goo_migration_fixtures`` runs with ``--blender``: it additionally probes the
  built 5.2+ Blender for the required RNA surface.  When no usable build exists
  the test *skips* (exit 77), it never passes.

Exit codes: 0 = pass, 1 = fail, 77 = skip.
"""

import argparse
import copy
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import goo_test_util as util  # noqa: E402

MANIFEST_SCHEMA = 'goo-migration-fixtures/v1'
TARGET_VERSION_FILE = 502
TARGET_FILE_SUBVERSION = 48
DEFAULT_MASK = [0, 0, 0, 1]
BLOOM_RESOLUTION = (1920, 1080)
BLOOM_DEFAULT = {
    'color': [1.0, 1.0, 1.0],
    'threshold': 0.8,
    'knee': 0.5,
    'intensity': 0.05,
    'radius': 6.5,
    'clamp': 0.0,
}


# ---------------------------------------------------------------------------
# Rule tables (each citation is checked against the on-disk source)
# ---------------------------------------------------------------------------

RULES = [
    {
        'id': 'R_LIGHT_GROUP_DEFAULTS',
        'summary': 'Missing Goo light-group DNA gets the reserved default group (0,0,0,1) '
                   'for Material, Material.shadow and Light; the check is DNA-only and '
                   'not version gated.',
        'file': 'source/blender/blenloader/intern/versioning_520.cc',
        'lines': [686, 743],
    },
    {
        'id': 'R_LEGACY_OPAQUE_PROVENANCE',
        'summary': 'For file subversion < 48, MA_LEGACY_OPAQUE is recomputed: '
                   '(legacy_eevee_era || goo_file_dna || from_goo) && '
                   '(was_legacy_opaque || raw blend_method == MA_BM_SOLID).',
        'file': 'source/blender/blenloader/intern/versioning_520.cc',
        'lines': [744, 790],
    },
    {
        'id': 'R_PROVENANCE_CURRENT_FORMAT',
        'summary': 'The provenance block is skipped entirely for 5.2.1 files '
                   '(subversion >= 48); a stored flag is preserved as-is.',
        'file': 'source/blender/blenloader/intern/versioning_520.cc',
        'lines': [744, 745],
    },
    {
        'id': 'R_GOO_ROLLBACK_MARKERS',
        'summary': 'goo_file_dna requires versionfile < 500 plus check_shadow_id, '
                   'Material.light_group_shadow_bits[4] and '
                   'NodeShaderInfo.light_group_shadow_bits[4] in the file DNA.',
        'file': 'source/blender/blenloader/intern/versioning_520.cc',
        'lines': [753, 770],
    },
    {
        'id': 'R_BLOOM_CALL_GATE',
        'summary': 'Bloom->Glare is only called for files older than 502/46; a 5.2.1 '
                   'file therefore never invokes the conversion.',
        'file': 'source/blender/blenloader/intern/versioning_520.cc',
        'lines': [785, 790],
    },
    {
        'id': 'R_BLOOM_GATE',
        'summary': 'The conversion returns for versionfile >= 500 and only runs when '
                   'SCE_EEVEE_BLOOM_ENABLED is set; the bit is cleared after a successful '
                   'migration.',
        'file': 'source/blender/blenloader/intern/versioning_520.cc',
        'lines': [481, 499],
    },
    {
        'id': 'R_BLOOM_GLARE',
        'summary': 'Glare mapping: size=clamp(radius/8.5,0,1), '
                   'chain_length=int(log2(max(1, smaller_dim*size))), '
                   'strength=clamp(intensity*max(1,chain_length),0,1), type BLOOM, '
                   'quality HIGH, threshold/knee/clamp/maximum/color from the legacy block.',
        'file': 'source/blender/blenloader/intern/versioning_520.cc',
        'lines': [560, 629],
    },
    {
        'id': 'R_SURFACE_401',
        'summary': 'A 4.1 file without surface_render_method maps raw blend_method: '
                   'BLEND -> FORWARD, otherwise DEFERRED.',
        'file': 'source/blender/blenloader/intern/versioning_410.cc',
        'lines': [887, 900],
    },
    {
        'id': 'R_SURFACE_420',
        'summary': 'Pre-4.2/64 EEVEE files recompute blend shadows and remap '
                   'surface_render_method from blend_method.',
        'file': 'source/blender/blenloader/intern/versioning_420.cc',
        'lines': [1354, 1366],
    },
    {
        'id': 'R_RNA_BLEND_METHOD',
        'summary': 'RNA blend_method is a compatibility view: legacy flag -> OPAQUE, '
                   'DEFERRED -> HASHED, FORWARD -> BLEND; the setter keeps provenance '
                   'when the exposed value does not change.',
        'file': 'source/blender/makesrna/intern/rna_material.cc',
        'lines': [222, 280],
    },
    {
        'id': 'R_LIGHT_GROUP_BITS_MAPPING',
        'summary': 'Named groups map to bits 0..126 (set_bit uses bit//32 and '
                   '(bit+1)%32); the default group is bit 127, i.e. mask (0,0,0,1).',
        'file': 'scripts/startup/goo_engine_light_groups.py',
        'lines': [47, 131],
    },
    {
        'id': 'R_DNA_LIGHT_GROUP_DEFAULTS',
        'summary': 'DNA defaults: light_group_bits[4] and light_group_shadow_bits[4] '
                   'are {0,0,0,1}; check_shadow_id is a Goo-only char.',
        'file': 'source/blender/makesdna/DNA_material_types.h',
        'lines': [454, 476],
    },
    {
        'id': 'R_BLOOM_DNA',
        'summary': 'Legacy bloom defaults: color (1,1,1), threshold 0.8, knee 0.5, '
                   'intensity 0.05, radius 6.5, clamp 0.0.',
        'file': 'source/blender/makesdna/DNA_scene_types.h',
        'lines': [2724, 2732],
    },
    {
        'id': 'R_GROUP_INTERSECT',
        'summary': 'The EEVEE shader intersects int4 masks; the default group bit is '
                   '0b1000 in the first int, i.e. (0,0,0,1).',
        'file': 'source/blender/draw/engines/eevee/shaders/eevee_light_groups.bsl.hh',
        'lines': [6, 14],
    },
]

RULE_IDS = {rule['id'] for rule in RULES}


# ---------------------------------------------------------------------------
# Rule model (host-side reimplementation of the documented C++/Python logic)
# ---------------------------------------------------------------------------

def make_glare():
    width, height = BLOOM_RESOLUTION
    size = min(max(BLOOM_DEFAULT['radius'] / 8.5, 0.0), 1.0)
    smaller = min(width, height)
    chain_length = int(math.log2(max(1.0, smaller * size)))
    strength = min(max(BLOOM_DEFAULT['intensity'] * max(1, chain_length), 0.0), 1.0)
    return {
        'label': 'Goo Bloom',
        'type': 'CMP_NODE_GLARE_BLOOM',
        'quality': 'HIGH',
        'size': round(size, 12),
        'chain_length': chain_length,
        'strength': round(strength, 12),
        'threshold': BLOOM_DEFAULT['threshold'],
        'smoothness': BLOOM_DEFAULT['knee'],
        'clamp': BLOOM_DEFAULT['clamp'] > 0.0,
        'maximum': BLOOM_DEFAULT['clamp'],
        'saturation': 1.0,
        'tint': list(BLOOM_DEFAULT['color']) + [1.0],
    }


GLARE_DEFAULT = make_glare()


def compute_light_groups(case):
    source_dna = case['source_dna']
    authored = case['input'].get('authored_light_groups') or {}
    default = list(DEFAULT_MASK)

    def bits(present, key):
        if not present:
            return list(default)
        if key not in authored:
            raise KeyError('{}: authored mask {} missing although DNA is present'.format(
                case['id'], key))
        return list(authored[key])

    return {
        'material_light_group_bits': bits(source_dna['material_light_group_bits'],
                                          'material'),
        'material_light_group_shadow_bits': bits(source_dna['material_light_group_shadow_bits'],
                                                 'shadow'),
        'light_light_group_bits': bits(source_dna['light_light_group_bits'],
                                       'light'),
        'node_shader_info_goo_fields': bool(source_dna['node_shader_info_light_group_shadow_bits']),
    }


def compute_expected(case):
    version_file = case['file_version']['version_file']
    subversion = case['file_version']['subversionfile']
    source_dna = case['source_dna']
    material = case['input']['material']
    scene = case['input']['scene']

    # surface_render_method: before 4.1.7 / 4.2.64 the field did not exist and
    # was derived from blend_method (BLEND -> FORWARD, else DEFERRED).
    surface = material.get('surface_render_method') or 'DEFERRED'

    provenance_block = (version_file < TARGET_VERSION_FILE
                        or (version_file == TARGET_VERSION_FILE
                            and subversion < TARGET_FILE_SUBVERSION))
    if provenance_block:
        legacy_eevee_era = version_file < 402
        goo_file_dna = (
            version_file < 500
            and source_dna['check_shadow_id']
            and source_dna['material_light_group_shadow_bits']
            and source_dna['node_shader_info_light_group_shadow_bits']
        )
        from_goo = bool(material.get('uses_goo_shader_info_node'))
        was_legacy_opaque = bool(material.get('legacy_opaque_flag'))
        flag = ((legacy_eevee_era or goo_file_dna or from_goo)
                and (was_legacy_opaque or material['raw_blend_method'] == 'SOLID'))
    else:
        flag = bool(material.get('legacy_opaque_flag'))

    if flag:
        rna_blend_method = 'OPAQUE'
    elif surface == 'FORWARD':
        rna_blend_method = 'BLEND'
    else:
        rna_blend_method = 'HASHED'

    bloom_enabled = bool(scene['legacy_eevee_bloom_enabled'])
    bloom_called = not (version_file > TARGET_VERSION_FILE
                        or (version_file == TARGET_VERSION_FILE
                            and subversion >= 46))
    bloom_migrated = bloom_called and version_file < 500 and bloom_enabled
    if bloom_migrated:
        bloom_flag_after = False
    elif version_file >= 500:
        bloom_flag_after = bloom_enabled
    else:
        bloom_flag_after = False

    return {
        'surface_render_method': surface,
        'legacy_opaque_flag': bool(flag),
        'rna_blend_method': rna_blend_method,
        'bloom': {
            'migrated': bloom_migrated,
            'flag_after': bloom_flag_after,
            'glare': copy.deepcopy(GLARE_DEFAULT) if bloom_migrated else None,
        },
        'light_groups': compute_light_groups(case),
    }


def expected(flag, rna_blend_method, bloom_migrated, bloom_flag_after,
             light_groups, node_goo_fields):
    return {
        'surface_render_method': 'DEFERRED',
        'legacy_opaque_flag': bool(flag),
        'rna_blend_method': rna_blend_method,
        'bloom': {
            'migrated': bloom_migrated,
            'flag_after': bloom_flag_after,
            'glare': copy.deepcopy(GLARE_DEFAULT) if bloom_migrated else None,
        },
        'light_groups': {
            'material_light_group_bits': list(light_groups['material']),
            'material_light_group_shadow_bits': list(light_groups['shadow']),
            'light_light_group_bits': list(light_groups['light']),
            'node_shader_info_goo_fields': node_goo_fields,
        },
    }


def build_cases():
    official_no_goo_dna = {
        'check_shadow_id': False,
        'material_light_group_shadow_bits': False,
        'node_shader_info_light_group_shadow_bits': False,
        'material_light_group_bits': False,
        'light_light_group_bits': False,
    }
    goo_dna = {
        'check_shadow_id': True,
        'material_light_group_shadow_bits': True,
        'node_shader_info_light_group_shadow_bits': True,
        'material_light_group_bits': True,
        'light_light_group_bits': True,
    }
    defaults = {'material': DEFAULT_MASK, 'shadow': DEFAULT_MASK, 'light': DEFAULT_MASK}
    goo_masks = {
        'material': [2, 0, 0, 0],
        'shadow': [0, 0, 0, 0],
        'light': [2, 0, 0, 0],
    }
    default_bloom_scene = {
        'engine': 'BLENDER_EEVEE',
        'resolution': list(BLOOM_RESOLUTION),
        'legacy_eevee_bloom_enabled': True,
        'legacy_bloom': copy.deepcopy(BLOOM_DEFAULT),
    }
    cases = []

    def add(case_id, label, description, version, subversion, source_dna,
            material, scene, authored_light_groups, expectation, citations):
        major, minor, patch = version
        case = {
            'id': case_id,
            'label': label,
            'description': description,
            'file_version': {
                'major': major,
                'minor': minor,
                'patch': patch,
                'version_file': major * 100 + minor,
                'subversionfile': subversion,
                'note': 'synthetic subversion reaches the documented version guard; '
                        'release files may carry a different subversion',
            },
            'source_dna': source_dna,
            'input': {
                'material': material,
                'scene': scene,
                'authored_light_groups': authored_light_groups,
            },
            'expected': expectation,
            'rules': citations,
            'blend_binary': None,
            'binary_note': 'descriptor-level fixture: no .blend binary is produced, '
                           'written or hashed',
        }
        cases.append(case)

    add(
        'official_4_1',
        'Official Blender 4.1 (legacy EEVEE)',
        'Legacy EEVEE file: no Goo DNA, raw OPAQUE blend mode and legacy Bloom enabled. '
        '4.1 predates the legacy-EEVEE provenance era, so the flag is reconstructed.',
        (4, 1, 0), 0, dict(official_no_goo_dna),
        {'raw_blend_method': 'SOLID', 'surface_render_method': None,
         'legacy_opaque_flag': False, 'uses_goo_shader_info_node': False},
        copy.deepcopy(default_bloom_scene), None,
        expected(True, 'OPAQUE', True, False, defaults, False),
        ['R_LIGHT_GROUP_DEFAULTS', 'R_SURFACE_401', 'R_LEGACY_OPAQUE_PROVENANCE',
         'R_BLOOM_CALL_GATE', 'R_BLOOM_GATE', 'R_BLOOM_GLARE', 'R_RNA_BLEND_METHOD'],
    )
    add(
        'official_4_2',
        'Official Blender 4.2 (legacy EEVEE)',
        '4.2 is the last official legacy-EEVEE format row with synthetic subversion 0: '
        'the surface-method remap runs, Bloom is still migrated, but the legacy OPAQUE '
        'era has ended because versionfile is not < 402.',
        (4, 2, 0), 0, dict(official_no_goo_dna),
        {'raw_blend_method': 'SOLID', 'surface_render_method': None,
         'legacy_opaque_flag': False, 'uses_goo_shader_info_node': False},
        copy.deepcopy(default_bloom_scene), None,
        expected(False, 'HASHED', True, False, defaults, False),
        ['R_LIGHT_GROUP_DEFAULTS', 'R_SURFACE_420', 'R_LEGACY_OPAQUE_PROVENANCE',
         'R_BLOOM_CALL_GATE', 'R_BLOOM_GATE', 'R_BLOOM_GLARE', 'R_RNA_BLEND_METHOD'],
    )
    add(
        'official_5_0',
        'Official Blender 5.0',
        'New-EEVEE file with a lingering Bloom flag: the 5.2 loader returns before the '
        'Bloom conversion for versionfile >= 500, so the flag is left untouched and no '
        'Glare is added.',
        (5, 0, 0), 0, dict(official_no_goo_dna),
        {'raw_blend_method': 'HASHED', 'surface_render_method': 'DEFERRED',
         'legacy_opaque_flag': False, 'uses_goo_shader_info_node': False},
        copy.deepcopy(default_bloom_scene), None,
        expected(False, 'HASHED', False, True, defaults, False),
        ['R_LIGHT_GROUP_DEFAULTS', 'R_LEGACY_OPAQUE_PROVENANCE', 'R_BLOOM_CALL_GATE',
         'R_RNA_BLEND_METHOD'],
    )
    add(
        'official_5_1',
        'Official Blender 5.1',
        'Same Bloom gate as 5.0: versionfile >= 500 means the legacy Bloom bit is ignored '
        'and never converted.',
        (5, 1, 0), 0, dict(official_no_goo_dna),
        {'raw_blend_method': 'HASHED', 'surface_render_method': 'DEFERRED',
         'legacy_opaque_flag': False, 'uses_goo_shader_info_node': False},
        copy.deepcopy(default_bloom_scene), None,
        expected(False, 'HASHED', False, True, defaults, False),
        ['R_LIGHT_GROUP_DEFAULTS', 'R_LEGACY_OPAQUE_PROVENANCE', 'R_BLOOM_CALL_GATE',
         'R_RNA_BLEND_METHOD'],
    )
    add(
        'official_5_2_1',
        'Official Blender 5.2.1 (current format)',
        'Current file format at BLENDER_FILE_SUBVERSION 48: the provenance block and the '
        'Bloom call are both skipped, so stored state is preserved. Official files have no '
        'Goo DNA and therefore receive the default light-group mask.',
        (5, 2, 1), TARGET_FILE_SUBVERSION, dict(official_no_goo_dna),
        {'raw_blend_method': 'HASHED', 'surface_render_method': 'DEFERRED',
         'legacy_opaque_flag': False, 'uses_goo_shader_info_node': False},
        {'engine': 'BLENDER_EEVEE', 'resolution': list(BLOOM_RESOLUTION),
         'legacy_eevee_bloom_enabled': False, 'legacy_bloom': copy.deepcopy(BLOOM_DEFAULT)},
        None,
        expected(False, 'HASHED', False, False, defaults, False),
        ['R_LIGHT_GROUP_DEFAULTS', 'R_PROVENANCE_CURRENT_FORMAT', 'R_BLOOM_CALL_GATE',
         'R_RNA_BLEND_METHOD'],
    )
    add(
        'goo_4_4',
        'Goo 4.4 (legacy EEVEE + Goo DNA)',
        'Goo fork file: check_shadow_id and shadow-bit DNA are present (goo_file_dna), '
        'light groups were authored with the Key group (bit 0 -> mask 2) and shadows '
        'ignored (empty shadow mask). Raw OPAQUE blend mode reconstructs the legacy flag.',
        (4, 4, 0), 0, dict(goo_dna),
        {'raw_blend_method': 'SOLID', 'surface_render_method': 'DEFERRED',
         'legacy_opaque_flag': False, 'uses_goo_shader_info_node': False},
        copy.deepcopy(default_bloom_scene), copy.deepcopy(goo_masks),
        expected(True, 'OPAQUE', True, False, goo_masks, True),
        ['R_LIGHT_GROUP_DEFAULTS', 'R_GOO_ROLLBACK_MARKERS', 'R_LEGACY_OPAQUE_PROVENANCE',
         'R_BLOOM_CALL_GATE', 'R_BLOOM_GATE', 'R_BLOOM_GLARE', 'R_RNA_BLEND_METHOD',
         'R_LIGHT_GROUP_BITS_MAPPING'],
    )
    return cases


LIMITATIONS = [
    'Descriptor-level only: no .blend binary is produced, opened or hashed; the loader '
    'behaviour is modelled from the cited source, not executed against a fixture file.',
    'The 4.x and 5.0/5.1 rows use a synthetic file subversion 0 so the documented guards '
    'are exercised. Real release files may carry another subversion; the asserted outcomes '
    'depend on versionfile and (for the 5.2.1 row) on the current subversion 48.',
    'Legacy Bloom has no RNA surface (only the deprecated use_pass_bloom render pass exists), '
    'so the Bloom/Glare oracle is derived from the C++ mapping instead of an executable '
    'Blender property.',
    'GOO 4.4 means the Goo fork file DNA (check_shadow_id + light_group_shadow_bits). '
    'Upstream Blender 4.4 files lack those fields and are covered by the official rows.',
    'When --blender is supplied the probe verifies the target 5.2+ RNA surface only; it '
    'cannot replay a 4.x file load inside the current binary.',
]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def normalise(value):
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, list):
        return [normalise(item) for item in value]
    if isinstance(value, dict):
        return {key: normalise(item) for key, item in value.items()}
    return value


def citation_problems():
    problems = []
    for rule in RULES:
        path = REPO_ROOT / rule['file']
        if not path.is_file():
            problems.append('{}: cited file missing: {}'.format(rule['id'], rule['file']))
            continue
        try:
            line_count = len(path.read_text(encoding='utf-8', errors='replace').splitlines())
        except OSError as exc:
            problems.append('{}: cannot read {}: {}'.format(rule['id'], rule['file'], exc))
            continue
        start, end = rule['lines']
        if start < 1 or end < start or end > line_count:
            problems.append('{}: line range {}..{} outside {} ({} lines)'.format(
                rule['id'], start, end, rule['file'], line_count))
    return problems


def validate_manifest(report, manifest):
    cases = manifest['cases']
    util.add_check(report, 'manifest_schema', manifest.get('schema') == MANIFEST_SCHEMA,
                   'schema={}'.format(manifest.get('schema')))

    ids = [case['id'] for case in cases]
    expected_ids = {'official_4_1', 'official_4_2', 'official_5_0', 'official_5_1',
                    'official_5_2_1', 'goo_4_4'}
    util.add_check(report, 'case_ids', set(ids) == expected_ids and len(ids) == len(set(ids)),
                   'cases={}'.format(sorted(ids)))

    version_ok = True
    version_detail = []
    for case in cases:
        version = case['file_version']
        computed = version['major'] * 100 + version['minor']
        if computed != version['version_file']:
            version_ok = False
            version_detail.append('{}: version_file {} != major*100+minor {}'.format(
                case['id'], version['version_file'], computed))
        if case['id'] == 'official_5_2_1':
            if version['subversionfile'] != TARGET_FILE_SUBVERSION:
                version_ok = False
                version_detail.append('official_5_2_1 must use subversion {}'.format(
                    TARGET_FILE_SUBVERSION))
        elif version['subversionfile'] != 0:
            version_ok = False
            version_detail.append('{}: expected synthetic subversion 0'.format(case['id']))
    util.add_check(report, 'version_encoding', version_ok,
                   '; '.join(version_detail) or 'versionfile=major*100+minor; '
                   '5.2.1 uses subversion 48')

    for case in cases:
        computed = compute_expected(case)
        declared = case['expected']
        same = normalise(computed) == normalise(declared)
        detail = ''
        if not same:
            detail = json.dumps({'computed': normalise(computed),
                                 'declared': normalise(declared)}, sort_keys=True)
        util.add_check(report, 'rule_consistency:{}'.format(case['id']), same, detail)

        unknown = [rule for rule in case['rules'] if rule not in RULE_IDS]
        util.add_check(report, 'rule_ids:{}'.format(case['id']), not unknown,
                       'unknown rule ids: {}'.format(unknown))
        util.add_check(report, 'no_fake_blend:{}'.format(case['id']),
                       case.get('blend_binary') is None,
                       'blend_binary must stay null in descriptor mode')

    glare = GLARE_DEFAULT
    size_ok = abs(glare['size'] - 6.5 / 8.5) < 1e-12
    chain_ok = glare['chain_length'] == 9
    strength_ok = abs(glare['strength'] - 0.45) < 1e-9
    util.add_check(report, 'bloom_glare_math', size_ok and chain_ok and strength_ok,
                   'size={} chain={} strength={} for {}x{}'.format(
                       glare['size'], glare['chain_length'], glare['strength'],
                       BLOOM_RESOLUTION[0], BLOOM_RESOLUTION[1]))
    util.add_check(report, 'bloom_glare_fields',
                   glare['type'] == 'CMP_NODE_GLARE_BLOOM'
                   and glare['quality'] == 'HIGH'
                   and glare['clamp'] is False
                   and glare['maximum'] == 0.0
                   and glare['tint'] == [1.0, 1.0, 1.0, 1.0],
                   'glare constants come from DNA defaults')

    problems = citation_problems()
    util.add_check(report, 'source_citations', not problems, '; '.join(problems) or
                   '{} citations verified'.format(len(RULES)))

    manifest['validation'] = {
        'checks': [{'name': check['name'], 'status': check['status']}
                   for check in report['checks']],
        'descriptor_checks_passed': not any(check['status'] == 'fail'
                                            for check in report['checks']),
    }


# ---------------------------------------------------------------------------
# Optional Blender probe
# ---------------------------------------------------------------------------

PROBE_CODE = r'''
import bpy
import json
import os

out = os.environ["GOO_MIGRATION_PROBE_OUT"]


def prop_exists(owner, name):
    if owner is None:
        return False
    try:
        return name in owner.bl_rna.properties
    except Exception:
        return False


def enum_ids(owner, name):
    try:
        return [item.identifier for item in owner.bl_rna.properties[name].enum_items]
    except Exception:
        return []


shader_info = getattr(bpy.types, "ShaderNodeShaderInfo", None)
data = {
    "blender_version": bpy.app.version_string,
    "blender_version_file": bpy.app.version[0] * 100 + bpy.app.version[1],
    "blender_file_subversion": bpy.app.version[0] * 100000 + bpy.app.version[1] * 1000
    + bpy.app.version[2],
    "shader_node_shader_info_exists": shader_info is not None,
    "properties": {
        "Material.light_group_bits": prop_exists(bpy.types.Material, "light_group_bits"),
        "Material.light_group_shadow_bits": prop_exists(bpy.types.Material, "light_group_shadow_bits"),
        "Material.surface_render_method": prop_exists(bpy.types.Material, "surface_render_method"),
        "Material.blend_method": prop_exists(bpy.types.Material, "blend_method"),
        "Light.light_group_bits": prop_exists(bpy.types.Light, "light_group_bits"),
        "ShaderNodeShaderInfo.light_group_bits": prop_exists(shader_info, "light_group_bits"),
        "ShaderNodeShaderInfo.light_group_shadow_bits": prop_exists(shader_info, "light_group_shadow_bits"),
        "ShaderNodeShaderInfo.use_own_light_groups": prop_exists(shader_info, "use_own_light_groups"),
    },
    "surface_methods": enum_ids(bpy.types.Material, "surface_render_method"),
    "blend_methods": enum_ids(bpy.types.Material, "blend_method"),
}
with open(out, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2, sort_keys=True)
print("GOO_MIGRATION_PROBE_DONE")
'''


def run_blender_probe(report, blender_path, probe_path, timeout=180.0):
    import os
    env = dict(os.environ)
    env['GOO_MIGRATION_PROBE_OUT'] = str(probe_path)
    command = [
        str(blender_path),
        '--background',
        '--factory-startup',
        '--python-exit-code',
        '1',
        '--python-expr',
        PROBE_CODE,
    ]
    report['commands'].append(command)
    run = util.run_command(command, timeout=timeout,
                           log_path=probe_path.with_name('probe.log'), env=env)
    data = util.read_json(probe_path)
    util.add_artifact(report, 'blender_probe', probe_path)
    util.add_artifact(report, 'log', run['log'])
    if not isinstance(data, dict):
        util.add_check(report, 'blender_probe_json', False,
                       'no probe JSON (exit {})'.format(run['returncode']))
        return None
    util.add_check(report, 'blender_probe_json', run['returncode'] == 0,
                   'exit {}'.format(run['returncode']))
    properties = data.get('properties') or {}
    missing = sorted(name for name, present in properties.items() if not present)
    util.add_check(report, 'blender_api_surface', not missing,
                   'missing RNA properties: {}'.format(missing) if missing else
                   'all required RNA properties exist')
    util.add_check(report, 'blender_shader_info_node',
                   bool(data.get('shader_node_shader_info_exists')),
                   'ShaderNodeShaderInfo type present')
    util.add_check(report, 'blender_opaque_enum',
                   'OPAQUE' in (data.get('blend_methods') or []),
                   'blend_method identifiers={}'.format(data.get('blend_methods')))
    util.add_check(report, 'blender_surface_enums',
                   {'DITHERED', 'BLENDED'}.issubset(set(data.get('surface_methods') or [])),
                   'surface identifiers={}'.format(data.get('surface_methods')))
    return data


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True,
                        help='directory for manifest.json and result.json')
    parser.add_argument('--manifest', type=Path,
                        help='explicit manifest path (default: <output-dir>/manifest.json)')
    parser.add_argument('--descriptor-only', action='store_true',
                        help='validate the manifest without probing a built Blender')
    parser.add_argument('--blender', type=Path,
                        help='Blender binary used for the API-surface probe')
    return parser.parse_args()


def write_manifest(path, manifest):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    util.write_json_atomic(Path(path), manifest)


def main():
    args = arguments()
    output_dir = Path(args.output_dir)
    try:
        test_root, _ = util.prepare_work_dir(output_dir.parent, output_dir.name)
    except (OSError, ValueError, RuntimeError) as exc:
        print('GOO_TEST_FAIL: cannot prepare output directory: {}'.format(exc), file=sys.stderr)
        return 1
    manifest_path = Path(args.manifest) if args.manifest else test_root / 'manifest.json'
    result_path = test_root / 'result.json'

    test_name = 'goo_migration_manifest' if args.descriptor_only else 'goo_migration_fixtures'
    report = util.make_report(test_name)
    report['commands'] = []
    report['summary'] = {}

    manifest = {
        'schema': MANIFEST_SCHEMA,
        'generated_utc': util.utc_now(),
        'mode': 'descriptor-only' if args.descriptor_only else 'blender-probe',
        'target': {
            'blender_version': '5.2',
            'version_file': TARGET_VERSION_FILE,
            'file_subversion': TARGET_FILE_SUBVERSION,
            'engine': 'BLENDER_EEVEE',
        },
        'fixture_policy': {
            'binaries': 'none; descriptor-level manifest only',
            'covered_versions': ['official 4.1', 'official 4.2', 'official 5.0',
                                 'official 5.1', 'official 5.2.1', 'GOO 4.4'],
            'authoring_note': 'real fixtures must be authored by the corresponding Blender '
                              'build and checked in as .blend files; this manifest pins '
                              'the expected post-load state for that work.',
        },
        'rules': RULES,
        'cases': build_cases(),
        'limitations': LIMITATIONS,
        'blender_probe': None,
        'validation': None,
    }
    report['cases'] = [
        {'id': case['id'], 'label': case['label'],
         'expected_rna_blend_method': case['expected']['rna_blend_method'],
         'expected_bloom_migrated': case['expected']['bloom']['migrated']}
        for case in manifest['cases']
    ]

    validate_manifest(report, manifest)

    if args.descriptor_only:
        write_manifest(manifest_path, manifest)
        util.add_artifact(report, 'manifest', manifest_path)
        util.add_limitation(report, LIMITATIONS[0])
        util.add_limitation(report, LIMITATIONS[1])
        util.add_limitation(report, LIMITATIONS[2])
        report['summary'] = {'mode': 'descriptor-only', 'manifest': str(manifest_path),
                             'cases': len(manifest['cases']), 'rendered': False}
        return util.finish(report, result_path)

    if args.blender is None:
        write_manifest(manifest_path, manifest)
        util.add_artifact(report, 'manifest', manifest_path)
        return util.finish_skip(
            report, result_path,
            '--blender not supplied; descriptor manifest validated but no Blender probe '
            'and no migration was executed')
    blender = util.blender_version(args.blender)
    report['blender'] = {key: blender.get(key) for key in
                         ('binary', 'available', 'version_text', 'version_file', 'build_hash', 'error')}
    util.add_check(report, 'blender_available', blender['available'],
                   blender.get('error') or blender.get('version_text') or '')
    if not blender['available']:
        write_manifest(manifest_path, manifest)
        util.add_artifact(report, 'manifest', manifest_path)
        return util.finish_skip(report, result_path,
                                'no usable Blender at {}: {}'.format(args.blender,
                                                                     blender.get('error')))
    if (blender.get('version_file') or 0) < TARGET_VERSION_FILE:
        write_manifest(manifest_path, manifest)
        util.add_artifact(report, 'manifest', manifest_path)
        return util.finish_skip(
            report, result_path,
            'migration rules target the 5.2 loader; found {}'.format(
                blender.get('version_text')))
    util.add_check(report, 'blender_target_version',
                   (blender.get('version_file') or 0) == TARGET_VERSION_FILE,
                   'expected version_file {} but found {} ({})'.format(
                       TARGET_VERSION_FILE, blender.get('version_file'),
                       blender.get('version_text')))

    probe_path = test_root / 'blender_probe.json'
    probe_data = run_blender_probe(report, args.blender, probe_path)
    manifest['blender_probe'] = probe_data
    manifest['validation'] = {
        'checks': [{'name': check['name'], 'status': check['status']}
                   for check in report['checks']],
        'descriptor_checks_passed': not any(
            check['status'] == 'fail' and not check['name'].startswith('blender_')
            for check in report['checks']),
    }
    write_manifest(manifest_path, manifest)
    util.add_artifact(report, 'manifest', manifest_path)
    util.add_limitation(report, LIMITATIONS[0])
    util.add_limitation(report, LIMITATIONS[4])
    report['summary'] = {
        'mode': 'blender-probe',
        'manifest': str(manifest_path),
        'cases': len(manifest['cases']),
        'probe': probe_data.get('blender_version') if isinstance(probe_data, dict) else None,
        'rendered': False,
    }
    return util.finish(report, result_path)


if __name__ == '__main__':
    sys.exit(main())
