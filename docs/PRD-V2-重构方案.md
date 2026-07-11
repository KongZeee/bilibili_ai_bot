# PRD V2：BiliBot 多账号 / 多人格 / 多 LLM 重构方案

> **版本**：V2.0  
> **日期**：2026-07-08  
> **状态**：待审核  
> **参考**：[AstrBot](https://github.com/AstrBotDevs/AstrBot) 多平台架构

---

## 1. 背景与目标

### 1.1 当前痛点

| 痛点 | 现状 | 影响 |
|------|------|------|
| 单账号 | `BilibiliAPI` 硬编码读取 `config.bilibili.sessdata` 等单组字段 | 无法同时运营多个 B站账号 |
| 单 LLM | `LLMAdapter` 全局单例，配置中只有一组 `api_key/base_url/model` | 不同账号/场景无法用不同模型 |
| 人格与账号耦合 | 人格切换是全局的，所有账号共享同一人格 | 账号 A 用亚托莉人格时，账号 B 也被迫切换 |
| 配置扁平 | `config.yaml` 中 `bilibili:` 下只有一组账号字段 | 无法表达多账号 × 多人格 × 多 LLM 的组合 |
| 调度器单实例 | `Scheduler` 持有单个 `self.bili`，主循环单次轮询一个账号 | 多账号需串行处理，效率低 |
| Web 面板单入口 | `/api/bilibili/qrcode` 等端点无账号维度 | 无法在面板管理多账号登录 |

### 1.2 重构目标

1. **多账号**：支持同时登录多个 B站账号，每个账号独立运行调度器
2. **多人格**：每个人格独立定义，可绑定到特定账号，也可全局共享
3. **多 LLM**：支持配置多个 LLM 提供商，不同账号/场景可用不同模型
4. **配置分离**：账号配置、人格配置、LLM 配置相互独立，可任意组合
5. **Web 面板**：支持多账号管理、独立登录、独立状态监控
6. **向后兼容**：保留单账号配置的兼容路径（旧 `config.yaml` 自动迁移）

### 1.3 非目标

- 不做插件系统（保持单进程简洁）
- 不做多平台支持（专注 B站，但架构预留扩展空间）
- 不做分布式部署（单机运行）

---

## 2. 架构设计

### 2.1 整体架构图

```
┌─────────────────────────────────────────────────────────┐
│                    BiliBotApp (主进程)                    │
│                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ AccountMgr   │  │ PersonaStore │  │  LLMManager  │  │
│  │ (账号管理器)  │  │ (人格存储)    │  │ (LLM管理器)  │  │
│  └──────┬───────┘  └──────────────┘  └──────────────┘  │
│         │                                               │
│  ┌──────▼──────────────────────────────────────────┐    │
│  │              AccountInstance (账号实例)           │    │
│  │                                                  │    │
│  │  ┌────────────┐  ┌────────────┐  ┌───────────┐  │    │
│  │  │ BilibiliAPI│  │ Scheduler  │  │ ReplyGen  │  │    │
│  │  │ (API封装)   │  │ (调度器)    │  │ (回复生成)│  │    │
│  │  └────────────┘  └────────────┘  └───────────┘  │    │
│  │                                                  │    │
│  │  ┌────────────┐  ┌────────────┐  ┌───────────┐  │    │
│  │  │ MemorySys  │  │ Knowledge  │  │  Safety   │  │    │
│  │  │ (记忆系统)  │  │ Memory     │  │ Checker   │  │    │
│  │  └────────────┘  └────────────┘  └───────────┘  │    │
│  └──────────────────────────────────────────────────┘    │
│         │                                               │
│  ┌──────▼───────┐                                       │
│  │  Web Panel   │  (管理所有账号/人格/LLM)               │
│  └──────────────┘                                       │
└─────────────────────────────────────────────────────────┘
```

### 2.2 核心设计原则（参考 AstrBot）

1. **Account = 平台实例**：每个 B站账号是一个独立的 `AccountInstance`，拥有自己的 API、调度器、记忆
2. **配置驱动**：所有实例由配置创建，`AccountManager` 负责加载/启停
3. **人格解耦**：人格存储独立，可绑定到账号（`account.persona_id`），也可运行时切换
4. **LLM 解耦**：LLM 提供商独立配置，账号引用 LLM ID，支持运行时切换
5. **事件路由**：每个账号独立轮询，消息在本账号实例内处理，互不干扰

### 2.3 数据流

```
AccountManager 启动
  │
  ├── 遍历 accounts 配置列表
  │     │
  │     └── 为每个 account 创建 AccountInstance
  │           │
  │           ├── 创建 BilibiliAPI(account_config)
  │           ├── 创建 Scheduler(account_config, bili, persona, llm, ...)
  │           ├── 创建 MemorySystem(account_id, data_dir)
  │           └── asyncio.create_task(scheduler.start())  # 并发运行
  │
  └── Web Panel 管理所有实例
        │
        ├── /api/accounts          → 列出所有账号
        ├── /api/accounts/:id      → 管理单个账号
        ├── /api/personas          → 管理人格
        └── /api/llms             → 管理 LLM 提供商
```

---

## 3. 核心模块设计

### 3.1 AccountManager（账号管理器）

**职责**：管理所有 B站账号实例的生命周期

**位置**：`bilibot/account/manager.py`（新建）

```python
class AccountManager:
    """账号管理器 — 管理多个 B站账号实例"""

    def __init__(self, config_loader, persona_store, llm_manager):
        self.config_loader = config_loader
        self.persona_store = persona_store
        self.llm_manager = llm_manager
        self._instances: Dict[str, AccountInstance] = {}  # account_id → instance

    async def initialize(self):
        """加载配置中的所有账号，创建实例"""

    async def start_all(self):
        """启动所有已启用的账号实例"""

    async def stop_all(self):
        """停止所有账号实例"""

    async def add_account(self, account_config: dict) -> str:
        """添加新账号（返回 account_id）"""

    async def remove_account(self, account_id: str):
        """移除账号"""

    async def restart_account(self, account_id: str):
        """重启单个账号"""

    def get_instance(self, account_id: str) -> Optional[AccountInstance]:
        """获取账号实例"""

    def list_instances(self) -> List[dict]:
        """列出所有账号状态"""
```

### 3.2 AccountInstance（账号实例）

**职责**：单个 B站账号的完整运行环境

**位置**：`bilibot/account/instance.py`（新建）

```python
class AccountInstance:
    """单个 B站账号实例 — 拥有独立的 API/调度器/记忆/人格"""

    def __init__(self, account_id: str, account_config: dict,
                 persona_store, llm_manager, data_dir: str):
        self.account_id = account_id
        self.config = account_config
        self.nickname: str = ""          # 从 nav API 获取
        self.uid: str = ""               # dede_user_id
        self.persona_id: str = ""        # 绑定的人格 ID
        self.llm_id: str = ""            # 绑定的 LLM ID
        self.status: str = "pending"     # pending/running/error/stopped

        # 子系统（每个实例独立）
        self.bili: BilibiliAPI           # 独立的 API 实例
        self.scheduler: Scheduler        # 独立的调度器
        self.memory: MemorySystem        # 独立的记忆
        self.knowledge_memory: KnowledgeBaseMemory  # 独立的知识库
        self.reply_gen: ReplyGenerator   # 独立的回复生成器
        self.safety_checker: SafetyChecker  # 独立的安全检查

    async def start(self):
        """启动账号实例：登录验证 → 初始化子系统 → 启动调度器"""

    async def stop(self):
        """停止账号实例"""

    async def reload(self):
        """重载配置（不重启进程）"""

    def get_status(self) -> dict:
        """获取账号运行状态"""
```

### 3.3 LLMManager（LLM 管理器）

**职责**：管理多个 LLM 提供商实例，支持运行时切换

**位置**：`bilibot/llm/manager.py`（新建，替换现有 `llm_adapter.py`）

```python
class LLMManager:
    """LLM 管理器 — 管理多个 LLM 提供商"""

    def __init__(self, config_loader):
        self._providers: Dict[str, LLMProvider] = {}  # llm_id → provider
        self._default_id: str = ""

    def initialize(self):
        """从配置加载所有 LLM 提供商"""

    def get_provider(self, llm_id: str = None) -> Optional[LLMProvider]:
        """获取 LLM 实例（None=默认）"""

    def add_provider(self, config: dict) -> str:
        """添加 LLM 提供商"""

    def remove_provider(self, llm_id: str):
        """移除 LLM 提供商"""

    def set_default(self, llm_id: str):
        """设置默认 LLM"""

    def list_providers(self) -> List[dict]:
        """列出所有 LLM 提供商"""
```

### 3.4 LLMProvider（LLM 适配器）

**职责**：封装单个 LLM 提供商的调用

**位置**：`bilibot/llm/provider.py`（新建）

```python
class LLMProvider:
    """LLM 提供商 — 封装 OpenAI 兼容 API 调用"""

    def __init__(self, llm_id: str, config: dict):
        self.llm_id = llm_id
        self.api_key: str = config.get("api_key", "")
        self.base_url: str = config.get("base_url", "")
        self.model: str = config.get("model", "")
        self.name: str = config.get("name", llm_id)
        self.enabled: bool = config.get("enabled", True)

    async def generate(self, prompt: str, system_prompt: str = "",
                       max_tokens: int = 1000, **kwargs) -> str:
        """生成文本"""

    async def test(self) -> bool:
        """测试连接"""

    def get_info(self) -> dict:
        """获取提供商信息"""
```

### 3.5 PersonaStore（人格存储，增强）

**已有**，需增强为支持账号绑定：

```python
class PersonaStore:
    # ... 现有方法 ...

    def set_account_persona(self, account_id: str, persona_id: str):
        """绑定账号到人格"""

    def get_account_persona(self, account_id: str) -> str:
        """获取账号绑定的人格 ID"""
```

### 3.6 Scheduler（调度器，改造）

**改造点**：从全局单例改为账号实例内部组件

```python
class Scheduler:
    def __init__(self, account_id: str, account_config: dict,
                 bili: BilibiliAPI, persona_store, llm_manager,
                 memory, data_store, ...):
        self.account_id = account_id  # 新增：账号 ID
        self.llm_id = account_config.get("llm_id", "")  # 新增：绑定的 LLM
        self.persona_id = account_config.get("persona_id", "")  # 新增：绑定的人格
        # ... 其他现有字段 ...

    async def _check_new_comments(self):
        # 每个账号独立检查自己的评论通知
        # LLM 调用时用 self.llm_manager.get_provider(self.llm_id)
        # 人格提示词用 self.persona_store.get_account_persona(self.account_id)
```

---

## 4. 配置结构

### 4.1 新配置格式（config.yaml V2）

```yaml
# ═══════════════════════════════════════════
# BiliBot 配置 V2 — 多账号 / 多人格 / 多 LLM
# ═══════════════════════════════════════════

# ─── LLM 提供商列表 ───
llm_providers:
  - id: "glm"                    # 唯一 ID
    name: "智谱 GLM"              # 显示名称
    api_key: "xxx"
    base_url: "https://open.bigmodel.cn/api/paas/v4"
    model: "glm-4-flash"
    enabled: true

  - id: "deepseek"
    name: "DeepSeek"
    api_key: "xxx"
    base_url: "https://api.deepseek.com/v1"
    model: "deepseek-chat"
    enabled: true

  - id: "local"
    name: "本地模型"
    api_key: "empty"
    base_url: "http://localhost:11434/v1"
    model: "qwen2.5:7b"
    enabled: false

# 默认 LLM（未绑定时的回退）
default_llm_id: "glm"

# ─── 人格列表 ───
# 人格配置存储在 data/personas.json（已有，不变）
# 账号通过 persona_id 引用

# ─── B站账号列表 ───
accounts:
  - id: "account_a"              # 唯一 ID
    name: "亚托莉小姐"            # 显示名称（自动从 nav API 更新）
    enabled: true
    bilibili:
      sessdata: "xxx"
      bili_jct: "xxx"
      dede_user_id: "3706995307711341"
      buvid3: ""
    persona_id: "atri"           # 绑定的人格 ID（为空则用默认）
    llm_id: "glm"               # 绑定的 LLM ID（为空则用默认）
    features:
      auto_reply: true
      reply_comment: true
      private_message: true
      proactive_watch: true
      proactive_dynamic: true
    reply:
      batch_size: 10
      auto_reply: true
    proactive:
      video_times: ["10:00", "14:00", "20:00"]
      dynamic_times: ["12:00", "18:00"]
    safety:
      paused: false
      blacklist: []
      rate_limit_per_hour: 30

  - id: "account_b"
    name: "第二个账号"
    enabled: true
    bilibili:
      sessdata: "yyy"
      bili_jct: "yyy"
      dede_user_id: "123456"
      buvid3: ""
    persona_id: "default"        # 用默认人格
    llm_id: "deepseek"           # 用 DeepSeek 模型
    features:
      auto_reply: true
      reply_comment: true
      private_message: false
      proactive_watch: false
      proactive_dynamic: false
    reply:
      batch_size: 5
      auto_reply: true
    proactive:
      video_times: []
      dynamic_times: []
    safety:
      paused: false
      blacklist: []
      rate_limit_per_hour: 20

# ─── Web 面板 ───
web:
  host: "0.0.0.0"
  port: 8080
  admin_password: "admin123"

# ─── 日志 ───
logging:
  level: "INFO"
  file: "data/bililog.log"
```

### 4.2 向后兼容迁移

旧配置（V1）：
```yaml
bilibili:
  sessdata: "xxx"
  bili_jct: "xxx"
  dede_user_id: "xxx"
llm:
  api_key: "xxx"
  base_url: "xxx"
  model: "xxx"
```

启动时检测到 V1 配置 → 自动迁移：
1. 生成 `default` LLM 提供商
2. 生成 `default` 账号（id=`account_default`）
3. 写入 V2 格式，备份旧配置为 `config.v1.yaml.bak`

---

## 5. 数据隔离

### 5.1 文件结构

```
data/
├── config.yaml                 # 全局配置
├── personas.json               # 人格存储（全局共享）
├── llm_providers.json          # LLM 提供商配置（可选，也可在 config.yaml）
│
├── accounts/                   # 账号数据（按 account_id 隔离）
│   ├── account_a/
│   │   ├── memory.json         # 该账号的记忆
│   │   ├── memory_atoms.db     # 该账号的知识库 SQLite
│   │   ├── replied.json        # 该账号的已回复记录
│   │   ├── watch_log.json      # 该账号的看视频记录
│   │   ├── watch_log.json      # 该账号的看视频记录
│   │   ├── dynamic_log.json    # 该账号的动态发布记录
│   │   └── session_ack.json    # 该账号的私信已读记录
│   │
│   └── account_b/
│       ├── memory.json
│       ├── memory_atoms.db
│       ├── replied.json
│       └── ...
│
└── bililog.log                 # 全局日志
```

### 5.2 数据隔离规则

| 数据 | 隔离维度 | 说明 |
|------|---------|------|
| 记忆 | 账号 | 每个账号有自己的记忆文件和 SQLite |
| 已回复记录 | 账号 | 不同账号的评论不互相干扰 |
| 看视频/动态记录 | 账号 | 每个账号独立的行为日志 |
| 人格 | 全局共享 | 人格定义是全局的，但绑定关系是账号级的 |
| LLM 配置 | 全局共享 | LLM 提供商是全局的，账号引用 ID |
| 安全配置 | 账号 | 每个账号有独立的暂停状态/黑名单/频率限制 |

---

## 6. Web 面板改造

### 6.1 新增页面

| 页面 | 路由 | 功能 |
|------|------|------|
| 账号管理 | `#/accounts` | 列出所有账号、状态、添加/删除/启停 |
| 账号详情 | `#/accounts/:id` | 单账号配置、登录状态、调度计划 |
| LLM 管理 | `#/llm` | 列出所有 LLM 提供商、测试连接、添加/删除 |
| 人格管理 | `#/personas` | 已有，增强账号绑定 UI |

### 6.2 API 端点

```
# 账号管理
GET    /api/accounts                    # 列出所有账号
POST   /api/accounts                    # 添加账号
GET    /api/accounts/:id                # 获取账号详情
PUT    /api/accounts/:id                # 更新账号配置
DELETE /api/accounts/:id                # 删除账号
POST   /api/accounts/:id/start          # 启动账号
POST   /api/accounts/:id/stop           # 停止账号
POST   /api/accounts/:id/restart        # 重启账号
GET    /api/accounts/:id/status         # 获取运行状态

# 账号级 B站操作
GET    /api/accounts/:id/bilibili/qrcode    # 获取登录二维码
GET    /api/accounts/:id/bilibili/qrcode/status  # 查询扫码状态
POST   /api/accounts/:id/bilibili/logout    # 退出登录

# 账号级数据
GET    /api/accounts/:id/memories        # 该账号的记忆列表
GET    /api/accounts/:id/safety/status   # 该账号的安全状态
POST   /api/accounts/:id/safety/pause    # 暂停该账号
POST   /api/accounts/:id/safety/resume   # 恢复该账号

# LLM 管理
GET    /api/llms                         # 列出所有 LLM
POST   /api/llms                         # 添加 LLM
PUT    /api/llms/:id                     # 更新 LLM 配置
DELETE /api/llms/:id                     # 删除 LLM
POST   /api/llms/:id/test               # 测试 LLM 连接

# 人格管理（已有，增强）
POST   /api/personas/:id/bind/:account_id  # 绑定人格到账号
```

---

## 7. 模块改造清单

### 7.1 新建模块

| 文件 | 说明 |
|------|------|
| `bilibot/account/manager.py` | AccountManager — 账号管理器 |
| `bilibot/account/instance.py` | AccountInstance — 账号实例 |
| `bilibot/llm/manager.py` | LLMManager — LLM 管理器 |
| `bilibot/llm/provider.py` | LLMProvider — LLM 适配器（从 llm_adapter.py 重构） |

### 7.2 改造模块

| 文件 | 改造内容 |
|------|---------|
| `bilibot/bilibili_api.py` | `__init__` 接受 `account_config` 而非全局 config |
| `bilibot/scheduler.py` | `__init__` 接受 `account_id`，LLM/人格从 manager 获取 |
| `bilibot/app/app.py` | 用 AccountManager 替代直接创建 BilibiliAPI/Scheduler |
| `bilibot/app/config_loader.py` | 新增 V2 配置解析（accounts 列表 + llm_providers 列表） |
| `bilibot/web/panel.py` | 新增账号管理/LLM 管理 API 端点 |
| `bilibot/services/persona_store.py` | 新增 `set_account_persona` / `get_account_persona` |
| `bilibot/reply.py` | `__init__` 接受 `llm_provider` 而非全局 LLM |
| `bilibot/humanized_behavior.py` | `__init__` 接受 `llm_provider` 而非全局 LLM |
| `bilibot/knowledge_memory.py` | `__init__` 接受 `account_id` 用于数据隔离 |
| `bilibot/memory.py` | `__init__` 接受 `account_id` 用于数据隔离 |
| `bilibot/data_store.py` | `__init__` 接受 `account_id`，数据目录按账号隔离 |

### 7.3 保留不变

| 文件 | 说明 |
|------|------|
| `bilibot/personality.py` | 人格定义类，不变 |
| `bilibot/safety.py` | 安全检查器，改为账号级实例化 |
| `bilibot/prompts/` | 提示词编排，不变 |
| `bilibot/services/comment_context.py` | 评论上下文，改为账号级实例化 |

---

## 8. 实施计划

### 阶段 1：基础设施（核心重构）

**目标**：搭建多账号骨架，单账号可运行

1. 新建 `bilibot/llm/provider.py` 和 `bilibot/llm/manager.py`
2. 新建 `bilibot/account/instance.py` 和 `bilibot/account/manager.py`
3. 改造 `config_loader.py` 支持 V2 配置 + V1 自动迁移
4. 改造 `app.py` 用 AccountManager 启动
5. 改造 `bilibili_api.py` 接受 account_config
6. 改造 `scheduler.py` 接受 account_id

### 阶段 2：数据隔离

**目标**：每个账号有独立的数据目录

1. 改造 `data_store.py` 支持 `account_id` 子目录
2. 改造 `memory.py` 和 `knowledge_memory.py` 数据路径
3. 改造 `safety.py` 为账号级实例
4. 改造 `reply.py` 和 `humanized_behavior.py` 接受 LLMProvider

### 阶段 3：Web 面板

**目标**：面板支持多账号管理

1. 新增账号管理页面（列表/添加/删除/启停）
2. 新增 LLM 管理页面（列表/测试/添加/删除）
3. 改造登录流程支持账号级二维码
4. 改造人格管理支持账号绑定

### 阶段 4：测试与迁移

**目标**：确保旧配置平滑迁移

1. V1 → V2 自动迁移测试
2. 多账号并发运行测试
3. 人格切换不影响其他账号测试
4. LLM 切换不影响其他账号测试

---

## 9. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| 多账号并发请求被 B站风控 | 账号封禁 | 每个账号独立的请求间隔和频率限制 |
| 内存占用线性增长 | 10+ 账号时内存压力大 | 限制最大账号数；共享 aiohttp 连接池 |
| 配置迁移失败 | 用户无法启动 | 保留 V1 备份；迁移失败时回退并提示 |
| 数据目录冲突 | 账号间数据串扰 | 严格按 account_id 隔离；启动时校验目录 |

---

## 10. 验收标准

1. ✅ 可在 `config.yaml` 中配置多个 B站账号
2. ✅ 每个账号可独立启动/停止/重启
3. ✅ 每个账号可绑定不同的人格
4. ✅ 每个账号可绑定不同的 LLM
5. ✅ 账号 A 切换人格不影响账号 B
6. ✅ 账号 A 的记忆/已回复记录与账号 B 隔离
7. ✅ Web 面板可管理所有账号/人格/LLM
8. ✅ 旧版 V1 配置可自动迁移到 V2
9. ✅ 多账号并发运行时无数据竞争

---

## 附录 A：与 AstrBot 架构对比

| 概念 | AstrBot | BiliBot V2 |
|------|---------|------------|
| 平台实例 | `Platform`（QQ/微信/TG...） | `AccountInstance`（B站账号） |
| 平台管理器 | `PlatformManager` | `AccountManager` |
| LLM 实例 | `Provider` | `LLMProvider` |
| LLM 管理器 | `ProviderManager` | `LLMManager` |
| 人格管理 | `PersonaManager`（DB存储） | `PersonaStore`（JSON存储） |
| 配置管理 | `AstrBotConfigManager`（多 abconf） | `ConfigLoader`（单文件多账号） |
| 消息路由 | `EventBus` + `umo→conf_id` | 账号内直接处理（无需路由） |
| Pipeline | `PipelineScheduler`（洋葱模型） | `Scheduler`（简单循环） |
| 插件系统 | `Star` + Handler 注册 | 无（不做插件） |

**关键差异**：BiliBot V2 不做插件系统和多平台支持，专注于 B站多账号运营，架构更简洁。每个账号实例内直接处理消息，不需要 AstrBot 的 EventBus 路由层。
