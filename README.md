# one-script-agent

用一个 Python 文件从零手写的 **Coding Agent**。走 OpenAI `chat.completions` 工具调用协议，兼容 DeepSeek / vLLM / Qwen / Kimi / OpenAI 等一切 OpenAI 兼容端点。

仓库只有两个脚本，对照着读，刚好是一次「从最小可跑到生产化」的演进：

| 文件 | 角色 | 工具 | 依赖 |
| --- | --- | --- | --- |
| `main_minimal.py` | 最小可跑版：理解 act loop 闭环 | `read_file` `grep` | 需 `pip install openai` |
| `main_improved.py` | 完整功能版：流式 + 并发 + 上下文压缩 | `read_file` `ls` `grep` `bash` `edit_file` | **零第三方依赖**（纯标准库） |

---

## 快速开始

### 1. 配置（三个环境变量）

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `OPENAI_API_KEY` | API 密钥 | 必填 |
| `OPENAI_BASE_URL` | 端点地址 | `https://api.deepseek.com` |
| `OPENAI_MODEL` | 模型名 | `deepseek-v4-flash` |

```bash
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://api.deepseek.com   # 可选
export OPENAI_MODEL=deepseek-v4-flash             # 可选
```

接其他端点示例：

```bash
# 本地 vLLM
export OPENAI_BASE_URL=http://localhost:8000/v1
export OPENAI_MODEL=Qwen/Qwen2.5-Coder-7B-Instruct

# OpenAI 官方
export OPENAI_BASE_URL=https://api.openai.com/v1
export OPENAI_MODEL=gpt-4o-mini
```

### 2. 运行

```bash
# 完整版（推荐，零依赖）
python3 main_improved.py

# 最小版（需先 pip install openai）
pip install openai
python3 main_minimal.py
```

启动后进入对话，直接输入需求即可（支持中文输入、按字符退格、方向键、历史）：

```
agent_learn  Ctrl-C to cancel current turn · Ctrl-D to exit
model=deepseek-v4-flash  url=https://api.deepseek.com

> 读一下 main_improved.py，总结它的 act loop
```

`Ctrl-C` 中断当前回合，`Ctrl-D` 退出。

---

## `main_improved.py` 的能力

- **零第三方依赖**：用标准库 `urllib` 直连，不需要 `openai` 包
- **流式输出**：边生成边显示；推理模型（如 `deepseek-v4-flash`）的思维链 `reasoning_content` 以暗色实时展示
- **多端点兼容**：自动处理 OpenAI / vLLM / 各代理的 SSE 方言差异（无 `[DONE]`、`usage` 末块、`tool_call` 缺 index 等）
- **一轮多工具并发**：只读工具（read/ls/grep）丢线程池并发，bash/edit 串行，结果按序配对
- **上下文压缩**：历史超预算时，旧的 `tool_result` 降级为「文件路径 + 头部」，原文落盘可经 `read_file` 回读
- **失败兜底**：模型调用指数退避重试；工具异常转成错误结果继续喂模型，不让 agent 崩
- **跨平台**：缓存目录用系统临时目录；无 `readline` 的平台自动降级

### 工具清单

| 工具 | 并发安全 | 说明 |
| --- | --- | --- |
| `read_file` | ✓ | 带行号读取；`edit_file` 依赖它做读前校验 |
| `ls` | ✓ | 单层目录列表 |
| `grep` | ✓ | `rg` 正则搜索，需本机装 ripgrep |
| `bash` | ✗ | 执行任意 shell 命令 |
| `edit_file` | ✗ | read-before-edit + mtime 乐观锁；`old_string=''` 且文件不存在则新建 |

---

## 运行环境

- Python 3.9+
- `main_improved.py`：无需 `pip install` 任何包（纯标准库）
- `main_minimal.py`：需 `pip install openai`
- `grep` 工具需本机安装 `rg`（ripgrep）；未安装时仅该工具不可用，不影响其余

---

## 协议

MIT
