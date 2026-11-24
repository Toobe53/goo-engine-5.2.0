# 驱动矩阵（Driver Matrix）

本文档描述 Goo Engine 5.2 生产分支的驱动/后端兼容矩阵、主机探测与矩阵执行器，以及把完整矩阵落到 CI 所需的硬件条件。

> 状态（2026-11-25）：仓库内已有可执行的矩阵与执行器。Windows 11 + Intel Iris Xe
> 和 Debian 13 + NVIDIA GTX 1660 两行已完成真实 Goo 构建的 smoke + EEVEE 4/4
> 执行；其余 13 行 Windows/Linux 组合和 4 行 macOS 历史数据**未在本交付中做实机
> 执行**。它们需要下文所述的硬件实验室/自托管 runner。不要把“矩阵里存在这一行”
> 误读为“这一行已经通过验证”。

## 1. 目的与范围

矩阵回答三个问题：

1. 哪些 OS + GPU + 显示服务器 + 后端组合属于发布门禁（gate）？
2. 每个组合的最低驱动版本是什么，测试集有哪些？
3. 当前主机能否运行其中某一行，结果如何留档？

范围仅覆盖图形后端与驱动一致性，不覆盖 CPU 特性、内存、磁盘、安装器、打包或性能基准。性能回归应另建基准任务，不要塞进本矩阵。

## 2. 文件与组件

| 路径 | 作用 |
| --- | --- |
| `tools/driver_matrix/matrix.json` | 机器可读矩阵，唯一事实来源（single source of truth） |
| `tools/driver_matrix/detect.py` | 探测当前主机 OS/架构/GPU/驱动/后端，输出 JSON |
| `tools/driver_matrix/run_matrix.py` | 按矩阵筛选当前主机、执行 smoke、写 JSON + Markdown 报告 |
| `tools/driver_matrix/smoke/gpu_probe.py` | 在 Goo/Blender 应用内运行的探测与 EEVEE/Workbench 渲染 payload |
| `tools/driver_matrix/driver-matrix.yml.example` | 自托管 GPU runner 的 GitHub Actions 示例（刻意不放在 `.github/workflows/`） |
| `driver_matrix_reports/`（运行目录下） | 本地默认报告目录（运行时生成，不应提交） |

## 3. 矩阵结构

`matrix.json` 的顶层字段：

- `schema_version`：当前为 `1.0`。
- `project`：项目名、版本、目标分支。
- `policy`：`tiers` 定义 gate/nightly/advisory 的含义；`hosted_ci.gpu_available=false` 明确 GitHub 托管 runner 不作为 GPU 证据。
- `backends`：允许的后端及其最低版本/平台限制。
- `binary_candidates`：查找 Goo 可执行文件的候选路径（相对仓库根目录）。
- `test_sets`：可复用的测试集定义。
- `environments`：矩阵行，每行一个 `id`。

每个 environment 包含：

| 字段 | 含义 |
| --- | --- |
| `id` | 稳定标识，报告与 `--entry` 使用 |
| `description` | 人读描述 |
| `os` | `family`（windows/linux/macos）、`release`、`arch` |
| `gpu` | `vendor`、`kind`、`examples`、`min_driver` |
| `display_server` | `windows` / `x11` / `wayland` / `quartz` |
| `backends` | 该行要验证的后端 |
| `test_sets` | 引用的测试集名称 |
| `runner_labels` | 自托管 runner 必须具备的标签（必须含 `self-hosted`） |
| `required` / `tier` | 是否发布必需、门禁级别 |
| `notes` | 覆盖范围、已知限制 |

`min_driver` 使用 `scheme` 描述版本如何比较，避免把 NVIDIA 的 `31.0.15.3623` 和显示版本 `536.23` 直接比较：

| scheme | 用于 | 比较值来源 |
| --- | --- | --- |
| `nvidia-windows` | NVIDIA Windows | 归一化后的显示版本，如 `536.23` |
| `nvidia-linux` | NVIDIA Linux | 驱动版本，如 `535.104.05` |
| `amd-windows-raw` | AMD Windows | INF 原始版本，如 `31.0.24033.1003` |
| `intel-windows-raw` | Intel Windows | INF 原始版本，如 `31.0.101.5186` |
| `mesa` | AMD/Intel Linux | Mesa 版本，如 `24.0.9` |
| `apple-os` | Apple/macOS | macOS 版本（Metal 随 OS 交付），如 `15.0` |

## 4. 覆盖范围

矩阵共 19 行：Windows/Linux 交付范围 15 行，macOS 4 行仅保留为历史/不交付数据。
Windows/Linux 行分三档：

- **gate（必需）**：发布前必须在对应硬件上通过；失败阻断发布。
- **nightly（可选）**：每周跑，不作为单次发布阻断条件，但连续失败需要 triage。
- **advisory（可选）**：已知限制或已弃用路径，只做知情记录。

| OS | GPU | 行数 | 级别 |
| --- | --- | --- | --- |
| Windows 10 22H2 | NVIDIA / AMD / Intel 集显 | 3 | NVIDIA、AMD gate；Intel 集显 nightly |
| Windows 11 23H2+ | NVIDIA / AMD / Intel Arc | 3 | 均 gate |
| Ubuntu 22.04 | NVIDIA / AMD / Intel | 3 | NVIDIA、AMD gate；Intel nightly |
| Ubuntu 24.04 | NVIDIA / AMD / Intel + NVIDIA/AMD Wayland | 5 | NVIDIA、AMD（X11）gate；其余 nightly |
| Debian 13 | NVIDIA GTX 1660 | 1 | gate |
| **合计** | | **15** | 当前 2 行有真实硬件结果 |

macOS 的 4 行仍保留在 `matrix.json` 中，但按用户要求移出 5.2.1 交付与 release gate；
不再作为构建或签名验收条件。

后端策略（Windows/Linux）：

| 后端 | Windows | Linux |
| --- | --- | --- |
| OpenGL | 最低 4.3，gate | 最低 4.3，gate |
| Vulkan | 最低 1.2，gate | 最低 1.2，gate |

> 重要：矩阵只声明“支持范围”，不等于“已在这些机器上验证”。当前只有
> `win11-intel`（Windows 11 / Iris Xe）和 `debian-13-nvidia-gtx1660`
> （Debian 13 / GTX 1660）两行完成了 smoke + EEVEE 的真实 4/4 执行；其余
> 13 行标为**未覆盖**，不得在发布说明中写成通过。发布 manifest 的
> `driver_requirements` 只声明目标要求，不构成覆盖证明。

### 4.1 最低驱动基线

| 平台 | 基线 | 说明 |
| --- | --- | --- |
| NVIDIA Windows | 显示驱动 `536.23` | 对应 2023 年中 R535 分支；更老的驱动不再进入 gate |
| NVIDIA Linux | `535.104.05` | 与 Windows 基线大致同代 |
| AMD Windows | Adrenalin 24.3.1，INF `31.0.24033.1003` | 只比较 INF 原始版本 |
| Intel Windows | `31.0.101.5186` | Arc 与 Iris Xe 共用驱动包，但行仍按硬件区分 |
| Mesa（Ubuntu 22.04） | `23.2.1` | AMD/Intel 开源栈；Ubuntu 自带的 Mesa 版本 |
| Mesa（Ubuntu 24.04） | `24.0.9` | 同上 |
| Apple | macOS `14.0` / `15.0` | Metal 版本随 OS；OpenGL 在 macOS 固定 4.1 |

基线不是“推荐驱动”。推荐版本、季度更新流程和责任人见 `maintenance.md`。

## 5. 主机探测 `detect.py`

```bash
python tools/driver_matrix/detect.py --out artifacts/host.json
python tools/driver_matrix/detect.py --compact
python tools/driver_matrix/detect.py --strict   # 无物理 GPU 时退出码 2
```

输出结构（节选）：

```json
{
  "schema_version": "1.0",
  "host": {"os": {"family": "windows", "release": "11", "arch": "x64"}, "arch": "x64"},
  "display_server": "windows",
  "gpus": [{"vendor": "intel", "kind": "integrated", "driver_version": "31.0.101.5590", "driver_scheme": "intel-windows-raw"}],
  "primary_gpu": {"vendor": "intel", "kind": "integrated"},
  "backends": {"opengl": {"available": true}, "vulkan": {"available": false}, "metal": {"available": false}}
}
```

探测是 **best-effort 静态探测**，不创建 OpenGL/Vulkan 上下文：

- Windows：PowerShell CIM 查询显示适配器；虚拟显示器（如远程桌面、IddDriver）会被标记为 `virtual` 并排除出主 GPU；Vulkan 通过 `vulkan-1.dll` 与 Khronos ICD 注册表项判断。
- Linux：`/etc/os-release`、`/sys/class/drm`、`lspci`、`nvidia-smi`、`glxinfo -B`、`vulkaninfo`、内核模块版本。无 X/Wayland 会话时 OpenGL 版本可能是 `unknown`。
- macOS：`sw_vers`、`system_profiler SPDisplaysDataType`；Metal 是否可用按 macOS 主版本与 GPU 推断；OpenGL 固定报告 4.1 deprecated。
- WSL 会被识别并注明，矩阵不覆盖 WSL 图形栈。

因此 `backends.*.available=true` 只代表“检测到驱动/loader”，不代表 Goo 一定能在该后端启动；真正的证据来自 `run_matrix.py` 的 smoke 结果。探测失败不会崩溃：无法识别的部分写 `null`/`unknown` 并追加 `warnings`。

## 6. 矩阵执行器 `run_matrix.py`

### 6.1 常用命令

```bash
# 校验矩阵（CI 静态检查）
python tools/driver_matrix/run_matrix.py --validate-only

# 列出所有行
python tools/driver_matrix/run_matrix.py --list

# 只看计划，不执行、不写文件
python tools/driver_matrix/run_matrix.py --dry-run

# 仅针对当前主机匹配的行执行
python tools/driver_matrix/run_matrix.py --out-dir artifacts/driver-matrix

# 明确指定一行，并用外部 smoke 脚本
python tools/driver_matrix/run_matrix.py --entry win11-intel \
  --smoke-cmd "python3 my_lab_smoke.py --json out.json"

# 允许没有 Goo 二进制（只验证编排/探测流程）
python tools/driver_matrix/run_matrix.py --dry-run --allow-missing-binary
```

### 6.2 匹配规则

- OS `family`、`release`、`arch` 精确匹配；`display_server` 精确匹配（Linux 的 X11/Wayland 行是分开的）。
- GPU 只比较 **vendor**（`nvidia`/`amd`/`intel`/`apple`），**不比较 `kind`**。原因是同一驱动包常同时覆盖集显与独显（例如 Intel Arc 与 Iris Xe），且探测在虚拟机/远程会话下对 kind 的判断不稳定。
- 副作用：Iris Xe 主机也会命中 `win11-intel`（Arc 行）。如果实验室必须区分集显/独显，应新增独立条目，或用 `--entry` 明确指定；报告会在 `notes` 中保留该行的硬件描述，triage 时需人工确认。
- `--entry`、`--tier`、`--os-family`、`--gpu-vendor`、`--backend`、`--required-only` 进一步筛选；被筛掉的行会记录 `selection.skipped[].reasons`。

### 6.3 驱动符合性

执行器把探测到的版本与 `min_driver` 比较，写入 `driver_conformance`，状态为 `met` / `below` / `unknown`：

- 默认 `below` 只警告，不阻断。
- `--require-min-driver` 时 `below` 也判定失败。
- `unknown` 不会被视为通过；在报告里必须人工确认。

### 6.4 执行与报告

- `test_sets` 中的占位符会展开：`{goo_binary}`、`{repo_root}`、`{backend}`、`{out_dir}`、`{python}`、`{entry_id}`。
- 可用 `--smoke-cmd` 完全覆盖默认测试集（每个 entry × backend 执行一次），便于实验室接入自己的 harness。
- 每个 run 写独立日志：`<out_dir>/logs/<entry>__<test_set>__<backend>.log`。
- 报告原子写入 `<out_dir>/report.json` 与 `<out_dir>/report.md`；JSON 包含 `matrix`、`host`、`selection`、`driver_conformance`、`environment`、`runs`、`summary`。
- 退出码：`0` 通过（无 failed/error）；`1` 存在失败/错误（`--strict` 时 skipped 也失败）；`2` 参数/矩阵错误，或当前主机无匹配行（`--allow-empty` 可放行）。
- 缺 Goo 二进制默认记 `error` 并退出 1；`--allow-missing-binary` 时记 `skipped`，适合只验证编排。
- `--dry-run` 只打印 host、匹配/跳过、binary 查找与执行计划，不写任何文件。

### 6.5 默认 smoke payload

`smoke/gpu_probe.py` 在 Goo 内运行，采集 `bpy.app` 与 `gpu.platform` 信息，校验 `gpu.platform.backend_type` 是否与预期后端一致，并尝试 Workbench/EEVEE 64px 渲染。它只依赖 `bpy`/`gpu` 与标准库；不修改用户配置，不写系统目录。

## 7. CI 与硬件要求

### 7.1 GitHub 托管 runner 没有可用 GPU

GitHub 托管的 `ubuntu-latest`、`windows-latest`、`macos-latest` **不提供可用于 OpenGL/Vulkan/Metal 一致性测试的 GPU**。它们可以运行：

- `python -m py_compile`；
- `run_matrix.py --validate-only`；
- `run_matrix.py --dry-run --allow-empty`。

不能作为任何 gate 行的证据。`driver-matrix.yml.example` 底部的 `static-checks` job 就是给托管 runner 用的。

### 7.2 自托管 GPU runner

完整矩阵必须落在硬件实验室或自托管 runner 上。`driver-matrix.yml.example` 演示：

- 用 `runs-on: ${{ matrix.labels }}` 选择 GPU runner；
- `labels` 与 `matrix.json` 的 `runner_labels` 一致（`self-hosted`、`windows`/`linux`/`macos`、`x64`/`arm64`、`gpu-nvidia`/`gpu-amd`/`gpu-intel`/`gpu-apple`、`gpu-discrete`/`gpu-integrated`，Linux 再加 `session-x11`/`session-wayland`）；
- checkout → `detect.py` → `run_matrix.py` → 上传 `artifacts/host.json` 与报告；
- 每周一 03:17 UTC 跑一次，也支持手动指定单个 entry。

部署步骤与准入条件：

1. 准备物理机，安装受支持的 OS 与最低驱动以上版本。
2. 安装 self-hosted runner，注册精确的 label 集；label 拼写错误会让 job 永久排队。
3. 在 runner 上放置 Goo 构建，用仓库/组织变量 `GOO_BINARY` 指定绝对路径。
4. 先手动跑 `detect.py` 与 `run_matrix.py --dry-run` 确认匹配，再启用定时任务。
5. 将示例复制到 `.github/workflows/` 是另外的变更，需仓库管理员评审；本仓库当前**没有**启用该 workflow。

### 7.3 实验室最低配置建议

| 平台 | 建议机器 |
| --- | --- |
| Windows | 2 台：NVIDIA 独显、AMD 独显；Intel Arc 可复用其中一台的第二块盘/第二系统 |
| Linux | 2 台：Ubuntu 22.04/24.04 双系统或两台，NVIDIA 与 AMD 各一；Wayland 行可在同一台切换会话 |
| macOS | 不适用：macOS 已移出 5.2.1 交付与 release gate；对应 4 行只保留历史 |
| 网络 | runner 需能拉取仓库与构建产物；签名密钥**不得**放在 runner 上 |

## 8. 新增或修改矩阵行

1. 修改 `tools/driver_matrix/matrix.json`，保持 `id` 唯一。
2. `runner_labels` 必须包含 `self-hosted`，且不得使用 `ubuntu-`、`windows-`、`macos-` 前缀的托管标签（加载器会拒绝）。
3. 新 test set 的命令只允许文档化的占位符；固定 `backend` 必须出现在该行的 `backends` 中。
4. 运行 `--validate-only`；再用 `--list` 检查排序与字段。
5. 在对应硬件上跑 `detect.py` + `run_matrix.py --dry-run`，确认匹配到预期行。
6. 更新本文档的覆盖表与基线表；驱动变更需在 `maintenance.md` 记录季度批次。

## 9. 已知缺口与未验证项（诚实清单）

- 已在 Windows 11 26200 / Intel Iris Xe（驱动 31.0.101.5590）和
  Debian 13 / NVIDIA GTX 1660（驱动 550.163.01）上跑通真实 Goo 构建的
  `gpu_probe.py`：两行的 smoke-opengl、smoke-vulkan、render-eevee
  opengl/vulkan 均为 4/4 passed，报告在
  `docs/production/evidence/driver-matrix-{windows,linux}-local/`。
- 剩余 13 行（Windows 10、NVIDIA/AMD/Intel Arc、Ubuntu 22.04/24.04 及
  Wayland）没有对应硬件，标为**未覆盖**，未执行。
- macOS 4 行未运行且已移出交付；不得作为 release gate 通过项。
- GitHub Actions 示例未在真实仓库启用，未验证 runner 标签与上传步骤；
  `driver-matrix.yml.example` 的 YAML 仅人工审查。
- 矩阵按 vendor 匹配、不按 kind 匹配，存在集显/独显串行风险（见 6.2）。
- 最低驱动基线来自公开驱动分支，需要在实验室季度更新时复核（见 `maintenance.md`）。

## 10. 责任人（占位）

| 职责 | 负责人 | 备注 |
| --- | --- | --- |
| 驱动矩阵 owner | TBD | 维护 matrix.json 与本文档 |
| Windows GPU lab | TBD | NVIDIA/AMD/Intel 机器与 runner |
| Linux GPU lab | TBD | Ubuntu X11/Wayland |
| macOS lab | 不适用 | 已移出 5.2.1 交付；仅保留历史矩阵行 |
| CI/runner 基础设施 | TBD | self-hosted runner、标签、密钥隔离 |
| 发布门禁裁决 | TBD | gate 失败时决定阻断/例外 |

未填写的 TBD 必须在对应硬件到位前保持 TBD，不要用“已覆盖”代替真实 runner。
