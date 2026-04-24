# agent_learn

从零手写一个 **Coding Agent** 的学习仓库。走的是 OpenAI `chat.completions` 工具调用协议，兼容 DeepSeek / Kimi / Qwen / 阿里云 DashScope 等第三方端点。

三个文件 = 三个渐进版本，按编号顺序读，刚好是一次"从草稿到生产"的重构之旅。

---

## 目录

| 文件 | 行数 | 角色 | 你能学到什么 |
| --- | --- | --- | --- |
| `main.py` | 130 | v0 · 草稿 | 一个 Agent 应该由哪些模块组成（CLI / Tool / ContextManager / Agent）。此版本**语法都不通**，是思维导图，不是可运行代码。 |
| `main_minimal.py` | 182 | v1 · 最小可跑 | 把 v0 的设计"最小改动跑通"。Read + Grep 两个工具，完整的 act loop，够你理解 tool use 协议的闭环。 |
| `main_improved.py` | 598 | v2 · 生产化 | 在 v1 上把 8 类工程问题逐条落地：重试、权限、并发、压缩、解耦、配对……每段代码都标了批判点编号 `#1~#8`。 |

推荐阅读顺序：`main_minimal.py` → `main_improved.py`（v0 只用来对比"想法"和"落地"的 gap）。

---

## 快速开始

```bash
git clone <this-repo>
cd agent_learn

cp .env.example .env
# 编辑 .env 填入 OPENAI_API_KEY，可选改 OPENAI_BASE_URL

pip install openai       # 唯一的运行时依赖
bash run.sh              # 等价于 python3.11 main_improved.py
```

界面长这样（真实终端里带色）：

```
agent_learn  Ctrl-C to cancel current turn · Ctrl-D to exit

> 读一下 main_minimal.py 总结它的 act loop

assistant
│ 我来读一下这个文件。

  → read_file(path='main_minimal.py', offset=0)
    [ok] 4f2a1c  (3821B)
      │ 1  """
      │ 2  main_minimal.py — 把你草稿里的结构"最小改动跑通"的版本。
      │ ...

assistant
│ act loop 的闭环是：...
```

---

## v1 → v2 的 8 条改进（对着 `main_improved.py` 里的 `#1~#8` 注释读）

| # | 主题 | v1 的问题 | v2 的做法 |
| --- | --- | --- | --- |
| 1 | 失败面 | 模型超时就崩；工具抛异常就崩 | 模型调用指数退避重试；工具异常转成 `is_error=True` 的 `tool_result` 继续喂模型 |
| 2 | 终止条件 | 有可能无限循环 | `MAX_TURNS` 熔断；上下文预算压缩；`KeyboardInterrupt` 只杀当前 act loop |
| 3 | Tool schema | system prompt 和 API 调用的 schema 两份手写易漂 | `Tools.schemas()` 作为单一真相源，工具类自带 `ToolSpec` |
| 4 | 并发 | 所有工具串行跑 | 工具声明 `parallel_safe`；调度器把同批可并行的丢 `ThreadPool`，`tool_call_id` 顺序不乱 |
| 5 | 权限层 | 副作用工具无审批 | `Approver.check()`：`allow / deny / always`，副作用工具（edit/bash）默认询问 |
| 6 | CLI 解耦 | CLI 逻辑散落在 Agent 里 | Agent 只发 `AgentEvent`；`CLIListener` 是外挂的 listener，测试可换 silent |
| 7 | 上下文预算 | 历史无限长，token 爆 | 旧 `tool_result` 降级为 `[cached:<path>] head:<首 20 行>`，原文落盘 `/tmp/agent_cache/`，用 `read_file` 回读 |
| 8 | `tool_use_id` 配对 | 中途异常 → 下一轮 API 报 400 | `_pair_dangling_tool_calls` 兜底补齐；任何异常也转成 `is_error` result，保证配对不断 |

---

## `main_improved.py` 可调参数速查

生产场景先看这几个旋钮。

| 位置 | 参数 | 默认 | 说明 |
| --- | --- | --- | --- |
| `ContextManager` | `MAX_CHARS` | `200_000` | ~66K tokens（3 chars/token 估），deepseek-v3.2 128K 上下文的 ~50% |
| `ContextManager` | `KEEP_RECENT_TOOLS` | `8` | 最近 N 条 tool_result 保留原文，其余降级 |
| `Agent` | `MAX_TURNS` | `60` | 单次 act loop 上限，防死循环 |
| `Agent.pool` | `max_workers` | `8` | 只读工具的并行度 |
| `Model` | `max_retries` | `5` | 指数退避 1+2+4+8+16 ≈ 31s |
| `Model` | `temperature` | `0.2` | 编码场景偏确定性 |
| `BashTool` | `timeout` | `120s` | 覆盖 build/test 常见耗时 |
| `GrepTool` | `timeout` | `30s` | 大仓库 `rg` 扫描 |
| `CLIListener` | `MAX_PREVIEW_CHARS` / `MAX_PREVIEW_LINES` | `600 / 12` | tool_result 终端预览长度 |

---

## v2 故意没做的事

- **真正的 token 计数**：用字符数近似，够用；上生产换 `tiktoken` 或模型方提供的 tokenizer
- **流式输出（streaming）**：为了代码清晰
- **记忆持久化**：会话级 `ContextManager`，进程退出就没了
- **多 session / 多用户**：单进程单对话

---

## 工具清单（v2）

| 工具 | 并行安全 | 副作用 | 说明 |
| --- | --- | --- | --- |
| `read_file` | ✓ | ✗ | 带行号的文件读取；`edit_file` 依赖它的 mtime 记录做乐观锁 |
| `ls` | ✓ | ✗ | 单层目录列表 |
| `grep` | ✓ | ✗ | `rg` 正则搜索，需要本机装了 ripgrep |
| `bash` | ✗ | ✓ | 任意 shell 命令，**默认走审批** |
| `edit_file` | ✗ | ✓ | read-before-edit + mtime 乐观锁；`old_string=''` 且文件不存在 → 创建新文件 |

---

## 运行环境

- Python 3.11+
- `pip install openai`（仅此一个运行时依赖）
- `rg`（ripgrep）—— `grep` 工具用
- 任意 OpenAI 兼容端点：DashScope / DeepSeek / Kimi / Qwen / 本地 vLLM ……

---

## 协议

MIT
