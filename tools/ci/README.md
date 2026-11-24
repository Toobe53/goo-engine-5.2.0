# CI and build probes for the GOO Engine production branch

This directory contains the **source-build CI skeleton** and local prerequisite
probes for the GOO Engine 5.2 LTS branch (`production/5.2`).

| File | Purpose |
| --- | --- |
| `../../.github/workflows/production-build.yml` | GitHub Actions workflow: Windows/Linux release build matrix (macOS out of scope) |
| `validate_workflow.py` | Stdlib-only static checker for the workflow (triggers, matrix, gating, caches, uploads) |
| `build_probe.py` | Stdlib-only JSON probe of local build prerequisites; never runs a real build |
| `local_gate.py` | Local release gate: compile, tests, workflow/matrix validation, bundle verification |
| `README.md` | This document |

> **Honesty note.** The workflow has run on GitHub-hosted runners: the Linux and
> Windows `Build release` jobs passed and produced artifacts. macOS is out of
> scope for this delivery, so the workflow now targets Windows/Linux only.
> Hosted CI is therefore **not** the acceptance gate here; use
> `tools/ci/local_gate.py` plus the local build/test evidence. Runner-image drift
> (apt package names, MSVC version) remains the most likely source of CI failure.

## Local usage

Use any Python >= 3.8; no third-party packages are required.

```bash
# 1. Syntax check
python -m py_compile tools/ci/build_probe.py tools/ci/validate_workflow.py

# 2. Can this machine build GOO Engine at all?
python tools/ci/build_probe.py --text
# JSON (default) for machine consumption:
python tools/ci/build_probe.py > probe.json

# 3. Is the workflow structurally sound?
python tools/ci/validate_workflow.py            # exit 0 = all checks passed
python tools/ci/validate_workflow.py --json     # machine-readable report

# 4. Local release gate (7 stages; requires dist/goo-engine-5.2.1)
python tools/ci/local_gate.py --release-dir dist/goo-engine-5.2.1
# add the Goo CTest suite on a machine with build-windows-lite
python tools/ci/local_gate.py --release-dir dist/goo-engine-5.2.1 --with-ctest
```

`build_probe.py` exits `0` only when every **required** prerequisite passes:
CMake >= 3.21, Ninja, Git, Git LFS, GNU make (Unix), a C/C++ compiler meeting
Blender's minimums (GCC >= 14, Clang >= 17, MSVC >= 19.44.35216), >= 25 GiB
free disk space (configurable with `--disk-gb`), and the expected repository
layout. Exit code `1` means at least one required check failed; `2` means the
`--repo-root` does not look like a Blender/GOO checkout.

Precompiled libraries under `lib/<platform>_<arch>` are a **warning** by
default (a fresh checkout has none until `make update`), use `--require-libs`
to make them fatal. `--skip-libs` skips that check entirely.

`validate_workflow.py` needs no arguments: it defaults to
`.github/workflows/production-build.yml` relative to the repository root. It
exits `1` on a failed check and `2` when the file is missing or cannot be
parsed by its built-in YAML subset parser.

If PyYAML happens to be installed, an extra sanity check is:

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/production-build.yml')); print('yaml ok')"
```

## Workflow behaviour

### Triggers

* `workflow_dispatch` — manual runs (choose the branch in the UI).
* `push` to `production/**`.
* `pull_request` with the `ci:build` label. The workflow listens to
  `opened`, `synchronize`, `reopened` and `labeled`, and the job-level gate is
  `github.event_name != 'pull_request' || contains(github.event.pull_request.labels.*.name, 'ci:build')`.
  A PR without the label is skipped (neutral), not failed; adding the label
  starts a run. Push and manual runs are never label-gated.

### Matrix

One job, `strategy.fail-fast: false`, 180-minute job timeout:

| Name | Runner | Precompiled libs | Build directory | Uploaded artifact |
| --- | --- | --- | --- | --- |
| `windows-x64` | `windows-2022` | `lib/windows_x64` | `build-windows-x64` | `goo-engine-windows-x64` |
| `linux-x64` | `ubuntu-24.04` | `lib/linux_x64` | `build-linux-x64` | `goo-engine-linux-x64` |

The build tree is exactly `matrix.build_dir`: passing `BUILD_DIR=...` on the
`make` command line suppresses the `GNUmakefile` `_release` suffix. macOS is not
part of the matrix.

### Steps

1. `actions/checkout@v4` with `fetch-depth: 1`, `submodules: false`,
   `lfs: false`. The branch's own sources are authoritative; submodules are
   fetched by the update step (and the repo's LFS files are not needed to
   compile — verified in this tree: zero LFS pointer files outside git's
   metadata).
2. `actions/setup-python@v5` (3.12) — `make_update.py` and this tooling.
3. Platform dependencies: Ubuntu apt packages (the list from
   `build_files/build_environment/install_linux_packages.py`, plus
   `gcc-14`/`g++-14` because 24.04's GCC 13 is below Blender's minimum);
   macOS `brew install cmake ninja ccache git-lfs` and an Xcode >= 16 check;
   Windows verification of `cmake`, `ninja`, `git`, `git-lfs` and a vswhere
   lookup that enforces MSVC >= 19.44.35216 before anything expensive starts.
4. Resolve the pinned precompiled-library revision
   (`git ls-tree HEAD lib/<libdir>`, with a `nogitlink` fallback) and restore
   caches.
5. Update: `python build_files/utils/make_update.py --no-blender
   --no-lfs-fallback --git-command git`. This is the non-interactive
   equivalent of `make update`: `make.bat update` prompts on Windows when
   `lib/<libdir>` is absent, so it cannot be used from CI. `--no-blender`
   skips pulling the main repository (the checkout is already the right
   commit); `--no-lfs-fallback` avoids fetching an LFS fallback remote into
   the main repository.
6. Build:
   * Linux: `CC=gcc-14 CXX=g++-14 make release ninja ccache BUILD_DIR=...`
   * macOS: `make release ninja ccache BUILD_DIR=...`
   * Windows: `make.bat release ninja builddir <absolute path>`
   Both streams are captured with `tee`/`Tee-Object` into `ci-logs/build.log`
   and the step propagates the compiler's exit code; **no**
   `continue-on-error` is used on update, configure or build.
7. Collect CMake diagnostics (`CMakeOutput.log`, `CMakeError.log`,
   `CMakeConfigureLog.yaml`, `CMakeCache.txt`, `buildtime.txt`) on every
   outcome.
8. Upload `logs-<name>` (`if: always()`, `ci-logs/**` plus the CMake logs,
   retention 14 days) and `goo-engine-<name>` (`if: success()`,
   `<out_dir>/bin/**`, retention 7 days). Log upload never masks a build
   failure.

The file also documents in comments that GitHub-hosted runners have **no GPU**
and that EEVEE/Cycles render regression belongs to the self-hosted driver
matrix (see below).

## Caches

| Cache | Key | Paths |
| --- | --- | --- |
| Precompiled libs | `libs-<os>-<libdir>-<pinned lib revision>-<CI_CACHE_EPOCH>` | `lib/<libdir>`, `.git/modules/lib/<libdir>` |
| CMake configuration | `cmake-<os>-<name>-<hashFiles('CMakeLists.txt', 'build_files/cmake/**/*.cmake')>-<CI_CACHE_EPOCH>` | `<out_dir>/CMakeCache.txt` |
| ccache (Unix only) | `ccache-<os>-<name>-<github.sha>` | `.ccache` (`CCACHE_DIR`) |

* The library key uses the **submodule commit** recorded in the git index, not
  a timestamp, so bumping `lib/*` in a Blender update automatically produces a
  new key. `.gitmodules` alone is not enough (the pinned SHA is in the tree).
* `.git/modules/lib/<libdir>` is cached together with the work tree so
  `make_update.py` finds a valid submodule repository and only fetches the LFS
  objects; after a revision bump it checks out the recorded commit itself.
* Bump `CI_CACHE_EPOCH` in the workflow `env` block when the build scripts,
  dependency list or configure flags change in a way hashes do not capture.
* GitHub's cache limit is 10 GB per repository; entries are evicted after
  seven days without use, so occasional cold runs are normal.
* ccache is deliberately **not** enabled on Windows (GitHub's Windows images
  do not ship ccache; wiring sccache is possible but not part of this
  skeleton). The release profile also builds Cycles kernels, which are slow
  and mostly cacheable only through ccache/CMake caches.

## Runner requirements

* **Windows**: `windows-2022` image, Visual Studio 2022 with the v143 C++
  x64 toolset >= 17.14.14 (the workflow checks `cl.exe` FileVersion before
  configuring), CMake/Ninja/Git/Git LFS preinstalled. 4 vCPU / 16 GB RAM is
  the practical minimum.
* **Linux**: `ubuntu-24.04` (needed for `gcc-14` from the distribution),
  the apt list above, CMake >= 3.21 (image provides 3.28+), Ninja, ccache.
* **macOS**: `macos-15` (arm64) with Xcode 16+; `brew` provides CMake, Ninja,
  ccache and Git LFS. Note that GitHub's arm64 runners have ~7 GB RAM and
  three cores — linking a release Blender can be memory- and time-tight. If
  macOS artifacts are release-critical, use a larger self-hosted macOS runner
  or raise the job timeout (max 360 minutes).
* **Disk**: source ~1.5 GB, precompiled libs several GB, build tree
  ~10-30 GB (release, both Cycles and CPU objects). 25 GiB free is the probe
  default; GitHub images usually expose far more, but monitor the build log.
* **GPU**: not required. `build_files/cmake/config/blender_release.cmake`
  requests CUDA/HIP/oneAPI kernels, but missing toolchains are downgraded to
  a configure warning by `set_and_warn_library_found`, so GPU-less hosted
  runners still produce the standard release feature set without GPU kernels.

## Known limitations

* **No GPU verification.** GitHub-hosted runners have no GPU, so this
  workflow does not run EEVEE/Cycles render regression. It builds the
  application and its CPU render path only. GPU runtime coverage is the job of
  the self-hosted driver matrix workstream (its documentation lives in
  `docs/production/driver-matrix.md`; tooling under `tools/driver_matrix/`).
* **No packaging, signing or notarization.** Artifacts are the raw
  `<out_dir>/bin/**` trees. Installers and code signing stay out of this
  skeleton.
* **Hosted runs are not the acceptance gate.** Linux and Windows
  `Build release` jobs passed and uploaded artifacts. macOS is out of scope and
  has been removed from this workflow. Use `tools/ci/local_gate.py` and the local
  evidence for acceptance; treat this workflow as a reproducible build harness,
  not as the release gate.
* **Windows update path.** `make.bat update` is interactive on a fresh
  checkout, which is why the workflow calls `make_update.py` directly.
  Developers on Windows who run `make.bat update` interactively are fine;
  scripts must use the Python entry point.
* **Linux arm64 and other architectures** are not in the matrix. They can be
  added as a fourth matrix entry with their own runner label and precompiled
  library directory (`lib/linux_arm64` or a self-built `lib/` tree).
* **Cache correctness is best-effort.** Caches are advisory: a bad CMake cache
  is detected and fixed by the build, but a stale `lib/` tree from a failed
  upload can be confusing. Delete the cache in the GitHub UI and bump
  `CI_CACHE_EPOCH` if in doubt.
* **Shallow checkout.** `fetch-depth: 1` saves time and is sufficient because
  `make_update.py` runs with `--no-blender`; it does not need upstream history.
  Tools that call `git describe` or compare against `origin` may not work in
  this workflow.

## Hooking up the self-hosted GPU runner (outline)

The production build must stay GPU-free; GPU coverage should be a **separate**
workflow so a driver image failure does not block source builds.

1. Register a self-hosted runner with unambiguous labels, e.g.
   `self-hosted, linux, gpu-nvidia, x64`, and install the vendor driver plus
   CUDA toolkit before running any job.
2. Add a second workflow (e.g. `.github/workflows/gpu-regression.yml`, owned by
   the driver-matrix workstream) with `runs-on: [self-hosted, gpu-nvidia]`.
3. Download the `goo-engine-linux-x64` artifact from a successful
   `production-build` run (or build on the GPU machine itself), then run the
   driver-matrix scripts against it.
4. Keep GPU tests out of `production-build.yml`: hosted runners cannot run
   them, and a self-hosted outage should not mask a source build result.

## Validation performed

With Python 3.12 (no third-party packages in the repository):

```text
python -m py_compile tools/ci/build_probe.py tools/ci/validate_workflow.py  -> exit 0
python tools/ci/validate_workflow.py                                      -> 60 checks passed, 0 failed
python tools/ci/local_gate.py --release-dir dist/goo-engine-5.2.1          -> 7 passed, 0 failed
build_probe.py --text (this Windows host, no cmake/ninja/git)             -> exit 1, NOT READY (expected)
build_probe.py --repo-root <non-checkout>                                 -> exit 2 (expected)
```

Hosted run `45384517223` (head `9d4dce4e`) built Linux x64 and Windows x64
release artifacts successfully. That run is the source of the Windows runtime
evidence, not a release gate. In addition, during development the workflow
was parsed with PyYAML installed into a throwaway temporary directory (never
into the repo) and the built-in parser's output for every step, trigger and
matrix entry matched PyYAML exactly. Five mutations of the workflow (missing
matrix entry, failing build allow to pass, removed label gate, log upload
changed from `always()`, invalid indentation) were all rejected by
`validate_workflow.py` with non-zero exit codes.
