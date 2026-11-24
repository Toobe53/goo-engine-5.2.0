# 生产化工程索引（Production Hardening）

本目录记录 Goo Engine 5.2 生产分支的构建、测试、崩溃监控、驱动矩阵、发布与长期维护方案。它是**工程骨架与门禁**，不是“已经稳定生产”的声明；真实的三平台构建、GPU/驱动实测、签名/公证与崩溃 dump 仍必须由对应硬件和密钥持有人完成。

## 文档

- [构建与 CI](../../tools/ci/README.md)：三平台构建入口、触发条件、产物和本地前置探测。
- [测试门禁](../../tests/python/README-goo-tests.md)：CTest 注册、GPU 探测、迁移 fixture、黄金场景与 skip 语义。
- [崩溃监控](crash-monitoring.md)：采集、脱敏、bundle 与上传计划。
- [驱动矩阵](driver-matrix.md)：18 个环境、门禁/nightly 分层、执行器和报告格式。
- [发布工程](release.md)：打包、SHA256SUMS、manifest、签名/公证占位与发布后复验。
- [长期维护](maintenance.md)：驱动季度复核、上游跟进、triage、回滚演练与责任人占位。

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
```

## 当前验证边界

- 已做：Python 语法检查、workflow 静态校验、矩阵/迁移/黄金 manifest 生成、崩溃监控 unittest、发布合成产物 dry-run/apply/verify。
- 未做：真实 CMake 构建、三平台 CI 首跑、EEVEE GPU 渲染、真实 GPU/驱动矩阵、真实崩溃 dump、Windows 签名、macOS notarization。
- 在完成这些之前，不应把本分支产物标记为“稳定生产版”。

生产分支：`production/5.2`。构建 workflow：`.github/workflows/production-build.yml`（PR 需 `ci:build` 标签才会进入重型构建）。
