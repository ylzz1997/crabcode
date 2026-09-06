# Windows 兼容性修复记录

日期：2026-09-06。基于 `76a495cf518335c4e322a9e9b5dc188be7f6e4d6`，对应本轮审计的 H1–H7、M1–M9、L1–L2。

## 处理结果

| 审计项 | 修改 | 验证与边界 |
| --- | --- | --- |
| H1 安装包路径逃逸 | ZIP 成员按可移植相对路径校验，拒绝盘符、根路径、UNC、反斜杠、上级目录、ADS 和 Windows 保留名称；写入前验证目标仍在解压目录内 | 危险名称拒绝测试及正常文件校验、解压测试通过 |
| H2 批处理参数被解释为命令 | 完整参数组装后再检查；识别标准 npm Node cmd-shim 并直接运行 Node/JS；其他 `.cmd/.bat` 遇到 Shell 特殊字符拒绝执行 | 真实 Windows 批处理及 Node 参数往返测试通过；自定义批处理不保证任意字符可传递，需配置原生程序入口 |
| H3 快照回滚改变字节 | 备份原文件字节，恢复时直接写回字节 | LF、混合换行、UTF-8 BOM、GBK 原始字节回滚通过；无法补回历史备份已丢失的信息 |
| H4 Write 读取失败仍覆盖 | 读取或解码旧文件失败时明确拒绝覆盖 | GBK 文件保持原始内容；未新增自动转码行为 |
| H5 凭据存储使用 mock | 显式启用 Windows/macOS/Linux 原生 keyring 特性并更新 Cargo.lock | Windows Credential Manager 写入、跨 Entry 读取、删除测试通过；macOS/Linux 原生后端尚未本机实测 |
| H6 LSP/DAP 工具发现忽略环境 | 使用启动环境的 PATH/PATHEXT，处理相对 cwd 和 Windows 环境变量大小写；找不到时不回退误用父环境程序 | 受控 PATH、PATHEXT、相对路径和环境键覆盖测试通过 |
| H7 VS Code 缺少 py 发现 | 增加 `py -3` 探测，取得实际解释器路径后验证版本；补充 CONDA_EXE 发现 | 转译真实模块后用受控进程探测测试：py-only、空格路径、版本拒绝、配置优先、非 Windows 分支 |
| M1 PowerShell 阻止 npm.ps1 | **保守缓解**：工具提示明确使用 npm.cmd/npx.cmd，不更改系统执行策略、不静默改写用户命令 | 进程级 Restricted 策略下 npm.cmd 测试通过；直接输入 npm 仍可能受到系统策略限制 |
| M2 等价 LSP URI 不匹配 | 解码一次后统一 Windows 路径身份，修复旧版 Python 对百分号编码盘符的转换 | 实际诊断通知/等待、盘符大小写、UNC URI、百分号不重复解码测试通过 |
| M3 CLI 吞掉反斜杠 | Windows slash-command 参数解析保留反斜杠，POSIX 保持 shlex 原有行为 | 含空格、带尾部分隔符的引号路径、UNC 和 POSIX 转义测试通过 |
| M4 搜索目录过滤失配 | 过滤时统一分隔符及 Windows 大小写，并按完整目录边界匹配 | 从真实搜索方法提取 AST 执行测试；兼容旧索引反斜杠路径，不要求迁移索引 |
| M5 Git 引号破坏中文文件名 | 使用 `git ls-files -z` 和 NUL 分隔，不 strip 文件名 | 临时 Git 仓库中的中文、空格和前导空格名称测试通过 |
| M6 取消及提前退出遗留子进程 | Windows 使用 kill-on-close Job Object 监督工具进程；Hook/Debugger 补充取消清理；CLI Gateway 也持有生命周期 Job | 父启动器提前退出、直接杀监督器、Hook 取消、正常 taskkill、stdin/stdout/env/退出码测试通过 |
| M7 Lint 输出乱码 | 复用具有 Windows 活动代码页回退的解码函数 | 真子进程写出 GBK 字节、受控 cp936 解码通过 |
| M8 LibreOffice 不在 PATH | 检查标准 Windows 安装目录，支持显式可执行文件路径 | 构造安装目录及显式路径发现测试通过；本轮未安装 LibreOffice 或执行完整 Office→PDF 转换 |
| M9 原子替换遇占用失败 | **有限缓解**：仅 Windows 共享/访问错误短暂重试，仍保留原子替换和原安装回退 | 实际不允许共享删除的句柄释放后替换成功；发布失败后旧安装恢复测试通过；长期占用仍会报错，不强删运行中资源 |
| L1 Windows 文件名显示 | 同时识别正反斜杠 | TypeScript 检查与扩展构建通过 |
| L2 缺少 Windows 功能 CI | 新增三系统 Python 测试矩阵，以及 Windows 前端、扩展、Rust 和凭据测试 | 工作流已添加；需提交并在 GitHub 上运行后才能确认远程矩阵通过 |

## 行为说明

- Windows 工具进程由监督器拥有。启动器退出、工具取消/超时或 CLI Gateway 退出时，其受管理的后代进程会结束。需要持续运行的服务应通过 Monitor 保持前台命令存活，不应依赖启动器退出后留下孤儿进程。
- 监督器仅依赖 Python 标准库，以隔离模式运行，不要求子工具环境包含 CrabCode 的 PYTHONPATH。POSIX 命令不会增加监督器。
- 普通 `.cmd/.bat` 的路径或参数含 `& | < > ^ % ! " ( )`、换行时返回明确错误。标准 npm Node cmd-shim 可转换成原生 Node 调用；自定义 shim 不会擅自跳过其初始化逻辑。建议配置 `node.exe` 和实际 JS 入口以支持复杂参数。
- Windows 的 `/goal`、`/team`、`/schedule` 使用单/双引号分组参数，反斜杠为字面字符；含双引号的 JSON 可放在单引号内。这里不是 Shell，不支持 POSIX 反斜杠转义。
- `CRABCODE_LIBREOFFICE_PATH` 指定完整的 `soffice.exe` 路径，例如 `C:\Program Files\LibreOffice\program\soffice.exe`。显式配置无效时返回不可用，不悄悄选择其他安装。
- Windows 原子替换最多尝试 5 次，重试间隔 50、100、200、400 ms。长时间占用需先关闭占用程序/停止相关文档任务再重试。

## 本机验证

Windows x64、Python 3.12.14、Node 24.18.0；桌面依赖已按现有锁文件重新安装，Vitest 3.2.7。

- `python -B -m unittest discover -s tests -p "test_windows*.py" -q`：48 项通过，含 26 项新增回归；需要正常进程权限的测试在沙箱外复验。
- Desktop `vitest run`：15 个文件、169 项通过。
- VS Code `node --test tests/gatewayManager.test.cjs`：4 项通过。
- Desktop 与 VS Code `tsc --noEmit`：通过。
- Desktop Vite production build、VS Code esbuild production build：通过。
- Rust `cargo check`、`cargo test --locked --offline`：通过；5 项常规测试通过。
- Rust 原生凭据测试显式运行：1 项通过，专用假凭据已删除。
- 五个 Python 包的 `compileall`、`git diff --check`：通过。

既有共享文件锁测试有未关闭 stdout 的 ResourceWarning；Vite 提示部分资源包大于 500 kB。这两项不导致上述测试/构建失败，本次未扩展到无关清理。

GitHub Windows runner 的 TEMP 使用 `RUNNER~1` 等 8.3 短路径别名，工具发现会将其展开为完整路径。路径发现测试已改用 `samefile()` 校验文件身份，并增加使用真实短路径 cwd 的回归，避免将同一文件的不同路径写法误判为失败。

## 尚未宣称完成的验收

未做真实网络 UNC、禁用 long-path 策略的全新机器、Windows ARM64/x86、企业防火墙、完整 BabelDOC/搜索模型下载、运行中引擎升级，以及重启桌面后的全链路 UI 验收。代码和自动化覆盖已落地不等于这些环境全部验证通过。

本轮改动已提交并推送到 `fix/windows-compat-audit`，PR 已提交；未将新构建替换到用户正在运行的桌面实例。
