# Goo Engine 驱动矩阵执行报告

- 生成时间：2026-11-24T17:53:27Z
- 矩阵文件：`C:\Users\zbq\Documents\deepseek-harness\default-workspace\goo-engine-5.2.0\tools\driver_matrix\matrix.json`（schema 1.0）
- 项目：Goo Engine 5.2.0（production/5.2）
- 判定：**PASS**

## 主机

| 项目 | 值 |
| --- | --- |
| 系统 | Windows 11 10.0.26200（x64） |
| 显示服务 | windows |
| CI | not detected |
| GPU | OrayIddDriver Device（unknown，virtual，驱动 17.50.19.949）<br>Intel(R) Iris(R) Xe Graphics（intel，integrated，驱动 31.0.101.5590） |
| 后端 | opengl: available<br>vulkan: available<br>metal: unavailable |

## 矩阵筛选

匹配条目：`win11-intel`

| 跳过条目 | 级别 | 原因 |
| --- | --- | --- |
| `win10-nvidia` | gate | filtered by --entry |
| `win10-amd` | gate | filtered by --entry |
| `win10-intel-igpu` | nightly | filtered by --entry |
| `win11-nvidia` | gate | filtered by --entry |
| `win11-amd` | gate | filtered by --entry |
| `ubuntu-22.04-nvidia` | gate | filtered by --entry |
| `ubuntu-22.04-amd` | gate | filtered by --entry |
| `ubuntu-22.04-intel` | nightly | filtered by --entry |
| `ubuntu-24.04-nvidia` | gate | filtered by --entry |
| `debian-13-nvidia-gtx1660` | gate | filtered by --entry |
| `ubuntu-24.04-amd` | gate | filtered by --entry |
| `ubuntu-24.04-intel` | nightly | filtered by --entry |
| `ubuntu-24.04-nvidia-wayland` | nightly | filtered by --entry |
| `ubuntu-24.04-amd-wayland` | nightly | filtered by --entry |
| `macos-14-apple-metal` | gate | filtered by --entry |
| `macos-15-apple-metal` | gate | filtered by --entry |
| `macos-14-apple-opengl` | advisory | filtered by --entry |
| `macos-15-intel-amd-metal` | advisory | filtered by --entry |

## 驱动符合性

| 条目 | 主机驱动 | 最低要求 | 结论 |
| --- | --- | --- | --- |
| `win11-intel` | 31.0.101.5590 | Intel Windows 31.0.101.5186 | met |

## 执行结果

| 条目 | 测试集 | 后端 | 必需 | 状态 | 退出码 | 耗时(s) |
| --- | --- | --- | --- | --- | --- | --- |
| `win11-intel` | smoke-opengl | opengl | yes | **passed** | 0 | 10.201 |
| `win11-intel` | smoke-vulkan | vulkan | yes | **passed** | 0 | 1.17 |
| `win11-intel` | render-eevee | opengl | no | **passed** | 0 | 4.496 |
| `win11-intel` | render-eevee | vulkan | no | **passed** | 0 | 5.762 |

## 说明

- 本报告只代表当前主机；矩阵中未匹配的条目必须由对应的自托管 GPU runner 执行。
- GitHub 托管 runner 不提供可用 GPU，不能代替本矩阵（见 `docs/production/driver-matrix.md`）。
- 驱动符合性默认只记录不阻断；使用 `--require-min-driver` 可将低于基线的驱动视为失败。
- 任何 failed/error 都会让退出码非零；`--strict` 会额外把 skipped（如缺少二进制）也视为失败。
