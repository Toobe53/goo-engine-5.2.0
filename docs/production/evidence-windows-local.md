# Windows 本地构建与运行时证据（2026-11-25）

本文件记录 `production/5.2` 在本地 Windows 主机上完成的可复现构建、干净安装、EEVEE
渲染、CTest 门禁和驱动矩阵结果。构建与测试均在本机执行，没有使用 GitHub hosted
runner；macOS 已从当前交付范围移除。

## 主机与工具链

| 项目 | 值 |
| --- | --- |
| 主机 | `sb`，Windows 11 `10.0.26200` x64 |
| CPU | Intel Core i5-12500H |
| GPU / 驱动 | Intel Iris Xe Graphics / `31.0.101.5590`（另有 Oray 虚拟显示适配器，已忽略） |
| 工具链 | VS 2022 Build Tools 17.14.45，MSVC `19.44.35230` |
| CMake / Ninja | VS 自带 CMake 3.31.6 / Ninja 1.12.1 |
| 源码 | `production/5.2`，基线 `f3bfd1b8` |
| 预编译库 | `lib-windows_x64`，分支 `blender-v5.2-release`，checkout 6.39 GB |

`build_files/cmake/platform/platform_win32.cmake` 保持官方最低 MSVC 要求
`19.44.35216`；本机 `19.44.35230` 满足要求，没有提交任何编译器版本降级补丁。

## 构建与安装

```powershell
cmake -S . -B build-windows-lite -G Ninja `
  -C build_files/cmake/config/blender_lite.cmake `
  -DCMAKE_BUILD_TYPE=Release `
  -DWITH_CYCLES=OFF -DWITH_CYCLES_DEVICE_ONEAPI=OFF `
  -DWITH_CYCLES_ONEAPI_BINARIES=OFF `
  -DWITH_MANIFOLD=OFF -DWITH_USD=OFF -DWITH_ALEMBIC=OFF `
  -DWITH_OPENVDB=OFF -DWITH_MATERIALX=OFF -DWITH_XR_OPENXR=OFF `
  -DWITH_INPUT_IME=ON -DWITH_BUILDINFO=OFF
ninja -C build-windows-lite -j 8 blender blender-launcher blender_cpu_check
cmake --install build-windows-lite --prefix _install-windows2 --config Release
```

结果：

- `7050/7050` 目标构建完成，无 `FAILED`。
- `build-windows-lite/bin/blender.exe` 为 76,098,560 B。
- 干净安装到 `_install-windows2` 返回 `INSTALL_EXIT=0`；目录包含
  `blender.exe`、`blender-launcher.exe`、`blender_cpu_check.dll`、
  `blender.crt`、`blender.shared`、`5.2/datafiles` 和启动脚本。
- 安装后的 `blender.exe --version` 输出 `Blender 5.2.1 LTS`，退出码 `0`。

安装规则注意：只构建 `blender` 目标时 `cmake --install` 会因缺少
`blender-launcher.exe` 失败；生产构建必须同时构建 `blender-launcher`（以及
`blender_cpu_check`）。补齐后安装不需要任何手工复制 DLL 或 manifest。

首次 full-release 构建曾因 `WITH_CYCLES_DEVICE_ONEAPI=ON` 调用
`lib/windows_x64/dpcpp/bin/clang++.exe` 失败；该目录的 LFS 对象未下载，且本地 lite
构建不需要 OneAPI。因此生产本地配置显式关闭 Cycles/OneAPI。

## EEVEE 与 Goo 节点

使用干净安装 `_install-windows2/blender.exe` 后台执行：

- 13 个 Goo 节点全部可创建：`ShaderNodeLightInfo`、`ShaderNodeCurvature`、
  `ShaderNodeScreenspaceInfo`、`ShaderNodeSdfNoise`、`ShaderNodeSdfOp`、
  `ShaderNodeSdfPrimitive`、`ShaderNodeSdfVectorOp`、`ShaderNodeSetDepth`、
  `ShaderNodeShaderInfo`、`ShaderNodeTexHexagon`、`ShaderNodeTwirl`、
  `ShaderNodeWaterRipples`、`ShaderNodeOKLabColorRamp`；`GOO_NODES_OK 13`。
- EEVEE 64×64 渲染成功，PNG 3,934 B。
- `material.check_shadow_id = True` 后再次渲染成功，PNG 3,934 B，退出码 `0`。

## CTest 门禁

```powershell
ctest --test-dir build-windows-lite -L goo --output-on-failure -j 1
```

| 测试 | 结果 | 耗时 |
| --- | --- | --- |
| `goo_light_groups` | Passed | 115.35 s |
| `goo_bsdf_light_groups` | Passed | 24.09 s |
| `goo_bsdf_light_groups_cases` | Passed | 32.66 s |
| `goo_screenspace` | Passed | 5.42 s |
| `goo_migration_manifest` | Passed | 0.25 s |
| `goo_migration_fixtures` | Passed | 0.79 s |
| `goo_golden_manifest` | Passed | 0.11 s |
| `goo_golden_smoke` | Skipped（77） | — |

`100% tests passed, 0 failed out of 8`，总耗时 181.36 s。`goo_golden_smoke`
的 77 是测试自身的 skip 约定，不是通过；黄金渲染仍需真实场景资产。

另外用干净安装的 `_install-windows2/blender.exe` 运行
`goo_ctest_runner.py --test screenspace`，结果为 `GOO_TEST_PASS: goo_screenspace`。

## 驱动矩阵

矩阵新增 `debian-13-nvidia-gtx1660` 行，与 `win11-intel` 行一起在本机执行。Windows
结果：

| 条目 | 测试集 | 后端 | 状态 |
| --- | --- | --- | --- |
| `win11-intel` | `smoke-opengl` | opengl | passed |
| `win11-intel` | `smoke-vulkan` | vulkan | passed |
| `win11-intel` | `render-eevee` | opengl | passed |
| `win11-intel` | `render-eevee` | vulkan | passed |

`verdict: PASS`，4/4 通过。Linux 的同一组 4 个测试也全部通过，见
[evidence-linux-local.md](evidence-linux-local.md)。

矩阵工具有两个真实缺陷已修复：

- `smoke/gpu_probe.py` 在渲染前查询 `gpu.platform`，后台模式下 GPU 模块尚未初始化，
  会把 Vulkan 误报为 OpenGL；现在先渲染、再查询实际后端。
- `detect.py` 只检查 `C:\Windows\System32\vulkan-1.dll`；Blender 自带的 loader 位于
  安装目录。现在检测器也会检查二进制旁边的 `vulkan-1.dll`，并把 smoke probe 作为
  设备可用性的权威判定。

原始报告、probe JSON 和运行日志保存在
[`evidence/driver-matrix-windows-local/`](evidence/driver-matrix-windows-local/)
与 [`evidence/driver-matrix-linux-local/`](evidence/driver-matrix-linux-local/)。

## 仍未验证

- Windows/Linux 15 行中只有 `win11-intel` 与 `debian-13-nvidia-gtx1660` 两行有真实
  硬件结果；其余仍是 descriptor，不能声称“完整矩阵已覆盖”。
- 真实崩溃 dump 的 `crashmon run` supervisor 采集与符号化；Windows 代码签名；安装包、自动更新与回滚。
- Cycles、USD、Alembic、OpenVDB、MaterialX、XR 在当前 lite 构建中关闭，未做功能
  回归。
- macOS 按用户要求不再纳入验收。

在这些项完成前，本分支只能称为“Windows/Linux 本地可构建、可测试的生产候选”，
不能称为完整稳定生产版。
