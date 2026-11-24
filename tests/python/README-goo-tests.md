# Goo EEVEE production test gate

These tests exercise the Goo-specific EEVEE features that upstream Blender does
not cover:

* material/light `light_groups` (bitsets, icons, shadow ignoring),
* legacy EEVEE `OPAQUE` provenance (`MA_LEGACY_OPAQUE`), the deprecated
  `Material.blend_method` RNA view and the 4.1/4.2/5.x migration rules,
* legacy EEVEE Bloom -> Compositor Glare conversion,
* screenspace refraction/probe/AOV paths,
* golden-scene pixel smoke checks (when scene files and a GPU are available).

Everything here is standard-library Python plus Blender's own Python. No third
party dependency is required.

## Files

| File | Purpose |
| --- | --- |
| `goo_test_util.py` | Host helpers: `--version` parsing, GPU probe invocation, fresh output dirs, subprocess logs, result JSON and the `0/1/77` exit-code contract. |
| `goo_gpu_probe.py` | Runs inside Blender: performs an 8x8 EEVEE render and reports `available` only if real, non-zero pixels were produced. |
| `goo_ctest_runner.py` | Host wrapper for the four existing Goo entries; prepares a clean `--output` dir, probes the GPU, runs the entry point and writes `result.json`. |
| `goo_migration_fixtures.py` | Descriptor-level migration manifest (official 4.1/4.2/5.0/5.1/5.2.1 + GOO 4.4) and optional 5.2+ RNA probe. |
| `goo_golden_smoke.py` | Golden-scene spec (`--spec`) and render/compare runner (`--run`). |
| `goo_golden_render.py` | Runs inside Blender: renders one golden `.blend` to linear EXR, hashes the RGBA pixels and reports luminance statistics. |
| `README-goo-tests.md` | This document. |

The pre-existing entry points are `goo_light_groups.py` (host matrix runner),
`goo_bsdf_light_groups.py`, `goo_screenspace.py` (both must run inside Blender)
and `goo_bsdf_light_groups_cases.py` (a library; its cases are reached through
`goo_bsdf_light_groups.py --case <name>`).

## Registering / running

The tests are registered in `tests/python/CMakeLists.txt` and only exist when
the Python tests are enabled, i.e. `WITH_BLENDER=ON`, `WITH_PYTHON=ON` and
`WITH_PYTHON_MODULE=OFF`. They run against the installed/built Blender at
`TEST_BLENDER_EXE` and require the Goo startup module at
`<source>/scripts/startup/goo_engine_light_groups.py`.

```sh
# configure once (inside the build dir)
cmake -DWITH_BLENDER=ON -DWITH_PYTHON=ON -DWITH_TESTS=ON ..

# all Goo entries
ctest -L goo --output-on-failure

# only the fast manifest checks
ctest -L "goo" -LE "render|slow"

# one entry, verbose
ctest -R '^goo_bsdf_light_groups$' -V

# list what CTest would run
ctest -N -L goo
```

| CTest name | Labels | TIMEOUT | Exit 77 when |
| --- | --- | --- | --- |
| `goo_light_groups` | `goo;render;slow` | 1800 | no Blender or no headless GPU |
| `goo_bsdf_light_groups` | `goo;render;slow` | 1800 | no Blender or no headless GPU |
| `goo_bsdf_light_groups_cases` | `goo;render;slow` | 1800 | no Blender or no headless GPU |
| `goo_screenspace` | `goo;render;slow` | 1800 | no Blender or no headless GPU |
| `goo_migration_manifest` | `goo` | 120 | never (descriptor-only, must pass) |
| `goo_migration_fixtures` | `goo;slow` | 900 | no built Blender / Blender < 5.2 |
| `goo_golden_manifest` | `goo` | 120 | never (spec-only, must pass) |
| `goo_golden_smoke` | `goo;render;slow` | 1800 | no Blender, no GPU, or no golden scenes |

`SKIP_RETURN_CODE 77` is set for the entries that can be skipped. CTest reports
those as **Not Run**, not as **Passed**. A skipped test must never be counted as
coverage: the JSON result records a non-empty `skip_reason` for exactly this
reason.

Output directories are created below `${CMAKE_BINARY_DIR}/tests/goo/<name>/`.
`work/` is deleted and recreated on every run (the Goo scripts require a
non-existing or empty `--output`), while `result.json` is atomically replaced.

## What a skip means

The rendering entries need a real headless EEVEE context. A backend name
(`EGL`, `Metal`, `Vulkan`, ...) is not enough: `goo_gpu_probe.py` renders 8x8
pixels and requires a non-zero frame. Therefore these conditions skip instead of
failing:

* `TEST_BLENDER_EXE` is missing (Blender was configured but not installed),
* Blender starts but cannot create a GPU context (typical on headless Linux
  without EGL, on VMs without GPU passthrough, or on remote sessions without a
  logged-in desktop on Windows/macOS),
* `--version` is unreadable or returns a non-zero exit code,
* for the golden runner: the golden scene directory is empty or absent.

A test that reaches the actual render never skips because of a black frame,
missing output file, bad args or a failing assertion: those are failures.

## Result JSON

Every entry writes `<build>/tests/goo/<name>/result.json` and prints one of:

```
GOO_TEST_PASS: goo_screenspace
GOO_TEST_FAIL: goo_screenspace
GOO_TEST_SKIP: goo_screenspace
GOO_TEST_SKIP_REASON: GPU/headless EEVEE unavailable: ...
GOO_TEST_RESULT: /path/tests/goo/goo_screenspace/result.json
```

Schema (see `goo_test_util.RESULT_SCHEMA`, currently `goo-test-result/v1`):

| Field | Meaning |
| --- | --- |
| `schema_version`, `test` | Result schema and CTest-facing entry name. |
| `status` | `pass`, `fail` or `skip`. Matches the process exit code `0/1/77`. |
| `skip_reason` | Human readable reason; non-null iff `status == "skip"`. |
| `started_utc`, `ended_utc`, `duration_seconds` | Timing. |
| `argv`, `cwd` | Exact invocation, for reproduction. |
| `host` | Platform, Python version/executable. |
| `blender` | `--version` parse: `version_text`, `version_file` (`major*100+minor`), `build_hash`. |
| `gpu` | Normalised probe answer: `available`, `reason`, `backend`, `renderer`, `vendor`, `device_type`. |
| `checks[]` | `{name, status: pass/fail, detail}`. A single failed check fails the test. |
| `artifacts[]` | `{kind, path}` for manifests, logs, EXRs, per-script `result.json`. |
| `limitations[]` | Coverage that was *not* verified, e.g. descriptor-only migration. |
| `notes[]` | Informational messages (e.g. smoke-only golden scene). |
| `commands[]` | Blender/host commands that were executed, including `--` arguments. |
| `summary` | Entry-specific summary (`host-matrix`, `blender`, `spec`, `run`, `descriptor-only`, `blender-probe`). |

The per-script `result.json` files written by the original Goo scripts are kept
as artifacts; for `goo_bsdf_light_groups.py` the `checks` dictionary is the
authoritative source of its pass/fail.

## Migration fixtures (`goo_migration_fixtures.py`)

This test is intentionally **descriptor-level**: it does not fabricate `.blend`
binaries. Fabricating DNA without the real writer would create files that no
Blender build could open, and a "pass" based on them would be meaningless.

Each row pins `file_version`, the Goo DNA markers, the raw blend method, the
legacy Bloom block and the expected post-load state:

| Row | Legacy OPAQUE | Legacy Bloom | light_group bits |
| --- | --- | --- | --- |
| official 4.1 (`401/0`) | flag reconstructed -> RNA `OPAQUE` | migrated to Glare | missing -> `(0,0,0,1)` |
| official 4.2 (`402/0`) | no legacy era -> RNA `HASHED` | migrated to Glare | missing -> `(0,0,0,1)` |
| official 5.0 (`500/0`) | RNA `HASHED` | version >= 500: not migrated, bit untouched | missing -> `(0,0,0,1)` |
| official 5.1 (`501/0`) | RNA `HASHED` | not migrated | missing -> `(0,0,0,1)` |
| official 5.2.1 (`502/48`) | provenance block skipped, preserved | call site skipped | missing -> `(0,0,0,1)` |
| GOO 4.4 (`404/0`) | `goo_file_dna` -> RNA `OPAQUE` | migrated to Glare | authored `[2,0,0,0]` / shadow `[0,0,0,0]` |

The expected state is recomputed from a host-side model of the cited rules and
compared with the hand-written expectation in each case. Source citations
(file + line range) are validated against the checkout, so a moved code block
fails the manifest test instead of silently drifting.

Run it in two modes:

```sh
# fast, no Blender: validates and writes the manifest, exits 0
python tests/python/goo_migration_fixtures.py \
    --descriptor-only --output-dir build/tests/goo/migration_manifest

# with a built 5.2+: additionally probes the RNA surface; skips without a build
python tests/python/goo_migration_fixtures.py \
    --blender /path/to/blender --output-dir build/tests/goo/migration_fixtures
```

`--blender` only verifies that the target build exposes the expected RNA
(Material/Light/ShaderNodeShaderInfo light-group properties, `OPAQUE`,
`DITHERED`/`BLENDED`). It cannot replay a 4.x file load, and the script says so
in `limitations`. Real `.blend` fixtures, when added later, belong under
`tests/files/goo_migration/` and should be produced by the corresponding
Blender build.

## Golden scenes (`goo_golden_smoke.py`)

`goo_golden_manifest` (`--spec`) is deterministic and never renders. It writes
`spec.json` describing the contract: scene ids, EEVEE engine, resolution,
required checks, reference policy and result fields. It records whether scene
files are present without claiming a pass.

`goo_golden_smoke` (`--run --blender ... --golden-dir ...`) renders every scene
and compares:

* `engine` contains `EEVEE`, resolution matches, image is finite and
  **non-degenerate** (mean/std/max luminance above zero thresholds),
* when a reference `pixel_sha256` exists (manifest field or sidecar
  `<scene-id>.reference.json`), the hash must match exactly,
* without a reference the scene is marked `smoke_only` and the whole result
  lists it under `summary.smoke_only` plus a `limitations` entry. It is never
  reported as a golden hash comparison.

Golden directory layout:

```
tests/files/goo_golden/
  manifest.json                       # optional; else every *.blend is used
  goo_light_groups_reopen.blend
  goo_light_groups_reopen.reference.json
```

`manifest.json`:

```json
{
  "scenes": [
    {
      "id": "goo_light_groups_reopen",
      "blend": "goo_light_groups_reopen.blend",
      "resolution": 64,
      "samples": 16,
      "reference": {"pixel_sha256": "<hex>", "format": "pixel_sha256"},
      "required": true
    }
  ]
}
```

No scene files are shipped in-tree, so `goo_golden_smoke` skips until someone
authors them. Pixel hashes depend on renderer/driver/platform; keep one
reference set per platform (or per GPU class) and document the hardware next to
the hash, otherwise the comparison will be flaky.

## Manual invocation

The host scripts are plain CPython; inside Blender the scripts expect args after
`--`:

```sh
# host-side wrapper (equivalent to what CTest does)
python tests/python/goo_ctest_runner.py \
    --test screenspace \
    --blender /path/to/blender \
    --startup /path/to/source/scripts/startup \
    --output-root /tmp/goo-tests

# direct in-Blender invocation of a script that requires a fresh --output
/path/to/blender --background --factory-startup --python-exit-code 1 \
    --python tests/python/goo_screenspace.py -- \
    --output /tmp/goo-screenspace
```

`goo_ctest_runner.py` supports `--test light-groups|bsdf-light-groups|bsdf-light-groups-cases|screenspace`.
The last two also accept `--cases a,b,c` to override their default case list.

## Verification status

The host-side code is exercised with `python -m py_compile` and, for the
descriptor/golden-spec entries, with a plain host Python run; those paths need
no GPU. The rendering entries are only meaningful on CI with an installed
Blender 5.2+ and a headless GPU. Do not record a pass for entries that printed
`GOO_TEST_SKIP` — inspect `skip_reason` instead.
