# Linux 本地构建与运行时证据（2026-11-25）

本文件记录 `production/5.2` 在本地 Linux 主机上完成的真实构建、EEVEE 渲染和 Goo 测试结果。所有命令均在该局域网主机上执行；没有使用 GitHub hosted runner，也没有伪造结果。

## 主机与工具链

| 项目 | 值 |
| --- | --- |
| 主机 | `matia-home`，Debian 13 trixie |
| 内核 | `6.12.113+deb13-amd64` |
| CPU / 内存 | i5-10400（6C/12T）/ 46 GiB |
| GPU / 驱动 | NVIDIA GeForce GTX 1660 6 GiB / `550.163.01` |
| 编译器 | gcc 14.2.0 |
| CMake / Ninja | 3.31.6 / 1.12.1 |
| 源码 | `f3bfd1b8`（从本地 `git archive` 传输） |
| 预编译库 | `lib-linux_x64`，分支 `blender-v5.2-release` |

## 依赖与库

- 系统包：build-essential、cmake、ninja-build、git、git-lfs、X11/GL/EGL、OpenAL、sndfile、图像库、fontconfig/harfbuzz、Vulkan/Epoxy、OpenColorIO 等。
- Linux 预编译库使用 Blender 5.2 分支，LFS checkout 后本地路径为 `~/lib-linux`；源码树中的 `lib/linux_x64` 是指向它的符号链接。
- 曾尝试完全使用 Debian 系统库配置；配置成功，但构建在 `BLI_ustring.hh` 失败：系统 OpenImageIO 2.5 使用 `OpenImageIO_v2_5` 命名空间，而 Blender 5.2 源码引用 `OpenImageIO::`。该路径未继续使用，最终构建使用 5.2 预编译库。

## 构建

```bash
cd ~/goo-work/goo-engine
ln -sfn ~/lib-linux lib/linux_x64
cmake -S . -B build-linux-libs -G Ninja \
  -C build_files/cmake/config/blender_lite.cmake \
  -DCMAKE_BUILD_TYPE=Release \
  -DWITH_LIBS_PRECOMPILED=ON \
  -DWITH_CYCLES=OFF -DWITH_USD=OFF -DWITH_ALEMBIC=OFF \
  -DWITH_OPENVDB=OFF -DWITH_OPENIMAGEDENOISE=OFF \
  -DWITH_XR_OPENXR=OFF
ninja -C build-linux-libs -j 10
cmake -S . -B build-linux-libs -DCMAKE_INSTALL_PREFIX=$HOME/goo-work/goo-install
ninja -C build-linux-libs install
```

结果：

- `7450/7450` 目标构建完成，无 `FAILED`。
- `build-linux-libs/bin/blender` 为 165,642,856 B。
- 安装到 `~/goo-work/goo-install` 后可启动：

```text
Blender 5.2.1 LTS
BPY_OK 5.2.1 LTS
```

- 预编译库没有写入二进制 RPATH；运行和测试时设置：

```bash
export LD_LIBRARY_PATH=$(find ~/lib-linux -maxdepth 4 -type d -name lib | paste -sd:)
```

## EEVEE 与 Goo 节点

在无显示器的 GTX 1660 主机上，通过 `-b` 后台模式执行：

- 13 个 Goo 节点全部可创建：
  `ShaderNodeLightInfo`、`ShaderNodeCurvature`、`ShaderNodeScreenspaceInfo`、
  `ShaderNodeSdfNoise`、`ShaderNodeSdfOp`、`ShaderNodeSdfPrimitive`、
  `ShaderNodeSdfVectorOp`、`ShaderNodeSetDepth`、`ShaderNodeShaderInfo`、
  `ShaderNodeTexHexagon`、`ShaderNodeTwirl`、`ShaderNodeWaterRipples`、
  `ShaderNodeOKLabColorRamp`。
- EEVEE 64×64 背景渲染成功，PNG 3,935 B。
- `material.check_shadow_id = 1` 后再次渲染成功，PNG 3,935 B，退出码 `0`；shadow-ID 的 depth/ID 两次 submit 路径未崩溃。

## Goo 测试

宿主 runner：`tests/python/goo_ctest_runner.py`；GPU/headless EEVEE 探测为 `True`，不是 skip。

| 测试 | 结果 | 用例 / 检查数 |
| --- | --- | --- |
| `goo_screenspace` | pass | 1 case / 5 checks |
| `goo_bsdf_light_groups` | pass | 6 cases / 40 checks |
| `goo_bsdf_light_groups_cases` | pass | 7 cases / 97 checks |
| `goo_light_groups` | pass | 18 cases / 24 checks |
| `goo_migration_fixtures`（Blender probe） | pass | 6 cases / 26 checks |

`goo_screenspace` 的实际像素哈希：

```text
pixel_sha256=367b13140bbb2c66b954b104448f9183fb7de4b433a74b67c2889f0fcc34f789
```

`goo_migration_fixtures` 覆盖 `goo_4_4`、`official_4_1`、`official_4_2`、`official_5_0`、`official_5_1`、`official_5_2_1`，并验证版本编码、Bloom→Glare 常量、14 条源码引用和 5.2.1 RNA 表面。

## 运行时注意

- 源码从 Windows `git archive` 传输后包含 CRLF；首次构建时 shader 生成器把 `\r` 当作非法 token（`draw_curves_infos.hh:3: Unexpected token "\"`）。已在 Linux 侧将所有非二进制文本归一化为 LF；Windows 源码归档流程后续应使用 `git -c core.autocrlf=false archive` 或上传前统一归一化。
- 测试输出目录位于 `~/goo-work/ctest-*` 与 `~/goo-work/migration-test`；结果 JSON 位于各自的 `result.json`。

## 驱动矩阵

矩阵新增 `debian-13-nvidia-gtx1660` 行（Debian 13、GTX 1660、驱动 `550.163.01`）。
在该主机上执行：

| 测试集 | 后端 | 状态 |
| --- | --- | --- |
| `smoke-opengl` | opengl | passed |
| `smoke-vulkan` | vulkan | passed |
| `render-eevee` | opengl | passed |
| `render-eevee` | vulkan | passed |

`verdict: PASS`，4/4 通过。原始报告、probe JSON 和日志在
[`evidence/driver-matrix-linux-local/`](evidence/driver-matrix-linux-local/)。
Windows 对应行见 [evidence-windows-local.md](evidence-windows-local.md)。

## 仍未验证

- 完整驱动矩阵：19 个环境中只有 `win11-intel` 与 `debian-13-nvidia-gtx1660` 两行
  有真实硬件结果；其余仍是 descriptor。
- 真实崩溃 dump 采集、Windows 签名、安装包/更新/回滚。
- 这些证据补齐前，不应把分支标记为“稳定生产版”。
