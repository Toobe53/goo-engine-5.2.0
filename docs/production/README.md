# 生产化工程索引（Production Hardening）

本目录记录 Goo Engine 5.2 生产分支在 **Windows + Linux** 上的构建、测试、崩溃监控、驱动矩阵、发布与长期维护方案。macOS 已按用户要求移出交付范围。当前已有双平台本地构建、运行时/测试证据、真实崩溃 dump 采集、真实发布 bundle，以及 Windows 安装包和安装/更新/回滚实测，但**仍不是“已经稳定生产”的声明**：完整 15 行 Windows/Linux 驱动矩阵（当前仅 2 行有真实硬件结果）、Windows 正式代码签名、Linux 正式 GPG 密钥签名、跨版本升级与自动回滚仍必须由对应硬件和密钥持有人完成。Windows 自签名 Authenticode 演示只证明签名写入/解析路径，根证书不受信任，不能当作正式签名。

## 文档

- [构建与 CI](../../tools/ci/README.md)：Windows/Linux 构建入口、触发条件、产物和本地前置探测。
- [测试门禁](../../tests/python/README-goo-tests.md)：CTest 注册、GPU 探测、迁移 fixture、黄金场景与 skip 语义。
- [崩溃监控](crash-monitoring.md)：采集、脱敏、bundle 与上传计划。
- [驱动矩阵](driver-matrix.md)：19 个环境（Windows/Linux 15 行交付，macOS 4 行历史）、门禁/nightly 分层、执行器和报告格式。
- [发布工程](release.md)：打包、SHA256SUMS、manifest、签名/公证占位与发布后复验。
- [长期维护](maintenance.md)：驱动季度复核、上游跟进、triage、回滚演练与责任人占位。
- [验证证据](evidence.md)：CI 构建、Windows 运行时与测试门禁。
- [Linux 本地构建证据](evidence-linux-local.md)：Debian 13 + GTX 1660 的真实构建、EEVEE 渲染与 Goo 测试。
- [Windows 本地构建证据](evidence-windows-local.md)：Windows 11 + Iris Xe 的 lite 构建、干净安装、CTest 与驱动矩阵。
- [Windows 安装包/更新/回滚证据](evidence-windows-installer.md)：Inno Setup 安装、卸载、zip 树更新与回滚实测。
- [Windows 签名 dry-run 证据](evidence/windows-signing-dryrun/README.md)、[Windows 自签名演示](evidence/windows-signing-selfsigned/README.md) 与 [Linux GPG 工具测试证据](evidence/linux-gpg-signing-test/README.md)：签名脚本路径与演示验证，不代表正式签名或受信任发布。

## 关键入口

```text
# 本机前置条件探测（不构建；NOT READY 时 exit 1）
python tools/ci/build_probe.py --text

# workflow 静态校验
python tools/ci/validate_workflow.py --quiet

# 驱动矩阵定义校验 / 本机探测
python tools/driver_matrix/run_matrix.py --validate-only
python tools/driver_matrix/detect.py --compact

# 崩溃监控自测（必须从 tools/crashmon 运行）
cd tools/crashmon
python -m unittest discover -s tests -v
cd ../..

# 发布目录 dry-run / 写入 / 校验
python tools/release/build_release.py --version 5.2.1 --artifacts <dir> --out <dir> --dry-run
python tools/release/verify_release.py --release-dir <dir>

# 本地发布门禁（7 阶段；--with-ctest 追加 Goo CTest）
python tools/ci/local_gate.py --release-dir dist/goo-engine-5.2.1
```

## 当前验证边界

- 已做：Linux 本地 release 构建（Debian 13 / GTX 1660，`7450/7450` 目标、安装后可启动、打包后不设 `LD_LIBRARY_PATH` 仍可启动）；Windows 本地 lite 构建（`7050/7050`，干净 `cmake --install` 后可启动）；两平台 13 个 Goo 节点、EEVEE 渲染、shadow-ID、Goo 测试与迁移 fixture 实测；Windows `ctest -L goo` 8 项（7 pass、1 skip）；Windows/Linux 两行真实驱动矩阵各 4/4 通过；真实 Windows minidump 采集、复制、SHA-256、manifest/report 校验；`crashmon run` 监督受控原生崩溃并生成 `CRASH` bundle；真实 Windows/Linux 发布包、`SHA256SUMS`、`release-manifest.json` 与 `verify_release.py` PASS；Windows zip 与 Linux tar 各两次重建同 SHA；Windows 安装包静默安装/卸载、注册表/快捷方式、zip 树更新与回滚实测；本地门禁 7 阶段全过；Linux GPG detached-sign/verify 用临时密钥实测；Windows 自签名 Authenticode 演示（签名可解析、根不受信任、PFX/证书已删）；workflow/矩阵静态校验。
- macOS：已从当前开发范围移除；不再以 macOS 构建或 notarization 作为验收项。
- 未做：剩余 13 行 Windows/Linux 驱动矩阵（当前只有 `win11-intel` 与 `debian-13-nvidia-gtx1660` 两行有真实硬件结果）、Windows 正式证书签名、Linux 正式发布 GPG 密钥签名、跨版本升级/自动回滚、真实产品故障的 `crashmon run` 监督与崩溃 dump 符号化。
- 在完成这些之前，不应把本分支产物标记为“稳定生产版”。详见 [evidence-windows-local.md](evidence-windows-local.md)、[evidence-linux-local.md](evidence-linux-local.md)、[evidence-windows-installer.md](evidence-windows-installer.md) 与 [evidence.md](evidence.md)。

生产分支：`production/5.2`。构建 workflow：`.github/workflows/production-build.yml`（PR 需 `ci:build` 标签才会进入重型构建）。
