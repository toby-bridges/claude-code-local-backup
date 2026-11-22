# Claude Code Local Backup

**首版预览。** 可在普通终端离线执行；Codex 的实际识别与内置沙箱仍待验证。仅供 macOS 上的普通本机用户，需 Python 3.9+；无第三方 Python 依赖。与 Anthropic、OpenAI 无隶属关系。这不是迁移/还原工具、系统备份或凭据备份。

## 范围

| 代号 | 固定目录（相对你的 macOS 用户主目录） |
| --- | --- |
| `projects` | `.claude/projects`：CLI 本地会话资料，不代表原代码仓库 |
| `desktop-code` | `Library/Application Support/Claude-3p/claude-code-sessions` |
| `desktop-scratch` | `Library/Application Support/Claude/scratch-workspaces` |
| `desktop-agent` | `Library/Application Support/Claude/local-agent-mode-sessions` |

目录是候选版的固定白名单，不是对 Claude 未来版本存储格式的承诺。脚本按当前账户查找主目录，不接受自定义源/目标或 `HOME` 覆盖；`projects` 不支持自定义 `CLAUDE_CONFIG_DIR`。若运行环境没继承该变量，但你的 Claude 实际用的是自定义目录，不要备份 `projects`。不访问登录、诊断日志、浏览器 Cookie、凭据目录或云端历史，也不会试图修复、恢复或清理来源。白名单内出现已知此类目录名或文件名（如 `.env`、`credentials.json`、`Cookies`、`*.log`、`*.pem`）会阻止**整类**备份；不会列出命中的名称。普通文档、会话和代码可能内嵌密钥、日志、账号信息、个人资料或私有代码；脚本不解读内容，也不能识别或剔除这些文字。不能接受原样私有副本时，不选该类。

## 安装到 Codex

1. 先审阅本目录，尤其是 `SKILL.md`、`agents/openai.yaml` 和 `scripts/local_backup.py`。
2. 将整个 `claude-code-local-backup` 文件夹复制到 `~/.agents/skills/`（个人安装），使入口文件位于 `~/.agents/skills/claude-code-local-backup/SKILL.md`。不要覆盖已有同名 Skill；可先审阅两个版本。只在一个版本出现在 Codex 的技能搜索路径中时测试。不要把安装路径当作备份存储。
3. 在 Codex CLI 或 IDE 输入 `$claude-code-local-backup`，再指定要先预览的类别。如果 `/skills` 没找到它，重启 Codex 后重新检查。`agents/openai.yaml` 关闭隐式调用。格式/发现规则：[OpenAI Build skills](https://learn.chatgpt.com/docs/build-skills)。
4. 备份前保存工作，并自行关闭相关 Claude Desktop / Claude Code 写入进程。先看 **每一类** 的预览与数量，再明确批准要写入的类别。若 Codex 沙箱不允许访问，不改高权限、不关闭沙箱；可以自行在普通终端运行同一版本。

## 普通终端（不需要 Codex 或网络）

从本目录运行；以下以 `projects` 为例。每一类需各自重新预览、确认和校验。不用 `sudo`，不要把脚本的命令输出转发到别人看得到的地方。若不希望类别、计数等摘要进入 Codex 会话，用普通离线终端。

```bash
python3 -I scripts/local_backup.py plan --item projects
```

`PLAN` 给出文件数、逻辑字节数、子目录数和 `CONFIRM=` 值；它只读元数据并检查能否打开文件，不读文件正文，不写入目录。看完计划并确认需要备份后，将下面命令中的 `PLAN_TOKEN` 换成原样值。它是类别和元数据的指纹，不是授权、签名或对人工同意的技术证明。

```bash
python3 -I scripts/local_backup.py backup --item projects --confirm PLAN_TOKEN
```

成功须同时满足退出码 0、`BATCH=` 和 `BACKED_UP`。从此命令输出复制 16 位批次号，换掉下方的 `BATCH_ID`。

```bash
python3 -I scripts/local_backup.py verify --backup BATCH_ID
```

`verify` 只读该批次的副本和清单，不再访问来源；它必须返回 0 且输出 `VERIFIED`。副本路径是 `~/.claude-code-local-backup/<批次号>/copy`。每个成功类别都由你自己用 Finder 从 `copy` **再复制**一份认得的文件到私有本地目录，在受信任的本机查看器打开，不执行脚本，不共享名称/内容/截图；记录 `已打开 / 失败 / 未查`。没打开的类别不算人工验收完成。

| 结果 | 如何处理 |
| --- | --- |
| `SKIP` / 3，`MISSING_SOURCE`、`EMPTY_SOURCE` | 无目录与无普通文件分别记录；都不创建新批次。零字节文件仍是一份文件。 |
| `SKIP` / 3，`MISSING_BATCH` | 存储目录或该批次不存在。核对原先记录的批次号，不创建替代批次。 |
| `STOP` / 2，`PERMISSION_DENIED` 或存储权限/ACL 异常 | 报受阻；不扩大访问范围，不改权限、不提权，不把私有数据另存到不明位置。 |
| `STOP` / 2，链接、特殊文件、跨卷、云占位或范围外条目 | 整类受阻，不跟随或跳过条目偷做部分备份。 |
| `STOP` / 2，`PLAN_MISMATCH` 或 `TREE_CHANGED` | 停止写入；让写入进程退出后重新预览、再确认。 |
| `STOP` / 2，不完整、坏清单或坏副本 | 不计作备份。保留原件和残留的私有批次；脚本不清理、不覆盖。未以 0 和 `BACKED_UP` 结束的批次不得用作验收。 |

## 隐私、完整性与限制

- 源文件仅以只读方式打开。私有存储固定在账户主目录下：目录 `0700`、文件 `0600`、当前用户所有，不允许扩展 ACL、符号链接、硬链接文件或跨卷树。写前重扫计划；复制时检查元数据变化，复制后重新读源文件和副本比对 SHA‑256。普通文件数据与子目录结构复制过去，但不复制 ACL、扩展属性、资源叉、时间戳或执行权限。不是原子快照，不保证与同时写入的应用一致，也不能证明应用能导入。
- 脚本没有网络调用、遥测、登录、子进程或上传，不向模型返回正文、文件名、路径清单或每文件哈希。清单**在本机私有批次内**，包含相对文件名和哈希，也不能上传。运行中的脚本会读取文件字节进行复制和哈希；不把这些字节交给模型。Codex 仍可能把对话和工具摘要交给其常规服务。
- 私有权限不是加密，不防同账户下的其他程序或管理员，也不防同时改写清单和副本。SHA‑256 用来发现损坏或不一致，不验证发布者身份。脚本拒绝非本地卷和识别到的无数据占位文件，但无法检查所有第三方同步、索引或备份工具；请自行确保本机主目录和存储不被同步、共享或纳入公开仓库。不够空间或中断时可能留下不可用的私有批次。

## 测试及发布前审核

只用合成目录、合成文件和临时私有存储跑测试，详见 [验证记录](TESTING.md)。

本 Skill 的代码与随附说明采用 [MIT License](LICENSE)。该许可不涵盖用户的备份资料、第三方文档或商标。

发布前：

- 后续新增或引用材料时，核对其来源、再许可条件和所需署名。当前设计参照了本地备份工具、执行步骤和测试思路；OpenAI 文档仅链接，不打包页面、示例代码或商标素材。
- 将本目录单独作为仓库根目录；不要把整个交付父目录加入仓库。随附的 `.gitignore` 默认拒绝未列入白名单的文件，但不会移除已跟踪的文件，也不会审查白名单文件的内容。发布前核对暂存清单、暂存差异、已有 Git 历史和发行附件；不要包含真实备份、会话、存储清单、用户名/私人绝对路径、历史证据、截图、运行日志、凭据或第三方文档快照。不要用强制添加绕过忽略规则。
- 在装有受支持 Codex 的环境中，以**纯合成样本**验收 `/skills` 识别、显式调用、无隐式调用、确认门和默认沙箱阻挡；不要为测试放宽权限。再在普通终端复测四类。发布是独立操作，需要维护者明确授权。
