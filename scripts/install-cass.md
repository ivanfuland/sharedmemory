# CASS 安装记录（M0 Task 2）

版本：cass 0.6.13（锁定基线，见 cass-version.txt）

## 踩坑 + 正确路径（Ubuntu 22.04）
1. **预编译二进制不可用**：官方 install.sh 装的 `cass-linux-amd64` 要 GLIBC 2.38/2.39，
   Ubuntu 22.04 只有 2.35 → 运行即报 `GLIBC_2.38 not found`。
2. **完整源码构建失败**：`install.sh --from-source` 默认带 `semantic` feature，
   编译 bundled onnxruntime 在链接阶段失败（ld error，ort_sys/resource_accountant）。
3. **正解 = baseline 构建（无 ONNX）**：Cargo.toml 文档化的方式，
   lexical 全功能保留，仅 semantic 模式返回明确错误（M0/捕获/词法检索够用；语义层是 P5）：
   ```bash
   git clone --depth 1 https://github.com/Dicklesworthstone/coding_agent_session_search
   cd coding_agent_session_search
   cargo build --release --no-default-features --features qr,encryption --bin cass
   install -m755 target/release/cass ~/.local/bin/cass
   ```
   需 Rust ≥1.85（本机 1.95）。

## 关键路径（Task 3 用）
- canonical DB：`~/.local/share/coding-agent-search/agent_search.db`
- data-dir：`~/.local/share/coding-agent-search`（search 必带 `--data-dir`）
- 检索：`cass search "<q>" --robot --mode lexical --fields summary --robot-meta --data-dir <data-dir>`
- hit 结构：`{hits:[{source_path,line_number,agent,title,snippet,content,score}]}`
