# AI 开发入口

修改、调试或审查本仓库前，必须完整阅读并遵循根目录的 [`CONTRIBUTING.md`](CONTRIBUTING.md)。其中定义了项目架构、数据契约、数据源插件化、缓存与性能要求、测试矩阵以及 PR 复审和合并标准。

涉及代码二次开发、前端插槽、后端可替换策略、扩展注册或上游升级兼容时，还必须阅读 [`docs/secondary-development.md`](docs/secondary-development.md)。该文档区分当前已实现能力与目标扩展契约；不得根据设计示例虚构尚不存在的 API。

同时遵守以下规则：

- 先理解调用链和现有测试，再进行修改。
- 保持实现简单、改动范围最小，不处理无关问题。
- 不覆盖工作区已有修改，不虚构测试或审查结果。
- 以实际验证结果作为完成标准。

## Git 由 agent 独家维护（2026-11-28 起）

本仓的 commit / push **不再征求确认**，也不要问"要提交吗"——见 `CONTRIBUTING.md` §13。
规则四条：

1. **会话开始**：`python scripts/git_autosync.py --install --status`。`--install` 补齐
   `.git/hooks/`（pre-commit / post-commit）与 `~/.wb-switch/hook.sh` 里的 Stop 段；
   `--status` 报告上个会话留下的改动。本机频繁切换账号，会话与账号配置会被重置，
   而**仓库不会被重置** —— 恢复入口必须放在仓库里，这一步就是它。
2. **交付前**：把"这次改了什么"写进 `.workbuddy/git/commit-msg.txt`（首行 Conventional
   Commits 主题，中文正文），再跑 `python scripts/git_autosync.py`。没写也照样提交，
   只是消息退化成 `chore(autosync)` —— 判断是可选的，留存不是。
3. **不要**手写 `git add -A` / `git commit` / `git push`。写 git 的唯一入口是该脚本：
   密钥扫描、超过 20 MB 的文件拦截、永不 force、合并/变基/冲突/游离 HEAD 一概不提交，
   都在它里面。它提交后由 post-commit 钩子推送，"本地永远等于 origin"是这条链维持的。
4. 它退出码为 2（提交被拒）时先读输出修根因：冲突、变基中、密钥命中、linked worktree
   各有各的修法，**不要 `--force` 绕过**。内容不会因此丢：被拒时它已经写了快照。

`refs/wb/checkpoints` 是写入快照（`git log refs/wb/checkpoints`），只在本地、不进
main 历史，用于"改坏了/会话被切断了，内容去哪了"的救急，不要当成分支使用。
