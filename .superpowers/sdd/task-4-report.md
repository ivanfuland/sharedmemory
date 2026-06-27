# Task 4 Report — 15 条合成 dev 控制集

> 状态：完成。`uv run pytest tests/test_m4_synthetic_control.py -v` → 1 passed。

---

## 15 条逐项说明

| # | source_path 后缀 | 类型 | gold 条数 | 拆合依据 |
|---|---|---|---|---|
| 1 | `choose-a-not-b` | decision | **2** | 签字点① R4：「用 Qlib 不用 backtrader」选 A 舍 B 拆 2 条（`{LFT, 底座选用 Qlib}` + `{LFT, 不选 backtrader}`），未来「用不用 backtrader」能独立命中 |
| 2 | `param-bundle` | fact | **3** | R2：三个独立可查参数（port 6299 / redis 6380 / docker compose），每参数各一条 |
| 3 | `multi-entity` | fact | **3** | R3 + M2：三实体各自角色各一条；同一关系不在两端各记（M2），故 Portola 不出现在 gold entity 中 |
| 4 | `decider` | decision | **2** | M3 签字点③：「老兰决定」是 provenance 不另开条；只记 TZ=Asia/Shanghai 规则 + 03:30 时间点两条事实 |
| 5 | `achievement-metrics` | fact | **4** | 签字点④ R2：成就（重构管线/首个方案）1 条 + 3 指标（FPS/功耗/设备数）各 1 条，共 4 条 |
| 6 | `noise-heartbeat` | — | **0** | §4 跳过规则：HEARTBEAT_OK 是 heartbeat，无信息量，gold=[] |
| 7 | `noise-ack` | — | **0** | §4 跳过规则：「嗯好的」+「收到」是寒暄，gold=[] |
| 8 | `fact-cass-port` | fact | **2** | R2：两个独立可查参数（绑定端口 7788 / 通过 tailscale serve 暴露），各一条 |
| 9 | `pref-download-fmt` | preference | **1** | 单一偏好设定，不可再拆（BluRay 2160p x265 10bit HDR 是一个完整规格槽位） |
| 10 | `pref-encode-group` | preference | **1** | 偏好整体（压制组名单是配套属性，内嵌）；若未来压制组名单有独立查询需求可拆，当前合 1 条 |
| 11 | `lesson-dirty-state` | lesson | **1** | 单一独立教训，不可再拆 |
| 12 | `lesson-e2e-verify` | lesson | **1** | 「看日志行≠功能正常」与「须真实触发末端」是同一教训的两面，合 1 条（理由紧贴约束，M3 附注） |
| 13 | `action-cass-upgrade` | action_item | **2** | R1 独立维度：「PASS 才切换」（正向动作）⟂「FAIL 时回滚 prev」（故障响应），各一条 |
| 14 | `action-openclaw-config` | action_item | **2** | R1 + M3 附注：「禁止直接编辑」（操作约束）⟂「静默 auto-restore 后果」（独立可查，「直接编辑后会怎样」），各一条 |
| 15 | `decision-api-key` | decision | **2** | 签字点① R4：「用 API key 不用订阅 OAuth」选 A 舍 B 拆 2 条，理由内嵌在舍 B 条中（M3 附注） |

---

## 覆盖统计

| 维度 | 要求 | 实际 |
|---|---|---|
| 总条数 | 15 | 15 ✓ |
| gold=[] (噪声/空) | ≥2 | 2 (items 6,7) ✓ |
| 类型 fact | ≥2 | 4 (items 2,3,5,8) ✓ |
| 类型 decision | ≥2 | 3 (items 1,4,15) ✓ |
| 类型 preference | ≥2 | 2 (items 9,10) ✓ |
| 类型 lesson | ≥2 | 2 (items 11,12) ✓ |
| 类型 action_item | ≥2 | 2 (items 13,14) ✓ |
| 5 复合边界 source_path | 各 1 | 全覆盖 ✓ |

## 测试结论

```
uv run pytest tests/test_m4_synthetic_control.py -v
1 passed in 0.01s
```
