# BiliBot 独立版

BiliBot 是一个完全独立运行的 B站 AI Bot 服务，不再依赖 AstrBot。

## 主要特性

- **完全独立** - 不依赖 AstrBot，可独立部署运行
- **多人格管理** - 支持保存多个人格，随时手动切换
- **统一 Prompt 编排** - 所有文本生成场景（评论、动态、周总结）统一受人格控制
- **强化上下文理解** - 评论线、评论区、视频内容、Bot 历史发言完整纳入
- **现代化 Web 控制台** - 清晰的控制台风格 UI
- **敏感信息保护** - API Key 脱敏显示，配置安全保存

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

复制配置示例文件：

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml`，填写必要的配置：

```yaml
bilibili:
  sessdata: "your_sessdata"
  bili_jct: "your_bili_jct"
  dede_user_id: "your_uid"

llm:
  api_key: "your_api_key"
  base_url: "https://api.siliconflow.cn/v1"
  model: "Qwen/Qwen2.5-72B-Instruct"

web:
  admin_password: "your_secure_password"  # 修改默认密码！
```

### 3. 运行

```bash
# 方式1: 直接运行
python -m bilibot

# 方式2: 指定配置文件
python -m bilibot --config config.yaml

# 方式3: 快速配置向导
python -m bilibot --quickstart
```

### 4. 访问 Web 控制台

打开浏览器访问 http://localhost:8080

默认登录：admin / admin123

## 目录结构

```
bilibot/
├── __init__.py              # 包入口
├── __main__.py              # CLI 入口
├── app/
│   ├── __init__.py
│   ├── app.py               # 主应用（真实启动 Uvicorn + Scheduler）
│   └── config_loader.py     # 配置加载器
├── api/
│   ├── __init__.py
│   ├── audit.py             # 审计 API
│   ├── config.py            # 配置 API
│   ├── logs.py              # 日志 API
│   ├── memory.py            # 记忆 API
│   ├── personas.py          # 人格 API
│   └── responses.py         # 统一响应工具（ok/fail）
├── models/
│   └── __init__.py          # 数据模型（SceneType/Persona/ReplyContext/...）
├── prompts/
│   ├── __init__.py
│   └── orchestrator.py      # Prompt 编排器（normalize_scene）
├── services/
│   ├── __init__.py
│   ├── audit_store.py       # 审计存储（mark_published）
│   ├── comment_context.py   # 评论上下文构建服务
│   ├── memory_writer.py     # 记忆写入服务
│   └── persona_store.py     # 人格库
├── web/
│   ├── __init__.py
│   ├── panel.py             # Web 面板（create_web_app）
│   └── static/
│       ├── css/
│       └── js/
├── bilibili_api.py          # B站 API 封装
├── bilibili_qrlogin.py      # B站二维码登录
├── config_loader.py         # 配置加载器（独立模块）
├── context_builder.py       # 上下文包构建器
├── data_store.py            # JSON 数据存储
├── humanized_behavior.py    # 人格化行为系统
├── knowledge_memory.py      # 知识库记忆
├── llm_adapter.py           # LLM 适配器
├── memory.py                # 记忆系统
├── personality.py           # 人格系统（legacy）
├── reply.py                 # 回复生成器（generate_reply）
└── scheduler.py             # 调度器（评论/动态/周总结）

scripts/
└── smoke_start_server.py    # 真实启动 smoke 测试脚本

tests/                       # 测试套件（177 passed）
```

## 配置说明

### B站账号

| 字段 | 说明 |
|------|------|
| sessdata | B站登录凭证（必需） |
| bili_jct | CSRF 令牌（必需） |
| dede_user_id | 你的 UID |
| buvid3 | Cookie 指纹 |

### LLM

支持 OpenAI 兼容 API，如 SiliconFlow、OpenAI、Azure OpenAI 等。

### 功能开关

- `reply.auto_reply` - 自动回复评论
- `proactive.enabled` - 启用主动行为
- `proactive.dynamic` - 发动态

### Web 管理面板

- 默认端口 8080
- 默认账号 admin/admin123
- **生产环境务必修改密码**

## API 端点

### 认证

- `POST /api/login` - 登录
- `POST /api/logout` - 登出

### 配置

- `GET /api/config/schema` - 获取配置 schema
- `GET /api/config/full` - 获取完整配置（脱敏）
- `PATCH /api/config` - 更新配置
- `POST /api/config/validate` - 验证配置
- `POST /api/config/reload` - 热重载

### 人格

- `GET /api/personas` - 列出所有人格
- `POST /api/personas` - 创建人格
- `GET /api/personas/{id}` - 获取人格
- `PATCH /api/personas/{id}` - 更新人格
- `DELETE /api/personas/{id}` - 删除人格
- `POST /api/personas/{id}/activate` - 激活人格
- `POST /api/personas/{id}/copy` - 复制人格
- `POST /api/personas/test` - 测试人格

### B站

- `GET /api/bilibili/qrcode` - 获取登录二维码
- `POST /api/bilibili/qrcode/status` - 查询扫码状态

### 记忆

- `GET /api/memory` - 列出记忆（支持 `page` / `page_size` / `category` / `active` / `keyword` 筛选）
- `GET /api/memory/{id}` - 获取单条记忆
- `DELETE /api/memory/{id}` - 软删除（`is_active=0`）
- `GET /api/memory/stats` - 记忆统计（按 category 分组、近 24h 新增等）
- `POST /api/memory/search` - 关键词搜索
- `POST /api/memory/migrate` - 从旧 JSON 记忆迁移到 SQLite（幂等）

### 审计

- `GET /api/audit/generations` - 列出生成审计（支持 `scene` / `persona_id` / `keyword` / `page` / `page_size` 筛选）
- `GET /api/audit/generations/{id}` - 获取单条审计详情
- `GET /api/audit/stats` - 审计统计

### 系统状态

- `GET /api/status/public` - 公开健康状态（无需登录）
- `GET /api/status` - 完整系统状态（需登录，含 `security.default_password` / `security.cors_open` 风险标志）

## 部署

### Docker

```bash
# 构建
docker build -t bilibot .

# 运行
docker run -d \
  -p 8080:8080 \
  -v $(pwd)/config.yaml:/app/config.yaml \
  bilibot
```

### Docker Compose

```yaml
version: '3.8'
services:
  bilibot:
    build: .
    ports:
      - "8080:8080"
    volumes:
      - ./config.yaml:/app/config.yaml
      - ./data:/app/data
```

## 开发

```bash
# 安装开发依赖
pip install -r requirements.txt

# 运行
python -m bilibot

# 代码格式
black bilibot/
ruff check bilibot/

# 测试（见下文「测试与验收」）
pytest tests/
```

## 测试与验收

### 必跑命令

实现/修改后必须全部通过：

```bash
# 1. 字节码编译
python -m compileall -q bilibot

# 2. CLI 入口
python -m bilibot --help

# 3. 核心导入
python -c "import bilibot; print(bilibot.__version__)"
python -c "from bilibot.models import Persona, SceneType, ReplyContext"
python -c "from bilibot.services.persona_store import PersonaStore"
python -c "from bilibot.prompts.orchestrator import PromptOrchestrator"

# 4. 完整测试套件
python -m pytest tests/

# 5. 残留依赖扫描（应为空）
#    扫描代码中是否还有 from/import 旧 astrbot 依赖
rg "from\s+astrbot|import\s+astrbot" bilibot README.md config.example.yaml tests

# 6. 真实启动 smoke（临时配置 + 随机端口，10 秒内 /api/status/public 返回 200）
python scripts/smoke_start_server.py
```

### 测试覆盖

`tests/` 目录下的测试文件：

| 文件 | 覆盖范围 |
|------|---------|
| `test_imports.py` | 核心模块导入、无外部强依赖 |
| `test_auth.py` | 鉴权中间件：未登录 401、登录 200、logout 401、会话过期 401 |
| `test_config_api.py` | 配置脱敏、`***已配置***` 保留原值、boolean/array/number 类型规范化、`CONFIG_VALIDATION_ERROR`、`/api/status.security.default_password` |
| `test_memory_api.py` | 空库 200、`/stats` 不被 `/{id}` 捕获、软删除 `is_active=0`、迁移幂等、`category` 筛选 |
| `test_prompt_orchestrator.py` | 每个场景都能 build、场景规则注入、人格切换 prompt 变化、视频上下文不足警告 |
| `test_context_builder.py` | dataclass / dict 兼容、字段进入上下文、`不得编造视频细节` 警告 |
| `test_audit.py` | `AuditStore.record/query/get/count`、HTTP API、`prompt_preview` 不含敏感字段 |
| `test_persona_store.py` | 人格 CRUD、激活切换 |
| `test_memory_system.py` | 记忆系统核心逻辑 |
| `test_app_startup.py` | 真实启动：`server.serve()` 调用、`web.enabled=false` 不创建 server、Web 异常退出传播、`/api/status/public` 无需登录 |
| `test_comment_context.py` | 评论上下文：完整 `ReplyContext` 构建、视频 API 失败降级、SQLite `content_video` 记忆复用、prompt 含视频标题/UP/用户名/评论内容 |
| `test_audit_publish.py` | 发布审计：`mark_published` true/false、target 合并、动态/评论发布后 audit 状态、HTTP 返回真实 `published` |
| `test_scene_normalize.py` | SceneType 兼容：`normalize_scene` 字符串/枚举/非法值、Orchestrator 字符串 scene、`/api/personas/preview` 与真实生成一致 |

### API Smoke 验收

```text
未登录 GET /api/config/full         -> 401
未登录 GET /api/personas            -> 401
未登录 GET /api/memory              -> 401
POST /api/login 正确账号密码        -> 200
登录后 GET /api/config/full         -> 200
登录后 GET /api/personas            -> 200
登录后 GET /api/memory              -> 200
登录后 GET /api/memory/stats        -> 200
登录后 GET /api/audit/generations   -> 200
POST /api/logout                    -> 200
logout 后 GET /api/config/full      -> 401
```

### 配置类型规范化验收

```text
PATCH reply.auto_reply=false             -> raw_config reply.auto_reply is False
PATCH reply.block_keywords="a,b,c"       -> raw_config reply.block_keywords is list
PATCH proactive.video_times="10:00,..."  -> raw_config proactive.video_times is list
PATCH llm.api_key="***已配置***"         -> 原值保留
PATCH llm.max_tokens="abc"               -> 400 CONFIG_VALIDATION_ERROR
```

## 安全注意

- **不要提交** `config.yaml`、API Key、Cookie 到代码仓库
- 敏感字段（API Key、密码）会以 `***已配置***` 脱敏显示
- 生产环境务必修改默认密码
- 已添加 `.gitignore` 保护敏感文件

## 许可证

MIT License

## 致谢

- 使用 SiliconFlow API 提供 LLM 支持
- 感谢所有 OpenAI 兼容 API 服务商