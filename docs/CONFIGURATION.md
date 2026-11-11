# 配置字段参考

由本次发布的 config.py 提取；最终校验以代码与宿主配置 UI 为准。不要把实际配置提交到仓库。

| 配置类.字段 | 类型 | 默认值/工厂 | 说明 |
|---|---|---|---|
| Settings.enabled | `bool` | `False` | 读取公开内容，可用本地登录态取得视频轨；不发布或互动 |
| Settings.config_version | `str` | `'0.2.0'` |  |
| Settings.interval_minutes | `int` | `180` | 采集间隔，至少180分钟 |
| Settings.daily_max | `int` | `3` | 每天最多采集次数，失败也计入 |
| Settings.interests | `list[str]` | `lambda: ['游戏', '电竞', '英雄联盟', 'LPL', '数码', '动漫']` | 话题关键词 |
| Settings.private_user_ids | `list[str]` | `list` | 允许取话题的私聊QQ |
| Settings.group_ids | `list[str]` | `list` | 允许取话题的群 |
| Settings.deep_read | `bool` | `False` | 话题被抽中时，额外读取公开弹幕并生成内容摘要；不登录、不下载视频 |
| Settings.deep_read_max_duration | `int` | `1800` | 只深读不超过该时长（秒）的视频 |
| Settings.deep_read_cooldown_minutes | `int` | `20` | 两次深读之间的最小间隔，避免频繁请求 |
| Settings.prewarm_count | `int` | `2` | 每轮采集后预先深读几条，0 表示关闭；预热让话题被抽中时立刻带摘要 |
| Settings.danmaku_limit | `int` | `600` | 每次最多读取多少条弹幕 |
| Settings.danmaku_sample | `int` | `120` | 清洗去重后均匀取样多少条送入模型 |
| Settings.summary_task | `str` | `'utils'` | 生成摘要使用的 MaiBot 模型任务 |
| Settings.summary_max_tokens | `int` | `600` | 摘要生成的最大输出 token |
| Settings.tool_sample | `int` | `50` | 工具读取视频时取样多少条弹幕 |
| Settings.tool_max_duration | `int` | `14400` | 工具按需读取时允许的视频最长时长（秒），默认4小时 |
| Settings.browse_deep_watch | `bool` | `True` | 刷B站时逐条深看（转写+画面） |
| Settings.frames_enabled | `bool` | `False` | 允许按弹幕热度抽帧看画面 |
| Settings.frames_auto_budget | `bool` | `True` | 按时长自动决定抽帧数 |
| Settings.frames_budget | `int` | `12` | 每次抽帧上限 |
| Settings.frames_max_height | `int` | `816` | 抽帧视频轨的最高分辨率 |
| Settings.frames_batch | `int` | `4` | 每次视觉请求送几张图 |
| Settings.frames_max_minutes | `int` | `20` | 超过这个时长不抽帧 |
| Settings.frames_keep_seconds | `int` | `3600` | 抽帧图片保留秒数，过期自动删 |
| Settings.share_watch_enabled | `bool` | `False` | 对发给她的 B站视频先处理画面和音轨，再回应 |
| Settings.share_watch_webui | `bool` | `False` | 允许本地 WebUI 聊天试用声画观看 |
| Settings.share_watch_frames | `int` | `24` | 分享视频的独立抽帧预算，覆盖首中尾 |
| Settings.share_watch_batch | `int` | `6` | 分享视频每个分析窗口最多几帧 |
| Settings.share_watch_timeout_seconds | `int` | `1200` | 下载、转写与分段画面分析的等待上限；剧情汇总单独计时 |
| Settings.browse_enabled | `bool` | `False` | 允许她主动去 B站 搜/刷感兴趣的内容当谈资 |
| Settings.browse_interval_minutes | `int` | `240` | 两次主动浏览的最小间隔（分钟） |
| Settings.browse_daily_max | `int` | `3` | 每天最多主动浏览几次 |
| Settings.browse_keywords | `list[str]` | `list` | 主动浏览的关键词；留空则用 interests |
| Settings.browse_max_minutes | `int` | `15` | 主动浏览时只挑这个时长以内的视频 |
| Settings.transcript_enabled | `bool` | `False` | 是否启用本地语音转写（下载音频→whisper→用完即删） |
| Settings.transcript_model_dir | `str` | `''` | whisper 模型目录；留空自动找 .tools/whisper-models/small |
| Settings.transcript_device | `str` | `'cpu'` | 推理设备：cpu 或 cuda |
| Settings.transcript_compute_type | `str` | `'int8'` | 计算精度：int8（CPU快）/ float16（GPU） |
| Settings.transcript_max_minutes | `int` | `20` | 允许转写的最长视频分钟数 |
| Settings.transcript_inline_seconds | `int` | `50` | 超过这么多秒才完成的转写改为后台继续 |
| Settings.transcript_timeout_seconds | `int` | `600` | 单次转写的硬超时 |
| Settings.transcript_keep_days | `int` | `30` | 转写文本保留天数 |
| Settings.transcript_sample_chars | `int` | `1500` | 回给模型的转写文本长度上限 |
| Settings.tool_rate_per_minute | `int` | `8` | 工具每分钟最多调用次数，防止刷接口 |
| BiliConfig.plugin | `Settings` | `Settings` |  |
