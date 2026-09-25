# session-broker

原子会话能力（atomic-session）。一句话：**会话与工作区生命周期的原子经纪：创建 / 挂起 / 恢复 / 交接 / 复用。**

## 定位

- 《建设方案-多Agent系统与GitOps》（PROP-0001）第 8 节团队分工中 `atomic-session` 角色的承载仓库；随 M3"原子能力开源"（WO-0008）交付。
- 与 openJiuwen 原生的边界：会话内上下文压缩、todo 等运行时能力归原生（第 4.9 节 #5 / #10）；本仓库只做**跨任务会话资产的原子化经纪**，不复制原生已有决策点。

## 状态

M0 骨架（README / LICENSE / .gitignore）。

## License

Apache-2.0
