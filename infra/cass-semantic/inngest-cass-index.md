# Phase C — CASS 增量拉取 Inngest cron（待部署到 jarvis-workflow-ts）

> 状态：**PREPARED, 待协调部署**。jarvis-workflow-ts 当前在 `m3/distill-bridge-cron` 分支（M3 部署中），
> 不在此 plan 内自动改它/重启 pm2。部署应在 jarvis 干净分支上做（M3 落地后或协调分支）。
> entrypoint `index-pull.sh` 已在本目录就绪并验过结构。镜像 `distill-bridge` 的 index.ts+runner.ts 双文件模式。

## 部署步骤（jarvis-workflow-ts）

1. 建分支 `feat/cass-index-cron`（off main 或与 M3 协调）。
2. 建目录 `src/inngest/apps/jarvis/functions/cass-index/`，放下面两文件。
3. `route.ts` 加 `import { cassIndexFunction } from "@/inngest/apps/jarvis/functions/cass-index";` + 列进 `functions: [...]`。
4. `pm2 restart jarvis-workflow` → Inngest dev server 重发现 → `cass-index-daily` 可见。
5. 验证：手动 invoke `cass/index.requested` → 聊一段新对话 → 再 invoke → canonical conv 计数 +N + 语义搜得到；空跑 idempotent。

## `functions/cass-index/index.ts`

```ts
import { inngest } from "@/inngest/apps/jarvis/client";
import { runCassIndex } from "./runner";

export const cassIndexFunction = inngest.createFunction(
  { id: "cass-index-daily", name: "CASS incremental index pull (nightly)",
    retries: 1, concurrency: { limit: 1 } },          // concurrency=1 + 脚本内 flock 双保护
  [                                                   // 双 trigger：cron 定时 + event 手动触发(验证/补跑)
    { cron: "TZ=Asia/Shanghai 0 4 * * *" },           // 04:00；与 distill-bridge(03:30) 两独立 cron，不串
    { event: "cass/index.requested" },                // 手动 invoke 入口（部署后验证用）
  ],
  async ({ step }) => {
    return await step.run("index-pull", () => runCassIndex());
  },
);
```

## `functions/cass-index/runner.ts`

```ts
import { spawnSync } from "node:child_process";
import * as path from "node:path";
import { sendTelegram } from "@/inngest/shared/notify";

const PULL = path.join(process.env.HOME ?? "/home/ivan",
  "projects/sharedmemory/infra/cass-semantic/index-pull.sh");

function lastJsonLine(s: string): any | null {
  const lines = (s || "").trim().split("\n").filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i--) {
    try { return JSON.parse(lines[i]); } catch { /* keep scanning up */ }
  }
  return null;
}

export async function runCassIndex(): Promise<any> {
  const r = spawnSync("bash", [PULL], { encoding: "utf-8", timeout: 2 * 60 * 60 * 1000 }); // ≤2h
  if (r.status !== 0) {
    // index-pull.sh 失败路径在 stdout 末行也吐 {"ok":false,"error":...}；优先取它（含 "Infinity down" 等关键信息，codex P2）
    const errJson = lastJsonLine(r.stdout || "");
    const detail = errJson?.error || (r.stderr || "").slice(0, 500) || String(r.error);
    await sendTelegram(`🛑 *cass-index* fatal exit=${r.status}\n${detail}`, { markdown: true });
    throw new Error(`cass-index failed (exit=${r.status}): ${detail}`);
  }
  const ok = lastJsonLine(r.stdout || "");
  if (!ok) throw new Error(`cass-index: no JSON report in stdout`);
  return ok;   // index-pull.sh 末行 JSON 报告
}
```
