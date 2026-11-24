# 崩溃监控（Crash Monitoring）

本文档描述 Goo Engine 5.2 生产分支的崩溃监督原型 `tools/crashmon/`：它做什么、怎么用、采集什么、不采集什么，以及距离生产级崩溃遥测还差哪些东西。

> 状态（2026-11）：这是一个**原型**。Windows 路径已在一台 Windows 11 + Blender 5.2.3 LTS 的机器上完成端到端自测（真实子进程、日志、manifest、report、dry-run 上传计划），并已对一份**真实 Blender minidump** 完成发现、复制、SHA-256 与 report 校验（见
> [`evidence/crashmon-windows-real/`](evidence/crashmon-windows-real/)）。另用 `crashmon run` 监督了一次**受控原生崩溃**（测试用 CPython 扩展空指针写，不是产品故障），supervisor 直接观察到退出码 `0xC0000005` 并生成 `CRASH` bundle；该次未配置 WER LocalDumps，所以 crash artifacts 为 0（见 [`evidence/crashmon-supervisor-test/`](evidence/crashmon-supervisor-test/)）。尚未用产品真实故障做 supervised 崩溃，也没有符号化。Linux 与 macOS 的采集代码已实现，但本交付中只通过注入/模拟的能力探测测试过，**没有在真实 Linux/macOS 主机上运行**。不要把“代码路径存在”当成“该平台已验证”。

## 1. 目标与非目标

### 1.1 目标

- 以**直接子进程**方式启动 Blender，捕获 stdout/stderr，记录退出码/信号/超时。
- 从操作系统默认位置收集崩溃产物：Windows minidump/WER、Linux coredumpctl/core、macOS DiagnosticReports。
- 生成自包含 bundle：`manifest.json` + 轮转日志 + 复制进 bundle 的崩溃文件 + 可选 Markdown/JSON 报告。
- 对文本做**尽力而为**的脱敏；环境变量只采集白名单；默认不联网、不上传。
- 提供 `doctor`，在采集前诚实报告当前平台“能采到什么、缺什么”。
- 让失败模式可解释：缺失 dump 不等于没崩溃，退出码映射有边界。

### 1.2 非目标（明确不做）

- 不做全进程树监控。它只监督它直接启动的那个 Blender 进程；Blender 再启动的崩溃子进程、GPU TDR/驱动挂起、整机故障可能完全不在 bundle 里。
- 不做符号化。minidump/core 原样复制；没有 PDB/DWARF/build ID 解析，也没有调用栈重建。
- 不保证捕获所有原生崩溃。WER 默认不写本地 dump；Linux core 受 `core_pattern`、`ulimit -c`、容器权限、systemd-coredump/apport 配置影响；macOS 报告是异步落盘的。
- 不修改系统配置。不写 WER LocalDumps 注册表，不改 `core_pattern`，不改 ulimit，不申请额外权限。
- 不是上传后端。`--upload-url` 只是显式 opt-in 的本地 dry-run 计划，不包含任何 socket/HTTP 代码。
- 不做长期存储、去重、限流、加密、访问控制。bundle 就是本地目录，保留/删除由使用者负责。

## 2. 文件与组件

| 路径 | 作用 |
| --- | --- |
| `tools/crashmon/crashmon/cli.py` | `run` / `collect` / `doctor` / `report` 四个子命令与参数解析 |
| `tools/crashmon/crashmon/runner.py` | 子进程监督、stdout/stderr 双线程捕获、轮转日志、超时 terminate→kill、退出码/信号分类 |
| `tools/crashmon/crashmon/collectors.py` | Windows/Linux/macOS 崩溃产物发现、能力探测、复制/哈希、日志导入 |
| `tools/crashmon/crashmon/probes.py` | OS/CPU/内存/GPU/Blender 版本探测，全部异常转成数据 |
| `tools/crashmon/crashmon/bundle.py` | manifest 构造、schema 校验、环境白名单、原子写 |
| `tools/crashmon/crashmon/redaction.py` | 用户名/家目录/主机名（basic）与邮箱/IPv4（strict）替换 |
| `tools/crashmon/crashmon/report.py` | Markdown/JSON 报告渲染 |
| `tools/crashmon/crashmon/upload.py` | dry-run 上传计划与 URL 脱敏；无网络传输 |
| `tools/crashmon/tests/test_crashmon.py` | 44 个 unittest，标准库 only |
| `tools/crashmon/crashmon_cli.py` | 从仓库根目录调用的启动器 |
| `tools/crashmon/README.md` | 英文快速上手 |

运行方式（在 `tools/crashmon` 下）：

```text
python -m crashmon doctor
python -m crashmon run --blender <blender 可执行文件> --output-dir ./bundles --report
```

也可以从仓库根目录运行 `python tools/crashmon/crashmon_cli.py ...`。只依赖 Python 标准库，要求 Python 3.9+。如果系统 `python` 是 Microsoft Store 占位程序，使用 Blender 自带的 Python，例如 Windows 上的 `<blender>\\<version>\\python\\bin\\python.exe`。

## 3. 命令

### 3.1 `run` — 监督一次 Blender 运行

```text
python -m crashmon run --blender /path/to/blender \
  --output-dir ./bundles --report \
  -- --background scene.blend --python smoke.py
```

- `--` 之后的所有参数原样传给 Blender。
- **没有**在 `--` 后提供参数时，crashmon 使用一段短的非交互探测：
  `--background --factory-startup --python-expr "import bpy; print('crashmon startup probe:', bpy.app.version_string)"`。
  这是为了避免测试/CI 意外打开 GUI；需要真实场景时要显式传参。
- 子进程退出后按 `--since-minutes`（默认 30 分钟）窗口搜索崩溃产物；找到后复制进 `crash/`。
- `--timeout` 超时后先 terminate，5 秒后仍未退出则 kill，并在 manifest 中标记 `timed_out`。
- `--no-raw-logs` 只保留脱敏后的尾部行；默认保留完整日志并在 manifest 标记 `raw_logs_included: true`。
- `--fail-on-crash`：崩溃/异常退出 10，超时 11，其他异常（error/signal/killed 等）12；正常为 0。

### 3.2 `collect` — 从已有日志/崩溃目录构建 bundle

```text
python -m crashmon collect --output-dir ./bundles \
  --log blender.log --log crash-handler.log \
  --crash-dir ./crashes --exit-code-hex 0xC0000005 --report
```

- `--log` 可重复；导入时只保留尾部（受 `--max-log-bytes` / `--tail-lines` 约束）并脱敏。
- `--crash-dir` 可重复；只收扩展名匹配（`.dmp/.hdmp/.mdmp/.core/.crash/.ips/.wer/.diag`，以及文件名以 `core` 开头的文件）且路径包含 `--process-name`（默认 `blender`）的文件。
- `--exit-code` / `--exit-code-hex` / `--signal` 三选一，用于把已知的退出状态写进 manifest；不提供时 `run` 为 `null`。
- `--since-minutes` 默认 1440 分钟（比 `run` 宽，因为手工收集通常晚于崩溃）。

### 3.3 `doctor` — 只读能力检查

```text
python -m crashmon doctor
python -m crashmon doctor --json
```

报告 Python 版本、平台、Blender 探测、崩溃产物来源可用性、环境采集策略和上传边界。默认**始终退出 0**；`--strict` 在没有可用产物来源时退出 2。它不会安装、修改或“修复”任何系统设置。

### 3.4 `report` — 渲染已有 bundle

```text
python -m crashmon report ./bundles/<bundle-id> --format md
python -m crashmon report ./bundles/<bundle-id> --format json -o report.json
```

`--strict` 在 manifest schema 有问题时失败。报告只读本地文件，不联网。

### 3.5 常用参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--output-dir` | `crashmon-bundles` | bundle 根目录 |
| `--redact` | `basic` | `none` / `basic` / `strict` |
| `--max-log-bytes` | 64 MiB | 单个日志轮转阈值 |
| `--log-backups` | 3 | 保留的轮转备份数 |
| `--tail-lines` | `run` 80，`report` 40 | manifest/报告中的尾部行数 |
| `--max-artifact-bytes` | 64 MiB | 单文件超过则跳过并记录原因 |
| `--max-total-artifact-bytes` | 256 MiB | 单次复制的总量上限 |
| `--max-artifacts` | 20 | 单次最多复制文件数 |
| `--skip-gpu` / `--skip-probe` / `--skip-artifacts` | 关 | 跳过 GPU 探测 / Blender 版本探测 / 产物收集 |
| `--crashdumps-dir` | `%LOCALAPPDATA%\\CrashDumps` | Windows 覆盖路径，便于测试/便携环境 |
| `--include-system-cores` | 关 | Linux 显式 opt-in，调用 `coredumpctl dump` |
| `--upload-url` + `--consent` | 关 | 只生成 dry-run 计划 |

## 4. 平台采集

### 4.1 Windows（本次唯一实机验证的平台）

| 项 | 行为 |
| --- | --- |
| `%LOCALAPPDATA%\\CrashDumps` | 扫描 `*.dmp`/`*.hdmp`/`*.mdmp`，按进程名与时间窗过滤 |
| WER 目录 | 扫描用户级与系统级 `ReportArchive` / `ReportQueue`；优先读 `Report.wer` 头部；报告目录内匹配的 dump 也会被收集 |
| LocalDumps 注册表 | **只读**读取 `HKLM`/`HKCU` 下 `...\\Windows Error Reporting\\LocalDumps` 及 `\\blender.exe`/`\\blender` 子键，记录已配置值与 `enabled_hint`；从不写入 |
| `WerFault.exe` | 只记录是否存在，不调用、不阻塞、不修改 |
| 退出状态 | 同时处理有符号/无符号 NTSTATUS；例如 `0xC0000005` → `ACCESS_VIOLATION`、类别 `crash`；`0xC0000409` → `STACK_BUFFER_OVERRUN`；`0xC000013A` → 控制台中断；未知状态保留原始十六进制 |

Windows 默认不写本地 dump。`doctor` 会在 LocalDumps 未配置时给出警告，但不会替你打开它。要让 minidump 可采集，需要在目标机器上按组织策略配置 WER LocalDumps（本工具不提供自动配置），或依赖 WER 报告。

### 4.2 Linux（代码已实现，未实机验证）

- 只读检查 `coredumpctl` 是否可用，并运行 `coredumpctl --no-pager --no-legend list`（10 秒超时）。
- 读取 `/proc/sys/kernel/core_pattern` 与 `RLIMIT_CORE`，记录是否 piped（systemd-coredump/apport 常见）、soft/hard 限制。
- 检查 `/var/lib/systemd/coredump` 是否存在/可读。
- 默认**不复制系统 core**；只有显式 `--include-system-cores` 才调用
  `coredumpctl --no-pager dump --output <bundle>/crash/system-core-blender.dump blender`。
  该操作可能需要 polkit/权限，失败会作为数据记录，不会提权。
- 不修改 `core_pattern` 或 ulimit。容器、只读文件系统、权限不足都可能导致无 core。

### 4.3 macOS（代码已实现，未实机验证）

- 扫描 `~/Library/Logs/DiagnosticReports` 与 `/Library/Logs/DiagnosticReports` 下的 `.ips` / `.crash` / `.diag`，默认最多 500 个文件。
- 匹配文件名，或 `.ips` 首行 JSON 中的 `app_name` / `name` / `procName`，或 `.crash` 的 `Process:` 行。
- 只读取前 8 KiB 做摘要；系统级目录权限不足时记录错误，不提权。
- macOS 报告异步生成：崩溃后立刻 collect 可能看不到，稍后重试。

### 4.4 通用 `--crash-dir`

用于自建崩溃处理器、CI 临时目录或测试夹具。文件必须扩展名匹配（或以 `core` 开头），且**路径**中包含进程名（除非 `--process-name '*'`）。这是一个刻意的保守过滤，避免把无关崩溃复制进 bundle。

### 4.5 退出码与信号语义

分类值包括：`success`、`crash`、`error`、`exception`、`interrupted`、`timeout`、`spawn-error`、`signal`、`killed`、`terminated`。

- Windows：`0` → success；已知 NTSTATUS 按表映射；未知非零状态保留 `code_hex` 并归为 `error` 或 `exception`；超时优先于退出码。
- POSIX：负 returncode 解释为信号（SIGSEGV/SIGABRT/SIGBUS/SIGILL/SIGFPE/SIGSYS/SIGTRAP 归为崩溃；其他归为 signal/killed）。
- 已知歧义：shell 可能把信号翻译成 `128+N` 的普通退出码，仅凭数字无法与真正 `exit 139` 区分；manifest 会保留原始值，报告里不要过度解读。
- 启动失败（找不到可执行文件）为 `spawn-error`，`run` 退出 1。

## 5. Bundle 与 manifest

目录结构：

```text
<bundle-id>/
  manifest.json          # schema crashmon.bundle/1
  report.md              # 仅 --report
  upload-plan.json       # 仅 --upload-url --consent
  logs/
    stdout.log
    stdout.log.1 ...     # 轮转备份（若发生）
    stderr.log
  crash/
    <崩溃产物>
```

manifest 顶层字段：

| 字段 | 内容 |
| --- | --- |
| `schema_version` | 固定 `crashmon.bundle/1` |
| `tool` | crashmon 版本、Python 版本、解释器路径 |
| `bundle` | id、kind（run/collect）、UTC/本地时间、时区 |
| `privacy` | 脱敏级别、原始日志是否保留、环境范围、网络状态、替换计数/占位符 |
| `run` | 命令行、cwd、起止时间、耗时、超时、退出状态、日志条目；`collect` 且未给退出状态时为 `null` |
| `system` | OS、架构、CPU 型号/逻辑核数、内存总量、Python |
| `gpu` | 适配器/驱动/来源；跳过时记录 `skipped` 与原因；探测异常写入 `errors` |
| `blender` | 路径、是否存在/可执行、版本、build hash/date/platform、探测返回码 |
| `environment` | 白名单键、实际存在的值、考虑过的键、排除数量；不保存全量环境 |
| `logs` | 每个流的相对路径、大小、SHA-256、tail、轮转信息、脱敏计数 |
| `crash_artifacts` | 来源、类型、原始路径、相对路径、是否复制、大小、SHA-256、mtime、说明 |
| `platform` | 当前平台的采集能力、目录存在性/可读性、WER/注册表/coredumpctl/core_pattern 等 |
| `upload` | 是否请求、是否 consent、模式、状态、`network_performed: false`、目标（已脱敏）、计划文件 |
| `limitations` | 固定的人类可读限制清单 |
| `notes` | 本次运行的上下文说明（默认探测、跳过项、脱敏提示等） |

所有文本在 finalize 时统一脱敏；manifest 写入使用临时文件 + fsync + `os.replace`，避免半写状态。`report` 会重新加载并校验 schema，schema 问题在报告中列出。

## 6. 脱敏、隐私与保留

### 6.1 脱敏级别

| 级别 | 替换内容 |
| --- | --- |
| `none` | 不替换；仅用于你完全控制的离线调试 |
| `basic`（默认） | 家目录（`\\` 与 `/` 变体）、用户名、主机名 → `<HOME>` / `<USER>` / `<HOSTNAME>` |
| `strict` | 在 basic 基础上，先整体替换邮箱 → `<EMAIL>`、IPv4 → `<IP>` |

实现细节与边界：

- 用户名/主机名使用词边界匹配，避免把 `alice` 误替换进 `malice` 之类的词。
- 文本日志在写入时脱敏；文本崩溃报告（`.wer`、`.ips`、`.crash`、`.diag` 等）在复制时脱敏。
- **二进制 minidump/core 原样复制，无法脱敏**。它们可能包含进程内存、路径、令牌、用户数据。bundle 应按敏感数据处理。
- 人机模式的终端回显是子进程原始输出，不经过脱敏；把终端输出重定向到文件时要自行评估。`--quiet` 或 `--json` 可关闭回显。
- 脱敏是**尽力而为**，不是数据泄露防护的保证。不要把它当成匿名化。

### 6.2 环境变量

只采集 `bundle.ENV_ALLOWLIST` 中的键（Blender 用户/系统路径、渲染后端相关变量、显示服务器、语言、临时目录、动态库路径等），单个值上限 2048 字符；其余变量只统计数量，不保存键名或值。`TEMP`/`TMP` 等路径仍会经过脱敏。

### 6.3 日志保留

- 默认保留完整日志（`raw_logs_included: true`），manifest 中标记 `retained: true`。
- 轮转：单文件超过 `--max-log-bytes`（默认 64 MiB）后写 `.1`、`.2`…，最多保留 `--log-backups`（默认 3）份。
- `--no-raw-logs`：只写脱敏后的 tail，manifest 标记 `retained: false` / `tail_only: true`。
- crashmon **不自动删除**旧 bundle。生产环境必须另行实现保留期、配额、访问控制和清理；bundle 可能很大（dump 上限 64 MiB/文件、256 MiB/次）。

## 7. 上传边界（dry-run）

默认 `upload.mode = "disabled"`、`network_performed = false`。只有同时提供 `--upload-url` 和 `--consent` 才会进入 dry-run：

1. 校验 URL（相对/非法 URL 报错）；去掉 userinfo；对 `token/key/secret/auth/password/signature/sig/credential/session` 等查询参数做脱敏；非 https 只给警告。
2. 扫描 bundle 中现有普通文件（跳过符号链接，最多 1000 个），记录相对路径、大小、SHA-256。
3. 原子写入 `upload-plan.json`（schema `crashmon.upload-plan/1`），其中 `network_performed: false`。
4. manifest 的 `upload` 段记录目标（脱敏后）、文件数、总字节、计划文件、`status: "planned-dry-run"`。

**没有任何网络代码**。这是有意的：要接入真实后端，必须另行实现传输、认证、重试、幂等、 consent UI、保留与删除策略，不能只把 URL 填进来。

已知实现细节：上传计划在 manifest finalize **之前**生成，因此它通常只包含日志和已复制的崩溃文件，不包含 `manifest.json` 和 `report.md`；`upload-plan.json` 中会写明这一点。manifest 里的 `file_count` 是计划快照，不是最终 bundle 的完整清单。这是原型的边界，不是完整上传清单。

## 8. 隐私与合规建议（生产前必须做）

- 明确告知用户采集范围，并把上传做成**默认关闭、逐次/逐组织显式同意**；不要把 `--consent` 简化成全局永久开关。
- 对 bundle 做静态敏感信息扫描（路径、用户名、令牌、邮箱、内存字符串），尤其是二进制 dump。
- 传输与静态存储加密；短期保留 + 到期删除；限制访问角色；记录访问审计。
- 若涉及欧盟/中国等地区的个人信息，先完成法律与数据出境评估；dump 可能包含第三方数据。
- 为崩溃上传建立速率限制与配额，避免崩溃风暴打爆存储/带宽。
- 用户应能查看、删除自己的 bundle；提供“只上传元数据/只上传日志/完整 dump”的分级选择。

## 9. 能做什么 / 不能做什么（验收口径）

### 9.1 已验证可用

- 启动真实 Blender 5.2.3 LTS 子进程，捕获 stdout/stderr，正确记录退出码 0。
- 生成合法 bundle：manifest schema 校验 0 问题、日志 SHA-256、尾部行、环境白名单、Windows 平台能力段、报告、dry-run 上传计划。
- `collect` 从日志 + 退出码构建 bundle；`report` 渲染 Markdown/JSON；`doctor` 在缺 Blender/缺 coredumpctl 时不崩溃；`--fail-on-crash` 映射；日志轮转；超时 terminate/kill。
- 脱敏 basic/strict；上传缺 consent 时拒绝生成计划。
- 44 个 unittest 全部通过；12 个 Python 文件 + 测试文件 `py_compile` 通过。

### 9.2 不能保证

- 不能保证捕获所有原生崩溃。没有 WER LocalDumps 时 Windows 可能没有 dump；Linux 可能被 `core_pattern`/ulimit/容器策略拦住；macOS 报告可能延迟或权限不足。
- 不能把 dump 变成可读调用栈；没有符号服务器、build ID 匹配、minidump 解析。
- 不能监控非直接子进程、GPU 挂起/TDR、驱动崩溃、整机重启。
- 不能保证退出码语义；shell 的 `128+N` 与真实退出码可能混淆。
- 不能保证脱敏覆盖二进制内容，也不能保证 bundle 匿名。
- 不是上传客户端，没有网络传输；`upload-plan.json` 不是已上传证明。

## 10. 生产化路线

如果目标是“生产可用的 Blender 崩溃上报”，建议按以下顺序演进，而不是继续给本原型堆功能：

1. **接入成熟崩溃 SDK**：Crashpad（跨平台、out-of-process handler、minidump）或 Sentry/Breakpad 方案；让崩溃处理器在 Blender 进程外运行，覆盖挂起/异常终止。
2. **构建 ID 与符号**：发布时保留 build ID / commit / PDB / DWARF，部署 symbol server（或 debuginfod），在服务端做符号化；bundle 中记录精确构建标识。
3. **受控上传**：认证、TLS、分片/断点续传、幂等键、速率限制、服务端配额、失败重试与本地 spool；上传前做敏感信息扫描与用户确认。
4. **保留与删除**：服务端 TTL、按项目/版本/严重度分级、用户删除接口、审计日志、加密静态存储。
5. **产品化隐私**：同意 UI、采集范围说明、按字段分级（元数据/日志/dump）、企业策略开关、数据出境评估。
6. **可观测性**：崩溃率、去重（stack hash/build ID）、版本回归、告警；不要把“bundle 数量”当指标。
7. **CI 集成**：在冒烟/夜间任务中用 `run --fail-on-crash` 作为门禁；产物作为受限 artifact 上传；失败时附 `report.md`。

## 11. 验证记录（2026-11，Windows 11）

环境：Windows 11（10.0.26200），Intel Core i5-12500H，16 GB 内存，Blender 5.2.3 LTS（build hash `e13b728c6880`），Blender 自带 Python 3.13.13。

执行的命令与结果：

```text
# 在 tools/crashmon 下
python -m py_compile crashmon\__init__.py crashmon\__main__.py crashmon_cli.py ^
  crashmon\util.py crashmon\redaction.py crashmon\probes.py crashmon\collectors.py ^
  crashmon\bundle.py crashmon\runner.py crashmon\upload.py crashmon\report.py ^
  crashmon\cli.py tests\test_crashmon.py
# exit 0

python -m unittest discover -s tests -v
# Ran 44 tests ... OK，exit 0

python -m crashmon run --blender "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" ^
  --output-dir <temp> --skip-gpu --skip-artifacts --report --json ^
  --upload-url "https://example.invalid/intake?token=demo-secret" --consent
# exit 0；bundle 含 manifest.json、report.md、upload-plan.json、logs/{stdout,stderr}.log
# 报告 Verdict: CLEAN EXIT；manifest validation: no schema problems；network_performed: false
```

实测 bundle 的要点：stdout 113 B，捕获到 `crashmon startup probe: 5.2.3 LTS` 与 Blender 版本行；stdout/stderr 的 SHA-256、tail、轮转信息齐全；basic 脱敏替换 6 处（家目录 → `<HOME>`）；环境白名单存在 4 个变量、排除 62 个；Windows 平台段记录 `CrashDumps` 存在/可读、LocalDumps 未配置（`enabled_hint: false`）、系统 WER 目录存在、`WerFault.exe` 存在；`upload-plan.json` 中 `token=demo-secret` 被替换为 `<redacted>`，且 `network_performed: false`。

### 真实崩溃 dump 采集（2026-11-24，Windows）

`crashmon collect` 处理了真实文件
`C:\Users\zbq\AppData\Local\CrashDumps\blender.exe.11652.dmp`（135,681,994 B，
`MDMP` 头，SHA-256 `8b1580476ab1e153cbe3a5efdd1050df5c90aba81245052c296cc99386be7452`）。
WER Application Error 记录：`blender.exe` 5.2.1.0、`tbbmalloc.dll`、`0xC0000005`、
偏移 `0x6829`。采集结果：

- `Verdict: CRASH`，发现 5 个候选产物、复制 1 个，schema 校验 0 问题。
- 真实 dump 被复制到 bundle 的 `crash/`，manifest 记录 129.4 MiB 与 SHA-256。
- 同目录另外 2 个 Blender dump 和 2 个 110 KiB WER 报告被识别，但因
  `--max-artifacts 1` 未复制。
- 退出码由 WER 事件人工提供，**不是 supervisor 观察到的退出码**；报告 Notes 明确记录。

证据：`manifest.json`、`report.md` 和来源说明在
[`evidence/crashmon-windows-real/`](evidence/crashmon-windows-real/)。129.4 MiB 的 dump
本身未提交，只保留在忽略的 `_work/` 中。

### supervisor 受控原生崩溃（2026-11-24，Windows）

为验证 `crashmon run` 自己不依赖 WER 事件也能识别崩溃，使用 `_install-windows2\blender.exe`
和一个**测试专用** CPython 扩展 `crash_ext.pyd`：其 `PyInit_crash_ext` 执行空指针写，
从 Blender 自带 Python `import crash_ext` 触发原生 `EXCEPTION_ACCESS_VIOLATION`。

```powershell
python tools\crashmon\crashmon_cli.py run `
  --blender _install-windows2\blender.exe `
  --output-dir _work\crashmon-supervisor --skip-gpu --skip-probe --timeout 120 --report `
  -- --background --factory-startup --python _work\crashnative\crash_blender_ext.py
```

结果：bundle `crashmon-run-20261124T192808Z-ec630c`，`Verdict: CRASH`，
退出 `0xC0000005`（raw `3221225477`），2 条日志，崩溃产物 0 个，manifest
schema 0 问题。该次没有配置 WER LocalDumps，所以没有生成 `.dmp`；Blender 自己写
`%TEMP%\blender.crash.txt` 也因权限失败，但把原生栈写到了 stderr。它证明 supervisor
的退出码分类、日志捕获和 bundle 收尾可用，**不代表**产品真实故障路径或 dump 符号化已验证。
证据：[`evidence/crashmon-supervisor-test/`](evidence/crashmon-supervisor-test/)。

### 明确未验证

| 项 | 原因 / 影响 |
| --- | --- |
| 真实 Linux 主机上的 coredumpctl/core_pattern/ulimit 采集 | 只在 Windows 上以注入的 `which_func`/`runner`/reader 测试；未验证发行版差异、权限、systemd-coredump |
| 真实 macOS DiagnosticReports 解析 | 未在 macOS 上运行；`.ips` 首行 JSON 与 `.crash` 的格式随版本变化，匹配可能漏报 |
| `crashmon run` 监督真实原生崩溃 | 已用受控测试扩展触发 `0xC0000005`，supervisor 直接观察到并生成 `CRASH` bundle；但未配置 WER LocalDumps，artifacts=0，且不是产品真实故障路径 |
| dump 符号化/调用栈重建 | 没有 PDB/build ID 匹配；只能保留原始 minidump |
| `coredumpctl dump` 实际提取 | 未在 Linux 实机执行；polkit/权限行为未知 |
| Windows `Report.wer` 真实报告解析 | 已发现真实 WER 报告并记录大小，但本次未复制/解析其内容 |
| GPU 探测在 Linux/macOS 的完整性 | Windows 实测可用；其他平台仅代码路径 |
| 生产上传 | 按设计未实现，只有 dry-run 计划 |

## 12. 测试覆盖与维护

```text
cd tools/crashmon
python -m py_compile crashmon/__init__.py crashmon/__main__.py crashmon/cli.py ^
  crashmon/util.py crashmon/redaction.py crashmon/probes.py crashmon/collectors.py ^
  crashmon/bundle.py crashmon/runner.py crashmon/upload.py crashmon/report.py ^
  crashmon_cli.py tests/test_crashmon.py
python -m unittest discover -s tests -v
```

测试覆盖：manifest schema/往返校验、脱敏（含 strict 邮箱/IP、词边界）、退出码与信号映射（Windows 有符号/无符号、POSIX、超时、spawn-error）、Linux 无 coredumpctl 时 doctor 不崩溃、产物复制/哈希/上限/跳过原因、日志导入截断、轮转与 tail-only、监督运行/超时/启动失败、报告 Markdown/JSON、上传参数校验/目标脱敏/consent 边界，以及 CLI `run`/`collect`/`doctor`/`report` 端到端。

新增平台逻辑时，请优先用“注入探测函数”的方式写单测（参考 `CollectorTests.test_linux_capabilities_do_not_crash_without_coredumpctl`），这样在没有对应 OS 的 CI 上仍能验证失败路径不崩溃。真实平台采集需要自托管 runner 和真实崩溃样本，属于后续工作。

## 13. 排错

- `doctor` 显示 `available: false`：当前平台既没有可读的默认崩溃目录，也没有匹配的报告目录；先用 `--crash-dir` 指向自建崩溃处理器输出，或在目标机器上配置 WER LocalDumps。
- Windows 没有 dump：确认 WER LocalDumps 已配置（crashmon 只读提示，不代改），或 WER 报告已生成；检查 `%LOCALAPPDATA%\\CrashDumps` 权限。
- Linux 没有 core：检查 `coredumpctl list`、`/proc/sys/kernel/core_pattern`、`ulimit -c`、容器 `--privileged`/seccomp 限制；系统 core 默认不复制，需 `--include-system-cores`。
- macOS 没有报告：等待异步落盘后重跑 `collect`；系统目录可能需要权限。
- 报告显示 `CLEAN EXIT` 但没有产物：这**不代表**没有崩溃，只代表本次直接子进程退出码为 0 且未发现匹配产物；看 limitations 和 `platform` 段。
- bundle 过大：用 `--no-raw-logs`、降低 `--max-artifact-bytes` / `--max-total-artifact-bytes`，并清理旧 bundle（工具不会自动删除）。
