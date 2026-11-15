# 业务记录约定

cases 的 occurred_week 是问题发生周，reported_week 是首次受理周，received_at 是系统获得该记录的时间。cause_versions 按 case_id、reviewed_at 记录当时的原因判断，cause=unresolved 是尚未判定，不是正常产品。
shipment_counts 是渠道和出货周的业务暴露量，不等于已发现问题数量，missing 与 confirmed_zero 分开。channel_rules 的 opened_week 记录渠道上线，成熟期长度由稳定历史估计而非字段承诺。
现有 development_triangle 按发生周、报告延迟计算累计已报数量；原因仅采用截止前最近一次人工或自动判断。合成数据包含渠道报告节律变化与类别迁移。

## 共同版本语义
所有示例记录均为本地合成，不包含真实客户资料。received_at 不晚于观察截止的记录才可见；缺省该字段的配置为静态配置。revision 表示同一业务身份的订正次数，不是新样本。时间不带时区时按 UTC 解读。
分析请求会保存可见数据的内容摘要及完整内容；重复 request_id 与原请求完全相同时返回原结果，即使源文件后来改变。其他请求内容复用该编号返回冲突。新观察须使用新编号。


周序号以 2026-01-05 为第零周；reported_week、reviewed_at 与 received_at 保持一致的周序关系。cause=unresolved 表示该案尚无原因结论。shipment_counts 用 missing/null 与 confirmed_zero/0 区分覆盖缺失和确认无出货。


## 快照样例字段补充

以上字段说明对应当前初始快照中的本地合成记录；示例不存在的历史字段不视为隐藏接口要求。


## 已有能力与字段边界

周序号以 2026-01-05 为第零周，每周七天；received_at 是实际获知时间，不把未来记录折叠进当前周。new 渠道在第28周后才有出货和受理；cause_versions 的最新 revision 代表该案当前判断。reported_triangle 输出渠道、发生周和延迟的已受理计数；最终原因数量与尚待报告量由 occurrence_nowcast 按发生周估计。

## 发生周估计口径

occurrence_nowcast 按渠道、发生周输出三段：known 是截止前已受理且最新原因判断非 unresolved 的实际件数，unresolved 是已受理但原因未定的实际件数，unreported 是尚待报告的估计。总量与最终各原因数量由这三段经同一分摊规则推出，区间随之传导，reconciliation 记录核对结果与残差。复核提速只把件数从 unresolved 移入 known，不改变总量估计，判断是否恶化应看 total 而非 known。
渠道完全成熟发生周数达到 mature_weeks_required 且最老发生周年龄超过已观察最大延迟时才给出估计；不满足时状态为 insufficient_history，只报告实际件数，不借用总体延迟分布填充，borrowing 给出拒绝证据（自身成熟周数、尾部是否封闭、与其他渠道延迟范围是否重叠）。
估计只使用 received_at 不晚于 as_of 的记录；回看历史周会时用当时的 as_of 加新 request_id 重新请求，旧 request_id 的记录不被改写。
