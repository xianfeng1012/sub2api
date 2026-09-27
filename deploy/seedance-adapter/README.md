# Seedance 适配器（Ark -> OpenAI Video）

Sub2API 的 Seedance 通道说火山方舟（Ark）异步任务协议，而上游中转只提供
OpenAI 风格的 `/v1/videos`。本适配器在两者之间做双向转换，Sub2API 侧
不需要改动任何协议逻辑，只要把账号的 `base_url` 指向它即可。

```
Sub2API ──Ark──> seedance-adapter:9000 ──OpenAI Video──> <上游中转>
```

## 部署

放在与 Sub2API 同一个 compose 网络里，Sub2API 通过服务名访问：

```yaml
  seedance-adapter:
    image: python:3.12-alpine
    container_name: seedance-adapter
    restart: unless-stopped
    command: ["python", "/app/seedance_adapter.py"]
    environment:
      UPSTREAM_BASE: "https://<上游中转域名>"
      UPSTREAM_CREATE_PATH: "/v1/videos"
      UPSTREAM_STATUS_PATH: "/v1/videos/{id}"
      TOKENS_PER_SECOND_720P: "5000"
      PORT: "9000"
    volumes:
      - ./seedance_adapter.py:/app/seedance_adapter.py:ro
    networks: [sub2api-network]
```

账号配置（后台 → 账号 → OpenAI API Key 类型）：

| 字段 | 值 |
|------|-----|
| `base_url` | `http://seedance-adapter:9000`（**不要**带 `/api/v3`，适配器路径由 Sub2API 拼） |
| `openai_capabilities` | 必须包含 `seedance` |
| `model_mapping` | 例如 `{"seedance_v2.0_std":"seedance_v2.0_std","seedance_v2.5":"seedance_v2.5"}` |

分组需要开启「允许图片生成」媒体权限，否则会被网关拦在 403。

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `UPSTREAM_BASE` | 必填 | 上游 OpenAI 视频接口的 origin |
| `UPSTREAM_CREATE_PATH` | `/v1/videos` | 创建任务的路径 |
| `UPSTREAM_STATUS_PATH` | `/v1/videos/{id}` | 查询任务的路径，`{id}` 会被替换 |
| `TOKENS_PER_SECOND_720P` | `5000` | 上游不回 token 数，按 720p/秒折算计费用量；需与分组单价对齐 |
| `UPSTREAM_TIMEOUT` | `120` | 上游请求超时（秒） |
| `PORT` | `9000` | 监听端口 |

## 参考素材能力（已实测）

| 类型 | 支持 | 行为 |
|------|------|------|
| `text` | ✅ | 可多条，合并为 prompt |
| `image_url` | ✅ | 仅单张，必须是公网 `https://` 地址 |
| `audio_url` | ❌ | 返回 400，不再静默丢弃 |
| `video_url` | ❌ | 返回 400，不再静默丢弃 |
| 其它 `type` | ❌ | 返回 400 |

上游还会二次校验参考图必须是公网 https，`http://` 与 base64/data URL 都会被拒。

> 历史行为提醒：v1.0 会把音频/视频素材静默丢弃，导致生成结果与预期不符却无任何报错。
> v1.1 起改为显式 400。
