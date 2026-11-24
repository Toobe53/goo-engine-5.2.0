# 真实 Windows 崩溃 dump 采集证据

本目录是 `crashmon collect` 对一份**真实 Windows minidump** 的采集结果，不是合成文件。

## 来源

| 项目 | 值 |
| --- | --- |
| dump | `C:\Users\zbq\AppData\Local\CrashDumps\blender.exe.11652.dmp` |
| 大小 | 135,681,994 B（129.4 MiB） |
| 文件头 | `4D 44 4D 50`（`MDMP`） |
| 写入时间 | 2026-11-24 18:34:56 |
| SHA-256 | `8b1580476ab1e153cbe3a5efdd1050df5c90aba81245052c296cc99386be7452` |
| 崩溃进程 | `blender.exe` 5.2.1.0（build hash `6b05648f`） |
| 出错模块 | `tbbmalloc.dll` 2022.3.0.0，偏移 `0x6829` |
| 异常 | `0xC0000005` ACCESS_VIOLATION |
| WER | Application Error Event ID 1000，2026-11-24 18:34:51 |

WER 事件中的 faulting 路径是旧的 CI 产物
`C:\Users\zbq\Downloads\goo-engine-windows-x64\blender.exe`，不是当前本地构建。

## 采集方式

```powershell
python tools/crashmon/crashmon_cli.py collect `
  --output-dir _work/crashmon-real `
  --crash-dir _work/crashmon-input `
  --process-name blender `
  --exit-code-hex 0xC0000005 `
  --report `
  --max-artifact-bytes 200000000 `
  --max-total-artifact-bytes 200000000 `
  --max-artifacts 1
```

`--crash-dir` 是仅含该 dump 硬链接的目录，避免同时复制同目录下其他 100 MiB 级 dump。
退出码由 WER 事件人工提供，**不是 crashmon 监督到的子进程退出码**；`manifest.json`
的 `run.exit` 和 `report.md` 的 Notes 都记录了这一点。

## 本目录内容

- `manifest.json`：schema `crashmon.bundle/1`，10,503 B；经 `report` 校验 0 个 schema 问题。
- `report.md`：6,837 B，`Verdict: CRASH`；记录 5 个发现产物、1 个复制产物、SHA-256、
  平台能力、脱敏计数和限制。

**dump 本身没有提交**：它 129.4 MiB，且二进制 minidump 可能包含进程内存、路径和用户
数据；它只保留在忽略的 `_work/crashmon-real/` 中，用于本地复核。

## 这次验证了什么

- 在真实 Windows 主机上发现 `%LOCALAPPDATA%\CrashDumps` 中的真实 Blender minidump，
  按进程名/时间窗过滤，复制到 bundle 并计算 SHA-256。
- 真实 `MDMP` 产物能进入 manifest/report，`collect` 退出 0，schema 校验通过。
- WER ReportArchive 目录和 110 KiB 级 `Report.wer` 也被发现并记录（本次因
  `--max-artifacts 1` 未复制）。

## 仍未验证

- 没有用 `crashmon run` 监督一次真实原生崩溃；退出码来自 WER 事件，不是 supervisor。
- 没有符号化，不能从 dump 重建调用栈；没有 PDB/build ID 匹配。
- 没有复制/解析真实 `Report.wer` 内容（只发现并记录了大小）。
- 没有真实 Linux core 或 `coredumpctl` 提取；Linux 侧仍是代码路径和单元测试。
