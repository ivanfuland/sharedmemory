# cass_mcp/config.py —— env 集中读取 + 语义后端 env 兜底（B3）
import os

# 语义检索必须打到本地 Infinity；import 时兜底设默认，保证 runner 起的 cass 子进程（继承 os.environ）总能命中，
# 即便 systemd/手动启动忘了带这俩 env（接线点，plan verbatim 没暴露）。
os.environ.setdefault("CASS_DATA_DIR", os.path.expanduser("~/.local/share/coding-agent-search"))
os.environ.setdefault("CASS_INFINITY_URL", "http://127.0.0.1:7997")

CASS_BIN   = os.environ.get("CASS_BIN", os.path.expanduser("~/.local/bin/cass"))  # 已 symlink 到 fork cass-infinity
CASS_PORT  = int(os.environ.get("CASS_MCP_PORT", "7788"))
CASS_AUDIT = os.environ.get("CASS_MCP_AUDIT", "infra/cass-mcp/cass_audit.log")    # B4 systemd 用绝对路径覆盖
SEMANTIC_FLAGS = ["--mode", "semantic", "--daemon", "--model", "bge-m3", "--rerank"]  # 语义检索固定 flags（契约 cass-semantic-prod.md）；--rerank 恒开不可关
