# B站话题与转写 · MaiBot 插件

公开热门、搜索、简介和弹幕素材，以及可选本地 faster-whisper 音频转写。

> 首次开源预发布，版本 0.4.1。尚未在所有宿主版本及系统实测；不是 DeepSeek 官方插件。

## 快速开始

1. 安装 MaiBot 与其要求的 Python（建议 3.12）。本插件使用 SDK 2.16.0 及以上的接口；旧环境验证基线为 MaiBot 1.3.15，新源码 1.3.19 的完整运行兼容尚待验证。
2. 将本仓库完整克隆到宿主实际的插件目录，确保 `_manifest.json`、`plugin.py` 位于该插件目录根层。Windows、Linux、Docker 要使用各自实际挂载路径，不照抄开发者电脑路径。
3. 在宿主使用的同一 Python 环境运行 `python -m pip install -r requirements.txt`。已有同版本依赖可复用，不要安装到无关的系统 Python。
4. 复制 `config.example.toml` 为 `config.toml`，或通过宿主插件配置页生成实际配置。**先填配置，再显式启用**。
5. 配置授权会话及模型别名。先关闭转写和自动浏览，验证基本查询；如需转写，另行安装 faster-whisper 并配置本地模型目录。
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

配置授权会话及模型别名。先关闭转写和自动浏览，验证基本查询；如需转写，另行安装 faster-whisper 并配置本地模型目录。

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

### B站资料层级与可选转写

基础工具 `bili_video_probe`、`bili_search`、`bili_trending` 获取公开素材。简介、弹幕和自动语音转写是不同资料，弹幕不能当成字幕或证明视频实际说了什么；代码包含音频转写，并非已实现读取所有 B站字幕。没有画面分析能力，不能理解仅在画面出现的操作和图表。

转写需在宿主环境安装 `requirements-transcript.txt`，另行获得兼容 faster-whisper/CTranslate2 的模型目录，并设置 `transcript_model_dir`。代码使用 `local_files_only=True`，不会因为填了模型名字就自动下载。CPU 可用 int8；GPU 为可选项，需要自己配置兼容 CTranslate2 的 CUDA/cuDNN 环境。仓库不含模型权重，也不宣称任意 GPU 配置可用。

转写会下载受限大小的音轨到临时目录，调用本地模型并尝试清理音频；进程被强制退出时要检查临时文件。不要处理无权访问的内容，不绕过付费、登录和验证码。B站接口不是稳定性承诺，限流或接口变化时可能不可用。旧开发笔记中的“免登录可用”不是所有视频都能访问的保证。

需要主动分享时可另装 `maibot-whale-speakup`，填写同一组授权会话。对外 API `local.bili-topics.pick_topic` 是可选话题源，取用不等于已发出消息。
