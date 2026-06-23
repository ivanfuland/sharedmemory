# OpenClaw AGENTS.md 分拣协议

记忆写入分拣：

- **世界知识 / 结论态**（人/项目/决策/偏好，跨会话稳定）→ `gbrain put` / `gbrain timeline`
- **运行态**（当前任务进度、临时状态、心跳）→ daily memory（**不进 gbrain**）

判据：「换个会话还成立吗？」

- 成立 → gbrain（持久世界知识）
- 只对当下有效 → daily memory

## 分拣示例

| 内容类型 | 示例 | 目标 |
|----------|------|------|
| 项目技术决策 | "Portola 选 Flutter + Unity 架构" | gbrain |
| 个人偏好 | "Ivan 偏好 A/B/C 方案格式" | gbrain |
| 代号/人物绑定 | "ivandebot = Ivan 的 GitHub 账号" | gbrain |
| 当前任务进度 | "Task6 已完成 Step 2" | daily memory |
| 临时状态 | "当前 PR 等待 CI" | daily memory |
| 心跳 / 会话元数据 | "上次活跃 2026-06-23 14:30" | daily memory |

## 写入命令参考

```bash
# 世界知识 → gbrain
gbrain put --slug "portola-arch" --title "Portola 架构决策" --content "..."
gbrain timeline --project "portola" --event "选型 Flutter+Unity"

# 运行态 → daily memory（gbrain 不介入）
# 由各端 daily memory 机制自行处理，gbrain adapter 不写入
```

## 注意事项

- gbrain 写入需 `GBRAIN_HOME` 可达，写失败时 fail-soft，**不崩会话**
- daily memory 路径由各端宿主决定（CC / Codex / OpenClaw 各异）
- 分拣错误（运行态写进 gbrain）只是冗余，不破坏正确性；但积累多会降低 query 精度
