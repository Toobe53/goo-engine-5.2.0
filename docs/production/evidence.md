# 生产验证证据：CI 历史（2026-11-24）

本文件记录 `production/5.2` 在提交 `9d4dce4e` 上通过 GitHub CI 和 Windows CI 产物取得的
历史证据。macOS 已按用户要求移出当前交付范围；Windows/Linux 的本地构建、干净安装、
CTest 和真实驱动矩阵见
[evidence-windows-local.md](evidence-windows-local.md) 与
[evidence-linux-local.md](evidence-linux-local.md)。本文件只记录已执行的内容；没有执行的
项目在最后一节明确列出。

## 构建产物

| 项目 | 结果 |
| --- | --- |
| Linux x64 | 成功；CI run `45384517223`，artifact `goo-engine-linux-x64` |
| Windows x64 | 成功；CI run `45384517223`，artifact `goo-engine-windows-x64`，314,166,084 B |
| macOS arm64 | 编译曾成功（run `45344327291`），但 artifact 路径错误；后续 macOS job 均未获得 runner |

Windows 产物实测版本：

```text
Blender 5.2.1 LTS
build hash: 9d4dce4e05d7
build date: 2026-11-24
build type: Release
```

## Windows 运行时 smoke

在解压后的 `blender.exe` 上执行，均为真实 Windows 进程：

- 启动退出码 `0`。
- 13 个 Goo 节点均可创建：Light Info、Curvature、Screenspace Info、SDF Noise/Op/Primitive/Vector Op、Set Depth、Shader Info、Tex Hexagon、Twirl、Water Ripples、OKLab Color Ramp。
- EEVEE 64×64 背景渲染成功，输出约 3,621 B PNG；首次约 22 s，后续约 2.5 s。
- `material.check_shadow_id = 1` 后再次渲染成功，退出码 `0`；验证 shadow-ID 的 depth/ID 两次 submit 路径不再崩溃。
- `goo_screenspace.py --raytracing 0` 成功；`--runtime-global` 在 RT 关闭/开启之间切换 4 次，全部成功。两次输出均为预期的背景色，`pixel_sha256=367b13140bbb2c66b954b104448f9183fb7de4b433a74b67c2889f0fcc34f789`。

## 测试门禁

以下结果是宿主 `tests/python/goo_ctest_runner.py` 直接调用 CI Windows 产物得到的；`GPU/headless EEVEE` 探测为 `True`，不是 skip。

| 测试 | 结果 | 耗时 | 检查数 |
| --- | --- | --- | --- |
| `goo_screenspace` | pass | 27.1 s | 5 |
| `goo_bsdf_light_groups` | pass（6 cases） | 22.4 s | 26 |
| `goo_bsdf_light_groups_cases` | pass（7 cases） | 41.4 s | 30 |
| `goo_light_groups` | pass（20 cases） | 105.2 s | 24 |
| `goo_migration_fixtures`（Blender 模式） | pass | — | 26 |

`goo_light_groups` 覆盖 `api`、16 个 inherit/shadow case、`reopen`、`append`、`link` 和两个 ordinary case；全部 exit `0`。

修复的测试运行时缺陷：`goo_ctest_runner.py` 在切换 working directory 后仍把调用方传入的相对 `--blender`/`--startup` 交给子进程，导致 `FileNotFoundError`。现在在解析参数时立即 `resolve()` 为绝对路径。

## macOS runner 状态

macOS 没有构建失败记录；失败原因是托管 runner 拿不到机器：

| run | 目标 | 结果 |
| --- | --- | --- |
| `45384517223` | `macos-15` | job 排队约 15 分钟后 `cancelled` |
| `45408737664` | `macos-26` | 单 mac job 排队 15 分钟后 `cancelled` |
| `45411962590` | `macos-15` 单 mac job | 14:14:57 入队，14:29:58 `cancelled` |
| `45411844901` | `macos-14` | 1 秒内 `cancelled`（镜像已不可用） |

macOS 的源码编译成功证据来自 `45344327291` 的 `Build release (macOS)` 步骤；该 run 后续仅因 artifact 上传路径错误失败。当前没有 macOS artifact，也没有在 macOS 上实际启动/渲染。

## 仍未验证（当前状态）

- 完整 19 行驱动矩阵：目前只有 `win11-intel` 与 `debian-13-nvidia-gtx1660` 两行有真实
  硬件结果；其余仍是 descriptor。
- 真实崩溃 dump 采集；crashmon 目前只有单元测试与 Windows 启动自测。
- Windows 代码签名、安装包/更新/回滚。
- macOS 已不在交付范围；本文中的 macOS runner 记录仅作历史，不再作为验收项。

Windows 本地 `ctest -L goo` 已真实执行（7 pass、1 skip），不再列为未验证；详见
[evidence-windows-local.md](evidence-windows-local.md)。在这些剩余项补齐前，不应把该分支
标记为“完整稳定生产版”。
