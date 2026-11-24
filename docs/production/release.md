# 发布工程（Release Engineering）

本文档描述 Goo Engine 5.2 生产分支的发布打包、校验和签名流程。目标是：**默认安全、可重复、哈希覆盖最终字节**。

> 状态（2026-11）：脚本与流程骨架已落地，并在 Windows 上用合成产物跑通 dry-run / `--apply` / 校验/ 篡改检测。**没有真实代码签名证书、没有在 macOS 上执行 codesign/notarytool、没有用真实 Goo 构建产物验证。** 签名与公证相关命令必须由持有密钥的发布负责人在真实环境复核。

## 1. 原则

1. **默认 dry-run**：`build_release.py` 不带 `--apply` 不写文件；两个签名脚本不带 `-Execute`/`--execute` 只打印命令。
2. **先签名，后计算哈希**：`SHA256SUMS` 必须覆盖签名后的最终字节。先跑签名脚本，再跑 `build_release.py --apply`。
3. **校验和与 manifest 互相约束**：所有产物必须同时出现在 `SHA256SUMS` 和 `release-manifest.json` 中，且哈希一致。
4. **占位不是签名**：manifest 的 `signing` 字段只是声明/占位，不能证明产物已签。签名真伪用平台工具验证。
5. **只追加，不覆盖**：发布目录和已上传的 release 资产不做原地替换；修正必须发新版本号。

## 2. 流水线

```text
构建产物（各平台）
   │
   ├─(可选) sign_windows.ps1 -Execute / sign_macos.sh --execute
   │        └─ 签名/公证后的字节
   ▼
build_release.py --apply
   ├─ 复制产物到 dist/<name>-<version>/
   ├─ 生成 SHA256SUMS
   └─ 生成 release-manifest.json
   ▼
verify_release.py
   ├─ 结构 / 哈希 / 覆盖一致性 / 签名占位
   └─ (可选) --require-signed --check-signatures
   ▼
上传 / 发布
   ▼
发布后再下载一份，对下载物重跑 verify_release.py
```

建议把“构建”和“签名”放在受控机器上，把“打包 + 校验”放在可复现的干净环境；不要把签名密钥放在 GPU runner 上。

## 3. 产物约定

- 输入：一个或多个 `--artifacts DIR`，可重复传入；每个目录会被递归扫描。
- 排除：`SHA256SUMS`、`release-manifest.json`、`.DS_Store`、`Thumbs.db`、`*.tmp`、`*.part`，以及输出目录自身。
- 重名：同名且大小相同视为重复，保留第一个并警告；同名但大小不同直接报错（避免静默覆盖）。
- 输出默认 `<repo>/dist/<name>-<version>/`（可用 `--out` 覆盖）。
- 建议命名包含版本、平台、架构，例如 `goo-5.2.0-windows-x64.zip`，便于人工识别；脚本不强制命名格式。

## 4. `build_release.py`

### 4.1 用法

```bash
# 只看计划（默认即 dry-run）
python tools/release/build_release.py --version 5.2.0 --channel rc \
  --artifacts dist/staging/windows --artifacts dist/staging/linux --artifacts dist/staging/macos

# 真正生成发布目录
python tools/release/build_release.py --version 5.2.0 --channel production \
  --artifacts dist/staging/windows --artifacts dist/staging/linux --artifacts dist/staging/macos \
  --commit "$GITHUB_SHA" --build-id "$GITHUB_RUN_ID" --apply

# 声明签名状态（仅记录，不执行签名）
python tools/release/build_release.py --version 5.2.0 --windows-signed --macos-signed --apply ...
```

常用参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--version` | 必填 | 发布版本，如 `5.2.0` |
| `--name` | `Goo Engine` | 产品名 |
| `--channel` | `production` | `production`/`beta`/`rc`/`nightly` |
| `--artifacts DIR` | 必填，可重复 | 待打包产物目录 |
| `--out DIR` | `<repo>/dist/<name>-<version>` | 输出目录 |
| `--commit` | `GITHUB_SHA` 或 `unknown` | 源码提交 |
| `--branch` | `GITHUB_REF_NAME` | 源码分支 |
| `--build-id` | `GITHUB_RUN_ID` 或 UTC 时间戳 | 构建标识 |
| `--builder` | `GITHUB_ACTOR` 或本地用户 | 构建人/系统 |
| `--dependency NAME=VERSION` | 可重复 | 声明依赖 |
| `--dependencies-file JSON` | 无 | JSON 列表形式的依赖 |
| `--driver-matrix PATH` | `tools/driver_matrix/matrix.json` | 从矩阵提取驱动要求；传空串禁用 |
| `--windows-signed` / `--macos-signed` | 否 | 声明对应平台已签（仅写入 manifest） |
| `--apply` | 否 | 真正写文件；`--dry-run` 可强制回 dry-run |

`--dependency`/`--dependencies-file` 之外，脚本总会写入 Python 运行时版本，便于回溯构建环境。

### 4.2 输出

`SHA256SUMS`（LF 换行，`hash  filename` 两空格）：

```text
1f20b8cb...  goo-5.2.0-windows-x64.zip
f548f2a2...  goo-5.2.0-linux-x64.tar.gz
```

`release-manifest.json` 关键字段：

| 字段 | 说明 |
| --- | --- |
| `schema_version` | manifest 结构版本，当前 `1.0` |
| `project` | `name`、`version`、`channel` |
| `source` | `commit`、`commit_short`、`branch` |
| `build` | `id`、`timestamp`、`host`、`platform`、`arch`、`python`、`builder` |
| `artifacts[]` | `path`、`size`、`sha256` |
| `checksums` | `algorithm: sha256`、`file: SHA256SUMS`、`count` |
| `dependencies` | 名称到版本的映射 |
| `driver_requirements[]` | 从驱动矩阵提取的平台/GPU/最低驱动/tier |
| `signing` | 各平台签名状态与身份（来自环境变量，仅占位） |
| `limitations` | 发布覆盖范围的诚实说明 |
| `warnings` | dry-run/打包期间的告警 |

复制产物后会重新计算哈希并与源文件比对，不一致则失败，避免复制损坏或哈希漂移。

### 4.3 退出码

- `0`：成功（dry-run 也算成功）。
- `1`：写文件失败、复制后哈希不一致等。
- `2`：参数错误、产物目录不存在、扫描/重名冲突等。驱动矩阵缺失或不可读只记为 warning，`driver_requirements` 为空，不改变退出码。

## 5. Windows 签名 `sign_windows.ps1`

默认 **dry-run**，只打印 `signtool` 命令；`-Execute` 才真正签名。

```powershell
# 预览
pwsh -File tools/release/sign_windows.ps1 -ArtifactsDir dist\5.2.0

# 真正签名（证书与密码只从环境读）
$env:GOO_WIN_CERT_PATH = 'C:\secrets\goo.pfx'
$env:GOO_WIN_CERT_PASSWORD = '***'
pwsh -File tools/release/sign_windows.ps1 -ArtifactsDir dist\5.2.0 -Execute
```

参数/环境变量：

| 名称 | 默认 | 说明 |
| --- | --- | --- |
| `-ArtifactsDir` | 必填 | 递归扫描的产物目录 |
| `-CertificatePath` / `GOO_WIN_CERT_PATH` | 空 | `.pfx` 路径 |
| `-CertificatePasswordEnv` | `GOO_WIN_CERT_PASSWORD` | 密码所在环境变量名；密码不落盘、不回显 |
| `-TimestampUrl` | `http://timestamp.digicert.com` | RFC3161 时间戳；传空串禁用 |
| `-SigntoolPath` / `SIGTOOL_PATH` | 自动查找 | Windows SDK 的 `signtool.exe` |
| `-Execute` | 关 | 真正签名 |

行为：

- 扫描 `.exe`、`.msi`、`.dll`、`.sys`；
- 每个文件 `signtool sign /fd SHA256 /f <pfx> /p <pwd> /tr <url> /td SHA256`，随后 `signtool verify /pa /q`；
- dry-run 不要求证书存在，打印命令后退出 `0`；
- `-Execute` 缺少 signtool/证书/密码退出 `2`；任一文件签名或验签失败退出 `1`。

注意：`signtool` 需要 Windows SDK；脚本不会自动安装。证书密码用环境变量传递，CI 中应使用 secret，不要写进命令行历史。

## 6. macOS 签名与公证 `sign_macos.sh`

默认 **dry-run**；必须在 macOS 上执行真实签名。

```bash
# 预览
tools/release/sign_macos.sh --artifacts dist/5.2.0

# 真正签名 + 公证（如配置了凭据）
export GOO_MAC_SIGN_IDENTITY='Developer ID Application: Example (TEAMID)'
export GOO_NOTARY_PROFILE='goo-notary'
tools/release/sign_macos.sh --artifacts dist/5.2.0 --execute
```

参数/环境变量：

| 名称 | 说明 |
| --- | --- |
| `--artifacts DIR` | 必填；扫描 `.app`、`.dmg`、`.pkg`、`.dylib`、`.so` |
| `--execute` | 真正签名；缺省 dry-run |
| `GOO_MAC_SIGN_IDENTITY` | codesign 身份，`--execute` 必填 |
| `GOO_MAC_KEYCHAIN` | 可选，`codesign --keychain` |
| `GOO_NOTARY_PROFILE` | 可选，`notarytool --keychain-profile` |
| `APPLE_ID` / `TEAM_ID` / `APP_PASSWORD` | 无 profile 时的公证凭据回退 |

行为：

- `.app`：`codesign --force --timestamp --options runtime --deep --sign ...`，再 `--verify --deep --strict`；
- `.dmg`/`.pkg`/`.dylib`/`.so`：`codesign --force --timestamp --sign ...`，再 `--verify --strict`；
- 公证：`notarytool submit ... --wait`，`.dmg`/`.pkg` 成功后 `stapler staple`；`.zip` 可公证但不可 staple；
- 没有公证凭据时只警告并跳过公证（签名仍执行）；
- 非 macOS 上 `--execute` 直接退出 `2`；某文件签名/验签/公证失败退出 `1`。

前置条件：已安装 Xcode Command Line Tools、身份已在登录 keychain 中、公证凭据已存入 notarytool profile。脚本不会创建/导入 keychain。

## 7. `verify_release.py`

```bash
# 完整性 + manifest + 签名占位
python tools/release/verify_release.py --release-dir dist/5.2.0

# 要求已签名（manifest 声明为 signed 且所有平台 status=signed）
python tools/release/verify_release.py --release-dir dist/5.2.0 --require-signed

# 额外调用平台工具验证真实签名
python tools/release/verify_release.py --release-dir dist/5.2.0 --check-signatures
```

检查项：

1. manifest 可读、`schema_version` 受支持、必填字段齐全且类型正确；
2. `SHA256SUMS` 每行格式合法（64 位 hex + 两空格 + 文件名），无重复；
3. `SHA256SUMS` 与 manifest 的产物集合双向一致，`checksums.count` 匹配；
4. 每个产物存在、大小匹配、SHA256 匹配，且两处哈希一致；
5. 路径安全：拒绝绝对路径和 `..` 逃逸；
6. 签名占位状态：`signed` 时所有平台必须 `status=signed`；`--require-signed` 时非 `signed` 失败；
7. `--check-signatures` 时：Windows 对 `.exe/.msi/.dll/.sys` 跑 `signtool verify /pa /q`；macOS 对 `.pkg` 用 `pkgutil --check-signature`、`.dmg` 用 `spctl`、`.dylib/.so` 用 `codesign --verify --strict`；其他平台只警告。

退出码：

- `0`：全部通过（warning 不算失败）；
- `1`：存在失败项（哈希不匹配、缺文件、要求签名但未签、真实签名验证失败等）；
- `2`：manifest 无法读取或参数错误。

> `verify_release.py` 不执行签名。`--check-signatures` 只是复核；没有证书的机器无法用它“补签”。

## 8. 发布检查清单

1. 版本号、channel、目标 commit 已确认。
2. 各平台产物齐全，Windows/macOS 已按策略签名；签名在打包前完成。
3. `build_release.py` 先 dry-run，检查产物列表、哈希、driver requirements 数量。
4. `--apply` 生成 `dist/<name>-<version>/`。
5. `verify_release.py` 通过；需要签名证据时加 `--require-signed`，有平台工具时加 `--check-signatures`。
6. 上传后重新下载一份，对下载物再跑 `verify_release.py`（防止传输损坏/对象存储改写）。
7. 记录 manifest 的 `source.commit`、`build.id`、`signing.status` 到发布记录。
8. 发布公告中如实写明未覆盖平台/驱动（引用 manifest 的 `limitations`）。

## 9. 回滚

发布回滚的触发条件、步骤与责任人见 `maintenance.md` 第 5 节。发布工程侧的硬规则：**不覆盖已发布资产、不修改旧版本的 SHA256SUMS**；需要修正时发布新补丁版本，并保留旧版本 manifest 供审计。

## 10. 已知未验证项（诚实清单）

- 未持有真实 Windows 代码签名证书，未执行 `sign_windows.ps1 -Execute`；仅验证了 dry-run、缺少 signtool 时退出 `2`、以及 fake 二进制的命令打印。
- 未在 macOS 上执行 `sign_macos.sh`；仅在 Windows 的 Git Bash 下做了 `bash -n` 语法检查与 dry-run，`--execute` 因非 macOS 正确退出 `2`。codesign/notarytool/stapler 的真实调用未验证。
- 未用真实 Goo 产物验证 `build_release.py`；测试使用临时合成文件，验证了 dry-run 不写盘、`--apply` 写出 SHA256SUMS/manifest、复制后哈希比对、`verify_release.py` 通过/篡改失败/`--require-signed` 失败/缺失 manifest 退出 `2`。
- 未验证 `--check-signatures` 对真实已签二进制的结果（本机无 signtool、无已签样本）。
- `driver-matrix.yml.example` 未在 GitHub Actions 实际启用。

## 11. 责任人（占位）

| 职责 | 负责人 | 备注 |
| --- | --- | --- |
| 发布工程 owner | TBD | 维护本文档与发布脚本 |
| Windows 签名 | TBD | 持有 EV/OV 证书与时间戳访问 |
| macOS 签名/公证 | TBD | 持有 Developer ID 与 notarytool 凭据 |
| 发布批准 | TBD | 决定 gate 例外与回滚 |
| 发布记录/合规 | TBD | 归档 manifest、校验和与签名证据 |
