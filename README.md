# B站话题与视频深看 · MaiBot 插件

获取公开话题、搜索、简介和弹幕，并可转写视频语音、抽帧描述画面。

> 当前预发布版本 0.6.0。验证环境为 MaiBot 1.3.19、SDK 2.21.0。

## 快速开始

1. 安装 MaiBot 与其要求的 Python（建议 3.12）。本插件使用 SDK 2.16.0 及以上的接口；已核对 1.3.19 下的插件测试；实际联网与 QQ 联调需在自己的部署中验收。
2. 将本仓库完整克隆到宿主实际的插件目录，确保 `_manifest.json`、`plugin.py` 位于该插件目录根层。Windows、Linux、Docker 要使用各自实际挂载路径，不照抄开发者电脑路径。
3. 在宿主使用的同一 Python 环境运行 `python -m pip install -r requirements.txt`。已有同版本依赖可复用，不要安装到无关的系统 Python。
4. 复制 `config.example.toml` 为 `config.toml`，或通过宿主插件配置页生成实际配置。**先填配置，再显式启用**。
5. 配置授权会话及模型别名。先验证基本查询，再按需开启音轨、画面和后台深看。
6. 重启或通过宿主支持的方式重新加载插件，在插件页查看状态和错误。先用隔离测试会话，不要在大群直接测试自动发送。

```sh
git clone https://github.com/idonotl/maibot-bili-topics.git <宿主插件目录>/bili-topics
# 在克隆后的插件目录、宿主 Python 环境执行
python -m pip install -r requirements.txt
```

## 环境与成本

- 需要运行中的 MaiBot 宿主和匹配的 SDK；不是独立运行 `python plugin.py` 的程序。
- 基础状态和 SQLite 不需要 GPU。Windows 缺少 IANA 时区数据库时由 tzdata 提供支持。
- 模型能力复用宿主配置；`deepseek-flash-affect` 等只是示例别名，不是硬编码供应商 API 或附带密钥。请替换成自己配置中可用的模型别名。
- 分类、记忆提炼、摘要、实际生成回复可能消耗所配置模型的额度；本地抽签本身不调用模型。
- 网络、模型和适配器的可用性由使用者环境决定，安装插件不等于已经登录 QQ。

## 使用和限制

配置授权会话及模型别名。先验证基本查询，再按需开启音轨、画面和后台深看。

运行数据位于 SDK `ctx.paths.data_dir` 指向的独立目录，通常为 `instance/data/plugins/local.bili-topics/`。源码仓库不包含任何用户记忆、好感数据库、账号配置或登录会话。备份使用 SQLite backup API 或宿主提供的备份操作，不只复制仍在写入的主文件而漏掉 WAL。

关闭功能：先在配置将 `plugin.enabled=false`，重新加载；已有数据不会因关闭开关自动删除。删除和迁移前应备份。不要随意修改插件 ID，否则运行目录、跨插件调用和已有数据不会自动迁移。

## 文档

- [用户与 Agent 接入指南](docs/AGENT_GUIDE.md)：架构、配置步骤、验证、数据与排错。
- [完整配置参考](docs/CONFIGURATION.md)：字段、默认值与说明。
- [贡献指南](CONTRIBUTING.md) 与 [隐私说明](PRIVACY.md)。

## 开发与测试

```sh
python -m unittest discover -s tests -v
```

测试账号只使用合成数据。部分宿主契约测试需要通过 `MAIBOT_HOST_ROOT` 指向兼容 MaiBot 源码；缺失时会明确跳过，不算完整通过。外网和真实 QQ 行为不由单元测试保证。

## 来源与许可

本插件许可证为 `MIT`，见 [LICENSE](LICENSE)。相关上游声明如有则保留在 NOTICE.md、UPSTREAM-LICENSE。本项目独立适配 MaiBot，不安装或冒充原 DSH 插件；依赖及模型权重适用其各自许可证。

### 视频资料与深看

- 基础工具包括 `bili_video_probe`、`bili_search`、`bili_trending`。标题、简介和弹幕分别提供元信息及观众反应。
- `bili_transcript` 将有权访问的音轨交给本地 faster-whisper。安装 `requirements-transcript.txt` 后提供 CTranslate2 格式的模型目录，设置 `transcript_model_dir` 与 `transcript_enabled=true`。CPU 可使用 `int8`；模型文件单独准备。
- `bili_frames` 按时长和弹幕热点取样，PyAV 解码，Pillow 压缩，宿主模型生成画面描述。设 `frames_enabled=true`，确认宿主 `utils` 模型能接收图片输入。截图只反映抽样时刻，弹幕不能作为画面事实。
- `browse_enabled=true` 后可启用 `browse_deep_watch`，后台按开关完成转写与抽帧。同一视频可以复用缓存。表达学习通过可选的 `local.whale-style.auto_learn` 联动；独立安装这个仓库即可使用视频功能。
- 若匿名无法获取部分媒体轨，可按自己的访问权限使用本地 SDK 数据目录中的 `bili-cookie.json`，结构为 `{"cookies": {"SESSDATA": "<本地值>"}}`。公开版本只将 Cookie 发往 B 站 API 的 HTTPS 请求。切勿提交真实文件。
- `local.bili-topics.pick_topic` 可供主动搭话使用，`local.bili-topics.learning_material` 可供表达学习取缓存。两者都不会直接发送 QQ 消息。
