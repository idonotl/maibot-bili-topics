# 0.7.1 分享的视频先听声音、再看画面

功能与 v0.7.0 相同，本版只修正发布仓库的测试：公开导出把各开关默认改为关闭后，
`test_shared_watch` 的聊天接线断言失效，现在测试显式打开 `share_watch_enabled`；
生成真实短视频的那一条改为在缺少 PyAV 的环境里明确跳过，不再让整个模块导入失败。

- 新增 `media_watch.py`：对用户发来的 B站视频按「完整音轨转写 + 分段画面」联合理解后再回应；由 `share_watch_enabled` 显式开启，`share_watch_frames`、`share_watch_batch` 控制画面预算，`share_watch_timeout_seconds` 统一约束下载、转写与分段分析，剧情汇总单独计时。
- 表达边界写进提示：有标题或简介可以说刷到，有完整声画才能说看完；观众说法按观众看法理解，不编造画面细节。
- 人设代入按字样判定：标题、简介或笔记命中 DeepSeek／大肥鱼类拟人字样时才把角色绑定到自己身上，且绑定证据必须逐字出现在笔记里；其他视频一律不带入，也不借用视频中其余角色的台词。
- 摘要模型改为宿主任务 `summary_task`（默认 `utils`）；开启主动浏览后默认逐条深看（`browse_deep_watch` 默认 `true`），插件总开关 `enabled` 与主动浏览仍默认关闭。
- manifest 增加 `message.get_by_id`、`message.get_by_time_in_chat` 能力，用于定位用户刚分享的那条视频消息。

四个插件的总开关 `enabled` 默认为 false；导出时清理了真实账号与机器路径，授权名单为空，未携带原数据库、聊天记录或 Git 历史。插件 ID 保留以维持数据与跨插件调用兼容。可用范围请结合 docs/VALIDATION.md 判断，不代表已通过插件商店审核或全平台实测。
