# PRD V3：BiliBot 链路修复 + 记忆系统重构

> **版本**：V3.0  
> **日期**：2026-07-10  
> **状态**：待审核  
> **前置**：PRD-V2-重构方案（多账号/多人格/多LLM架构）

---

## 1. 背景与目标

### 1.1 当前痛点

PRD V2 完成了多账号/多人格/多 LLM 架构重构，但全链路审查发现以下问题：

| 类别 | 问题数 | 影响 |
|------|--------|------|
| P0 严重 | 3 | 核心功能失效（多账号登录/记忆一致性/配置热重载） |
| P1 重要 | 10 | 稳定性/性能/数据一致性 |
| P2 优化 | 5 | 代码质量/用户体验 |
| 记忆系统 | 重构 | 双系统冗余、性能差、无限增长 |

### 1.2 重构目标

1. **记忆系统合并**：删除 `MemorySystem`，统一为 `KnowledgeBaseMemory` + 新建 `UserStateSystem`
2. **修复 P0**：多账号扫码登录、配置热重载、记忆写入不阻塞
3. **修复 P1**：动态发布阻塞、记忆无限增长、SQLite 阻塞事件循环等
4. **修复 P2**：评论截断不一致、未集成功能等
5. **配置页补全**：补齐 memory/personality/proactive 缺失的配置项

### 1.3 非目标

- 不改变现有 API 路由结构
- 不改变 config.yaml 顶层结构
- 不引入新的外部依赖（aiosqlite 除外，可选）

---

## 2. 记忆系统重构（核心）

### 2.1 现状问题

当前存在**双记忆系统并存**：

```
MemorySystem (memory.py)          KnowledgeBaseMemory (knowledge_memory.py)
├── JSON 文件存储                  ├── SQLite + BM25 + 向量索引
├── 纯Python遍历余弦相似度         ├── numpy批量计算 + RRF融合
├── 无容量上限（P1-4）             ├── is_active软删除 + TTL衰减
├── 独有：用户画像/好感度/心情      ├── 独有：结构化记忆原子(5类)
└── 独有：日终LLM清算              └── 独有：实体索引
```

**问题**：
1. 每次回复/看视频/发动态后**双写**，浪费一次 embedding 调用 + 一次 JSON 全量重写
2. `MemorySystem.semantic_search` 纯 Python 遍历全部 embedding，记忆多时严重拖慢（P1-5）
3. `MemorySystem._memory` 列表只 append 不限长，config 有 `max_today:50` 等上限但代码未实现（P1-4）
4. 两个系统的检索结果未统一，`ContextBuilder` 只用 `reply_context`，`MemorySystem.semantic_search` 的结果未进入回复上下文

### 2.2 重构方案：拆分 + 合并

```
删除 MemorySystem
    │
    ├── 记忆存储/检索/衰减  →  KnowledgeBaseMemory（已有，作为唯一记忆存储）
    │
    └── 用户画像/好感度/心情  →  新建 UserStateSystem（纯JSON读写，无LLM）
```

#### 2.2.1 新建 `bilibot/user_state.py`

从 `MemorySystem` 拆出以下功能：

| 方法 | 存储文件 | 用途 |
|------|----------|------|
| `get_user_profile_context(mid)` | `user_profiles.json` | 获取用户画像上下文 |
| `update_user_profile(mid, ...)` | `user_profiles.json` | 更新用户画像 |
| `get_affection(user_id)` | `affection.json` | 获取好感度 |
| `update_affection(user_id, delta)` | `affection.json` | 增减好感度 |
| `get_level(score, mid)` | - | 根据好感度获取关系等级 |
| `get_level_prompt(level)` | - | 获取等级对应的行为提示 |
| `get_today_mood()` | `mood.json` | 获取今日心情 |

**设计要点**：
- 纯 JSON 读写，无 LLM 调用，无 embedding
- 不依赖 `LLMAdapter`，构造参数只需 `(data_store, config)`
- 好感度等级映射逻辑保持不变（special/cold/stranger/normal/friend/close）

#### 2.2.2 日终清算处理

`MemorySystem.run_daily_consolidation()` 的 LLM 评估逻辑**删除**，原因：

1. `KnowledgeBaseMemory` 已有 TTL 衰减机制（exponential/linear/step），记忆自动过期
2. `cleanup_expired()` 已实现主动清理
3. LLM 日终评估每条记忆调用一次 LLM，成本高且不可靠（JSON 解析失败率高）
4. 衰减分数 `decay_score` 已能区分记忆重要性

**scheduler 中的日终清算调用**（`scheduler.py:232-248`）改为：
```python
# 只调用 KnowledgeBaseMemory.cleanup_expired()
if self.knowledge_memory and hasattr(self.knowledge_memory, "cleanup_expired"):
    purged = self.knowledge_memory.cleanup_expired()
```

#### 2.2.3 引用点迁移

| 文件 | 原调用 | 迁移后 |
|------|--------|--------|
| `scheduler.py` 构造 | `self.memory = memory` | `self.user_state = user_state` |
| `scheduler.py` 回复成功后 | `self.memory.save_chat_memory(...)` | **删除**（已有 `knowledge_memory.save_conversation_as_memory`） |
| `scheduler.py` 看视频后 | `self.memory.save_self_memory(...)` | **删除**（已有 `write_memory_atom`） |
| `scheduler.py` 发动态后 | `self.memory.save_self_memory(...)` | **删除**（已有 `write_memory_atom`） |
| `scheduler.py` 日终清算 | `self.memory.run_daily_consolidation()` | `self.knowledge_memory.cleanup_expired()` |
| `reply.py` 构造 | `self.memory = memory_system` | `self.user_state = user_state` |
| `instance.py` 初始化 | `MemorySystem(data_store, llm, config)` | `UserStateSystem(data_store, config)` |
| `context_builder.py` | `self.memory = memory_system` | `self.user_state = user_state` |
| `comment_context.py` | `memory_system` 参数 | 改为 `user_state` |

#### 2.2.4 删除清单

- `bilibot/memory.py` 整个文件删除
- `scheduler.py` 中所有 `if self.memory:` 双写代码块删除
- `instance.py` 中 `from bilibot.memory import MemorySystem` 删除
- `app.py` 中 `MemorySystem` 的导入和初始化删除
- `tests/test_memory_system.py` 删除或重写为 `KnowledgeBaseMemory` 测试

### 2.3 迁移兼容

对于已有 `memory.json` 数据的用户，提供一次性迁移脚本：

```python
# bilibot/migrate_memory.py
def migrate_memory_json_to_sqlite(memory_json_path, knowledge_base):
    """将旧 memory.json 迁移到 KnowledgeBaseMemory"""
    # 复用 KnowledgeBaseMemory.migrate_old_memory()（已有）
```

在 `Scheduler.start()` 中检测 `memory.json` 是否存在，存在则提示用户运行迁移脚本。

---

## 3. P0 修复（严重）

### 3.1 P0-1：QR 登录在 V2 多账号架构下写错位置

**文件**：`bilibot/bilibili_qrlogin.py:176-206`

**问题**：`update_config` 把新凭据写入 `raw["bilibili"]`（V1 段），但 V2 架构下 `AccountManager` 只加载 `accounts` 列表。如果已有 accounts 列表，扫码登录的凭据完全不会被任何 AccountInstance 使用。

**修复方案**：

1. `BilibiliQRLogin` 构造增加 `account_id` 参数
2. `update_config` 根据 `account_id` 写入 `accounts` 列表中对应账号的 `sessdata/bili_jct/dede_user_id/buvid3`
3. 如果 `account_id` 为空或找不到对应账号，回退到 V1 写入 `raw["bilibili"]`（兼容单账号）
4. QR 登录 API 路由增加 `account_id` 查询参数

**伪代码**：
```python
def update_config(self, cookies: Dict[str, str], account_id: str = "") -> Dict:
    raw = self.config.get_raw_config()
    accounts_list = raw.get("accounts", [])
    
    if account_id and accounts_list:
        # V2：写入指定账号
        for acc in accounts_list:
            if acc.get("id") == account_id:
                acc["sessdata"] = cookies.get("SESSDATA", "")
                acc["bili_jct"] = cookies.get("bili_jct", "")
                acc["dede_user_id"] = cookies.get("DedeUserID", "")
                if cookies.get("buvid3"):
                    acc["buvid3"] = cookies["buvid3"]
                break
    else:
        # V1 兼容：写入 bilibili 段
        bili = raw.setdefault("bilibili", {})
        bili["sessdata"] = cookies.get("SESSDATA", "")
        # ...
    
    self.config.save_config(raw, "config.yaml")
```

### 3.2 P0-2：记忆系统双写无事务，数据不一致

**文件**：`scheduler.py:538-570`

**问题**：先写 `knowledge_memory`（SQLite，内部调 LLM 提取记忆原子，可能很慢或失败），再写 `memory`（JSON）。无回滚。知识库的 `save_conversation_as_memory` 调用 LLM 处理对话，如果 LLM 超时，整条回复链路被阻塞。

**修复方案**：

1. **记忆合并后此问题自动消失**（不再有双写）
2. `KnowledgeBaseMemory.save_conversation_as_memory` 改为异步不阻塞：
   ```python
   # scheduler.py 回复成功后
   if self.knowledge_memory:
       asyncio.create_task(
           self.knowledge_memory.save_conversation_as_memory(...)
       )
   ```
3. 加 `done_callback` 记录异常，避免静默丢失

### 3.3 P0-3：配置热重载不更新运行中的 AccountInstance 凭据

**文件**：`account/instance.py:97-118`, `bilibili_api.py:58`

**问题**：`BilibiliAPI._csrf_token` 在构造时读取一次（`config.bilibili.bili_jct`），Web 配置保存后热重载只更新 `ConfigLoader`，不重建 `BilibiliAPI`。修改账号凭据后必须重启才生效。

**修复方案**：

1. `BilibiliAPI` 增加 `reload_credentials(config)` 方法，重新读取 `bili_jct/sessdata/dede_user_id/buvid3`
2. `AccountInstance` 增加 `reload()` 方法：
   ```python
   async def reload(self):
       """热重载账号配置（不重启调度器）"""
       self.account_config_loader = self._build_account_config_loader()
       if self.bili:
           self.bili.reload_credentials(self.account_config_loader)
       # persona_id/llm_id 变更需要重新 initialize
   ```
3. Web 配置保存后，遍历 `AccountManager._accounts`，调用 `reload()`
4. `update_config` 方法更新后立即触发 `reload()`

**注意**：`persona_id` / `llm_id` 变更需要重新 `initialize`，提示用户"切换人格/LLM 需要重启账号"。

---

## 4. P1 修复（重要）

### 4.1 P1-1：发布动态 await 阻塞主循环

**文件**：`scheduler.py:857`

**问题**：`await self._do_post_dynamic()` 阻塞主循环。配图链路（LLM 生成图片 prompt + 图片生成 120 秒超时 + 上传 60 秒）可使主循环卡 3 分钟，期间评论/私信检查全部停滞。

**修复方案**：
```python
# scheduler.py:856-860
if features.get("dynamic_post", True):
    for trigger_time in self._dynamic_times:
        time_str = f"{trigger_time[0]:02d}:{trigger_time[1]:02d}"
        if time_str == current_time and current_time not in self._dynamic_triggered:
            task = asyncio.create_task(self._do_post_dynamic())
            task.add_done_callback(self._on_task_done)
            self._dynamic_triggered.add(current_time)
            self._save_dynamic_schedule_state()
            break

def _on_task_done(self, task):
    """任务完成回调，记录异常"""
    if task.exception():
        logger.error(f"后台任务异常: {task.exception()}", exc_info=task.exception())
```

### 4.2 P1-2：主动看视频后台任务无引用，异常静默丢失

**文件**：`scheduler.py:847`

**问题**：`asyncio.create_task(self._do_proactive_video())` 不保存 task 引用，不加 `done_callback`。

**修复方案**：同 P1-1，统一用 `_on_task_done` 回调。保存 task 引用到 `self._running_tasks: set`，完成后移除。

### 4.3 P1-3：联网搜索每次回复先调一次 LLM 判断，延迟翻倍

**文件**：`reply.py:83`, `services/web_search.py:243-290`

**问题**：每次回复先调 LLM 判断是否搜索（`should_search_for_reply`），再调 LLM 生成回复。最坏 3 次 LLM 调用。

**修复方案**：

1. 增加规则预筛层（在 LLM 判断之前）：
   ```python
   def _quick_should_search(comment: str) -> Optional[str]:
       """规则预筛：明显需要搜索的直接返回关键词"""
       import re
       # 检测时间/新闻/价格/人物等关键词
       patterns = [
           (r"(最近|最新|今天|昨天|202\d年).+", "时事"),
           (r"(价格|多少钱|费用).+", "价格"),
           (r"(是谁|什么人).+", "人物"),
       ]
       for pattern, topic in patterns:
           if re.search(pattern, comment):
               return comment[:60]
       return None  # 需要进一步 LLM 判断
   ```
2. 规则命中则直接搜索，跳过 LLM 判断
3. 规则未命中且评论 >10 字才调 LLM 判断

### 4.4 P1-4：MemorySystem 无容量上限（记忆重构后修复）

**文件**：`memory.py:43-44, 133-134`

**问题**：`_memory` 列表只 append 不限长，config 有 `max_today:50` 等上限但代码未实现。

**修复方案**：**记忆重构后此问题自动消失**（`MemorySystem` 被删除，`KnowledgeBaseMemory` 用 SQLite 无内存列表）。`cleanup_expired()` 负责清理。

### 4.5 P1-5：MemorySystem.semantic_search 纯 Python 遍历（记忆重构后修复）

**文件**：`memory.py:190-230`

**问题**：每次回复遍历全部记忆的 embedding 逐条算余弦相似度。

**修复方案**：**记忆重构后此问题自动消失**（`KnowledgeBaseMemory.HybridRetriever` 用 numpy 批量计算）。

### 4.6 P1-6：KnowledgeBaseMemory SQLite 操作阻塞事件循环

**文件**：`knowledge_memory.py:189-194`

**问题**：`execute`/`query` 是同步 sqlite3 操作，在 asyncio 事件循环中直接调用。BM25 搜索（`search_by_keyword` 多词多次查询）会阻塞主循环。

**修复方案**：

方案 A（推荐）：用 `asyncio.to_thread` 包装所有 SQLite 操作
```python
async def search_memories_async(self, query: str, **kwargs):
    return await asyncio.to_thread(self.retriever.search, query, **kwargs)
```

方案 B（备选）：引入 `aiosqlite` 替换 `sqlite3`（改动大）

采用方案 A，改动最小。在 `KnowledgeBaseMemory` 中增加 async 方法：
- `search_memories_async` → 包装 `search_memories`
- `save_memory_async` → 包装 `save_memory`（embedding 获取仍为 async）
- `cleanup_expired_async` → 包装 `cleanup_expired`

调用方（`comment_context.py`、`scheduler.py`）改为调用 async 版本。

### 4.7 P1-7：日终清算遍历 self._memory 时重建列表（记忆重构后修复）

**文件**：`memory.py:560-568`

**问题**：遍历中重建列表 `self._memory = [x for x in self._memory if ...]`。

**修复方案**：**记忆重构后此问题自动消失**（`MemorySystem` 被删除）。

### 4.8 P1-8：评论失败计数器不持久化

**文件**：`scheduler.py:143, 501`

**问题**：`_comment_fail_counts` 纯内存，重启后丢失。如果某评论连续失败 2 次后重启，计数器清零，又会尝试 3 次。

**修复方案**：

1. 持久化到 `data_store`：
   ```python
   def _save_fail_counts(self):
       if self.ds:
           self.ds.save_json("comment_fail_counts.json", self._comment_fail_counts)
   
   def _load_fail_counts(self):
       if self.ds:
           self._comment_fail_counts = self.ds.load_json("comment_fail_counts.json", {})
   ```
2. `start()` 中调用 `_load_fail_counts()`
3. 失败计数变化时调用 `_save_fail_counts()`

### 4.9 P1-9：联网搜索结果拼接到 user_prompt 末尾

**文件**：`reply.py:90, 97`

**问题**：`search_context` 加在 `user_prompt` 末尾。如果 prompt 已长，搜索结果在末尾容易被 LLM 忽略。

**修复方案**：

将搜索结果作为独立的 context 段注入，而非简单拼接：
```python
# reply.py generate_reply()
if search_context:
    # 注入到 system_prompt 末尾（更受 LLM 重视）
    system_prompt = system_prompt + "\n\n" + search_context
    # 或者作为独立的 context 段
    # system_prompt += f"\n\n【联网搜索参考】\n{search_result[:800]}"

reply_text = await self.llm.generate(
    prompt=user_prompt,  # 不再拼接 search_context
    system_prompt=system_prompt,
    max_tokens=200,
)
```

### 4.10 P1-10：ContextBuilder 多账号并发时 get_current() 竞态

**文件**：`context_builder.py:153`

**问题**：`ContextBuilder` 是应用级共享单例，`self.persona_store.get_current()` 在多账号并发调用时，可能返回其他账号绑定的人格。

**修复方案**：

1. `ContextBuilder.build` 增加 `account_id` 参数：
   ```python
   def build(self, context, account_id: str = "") -> dict:
       # 优先用账号绑定的人格
       if self.persona_store and account_id:
           persona = self.persona_store.get_account_persona(account_id)
           if not persona:
               persona = self.persona_store.get_current()
       # ...
   ```
2. `PersonaStore` 增加 `get_account_persona(account_id)` 方法
3. `reply.py._build_prompts` 传入 `account_id`（从 `ReplyGenerator` 构造时注入）

---

## 5. P2 修复（优化）

### 5.1 P2-1：回复截断 200 字但 prompt 要求 50 字

**文件**：`reply.py:106-107` vs `reply.py:236`

**问题**：prompt 说 50 字，代码截断 200 字。

**修复方案**：统一为 233 字（B站限制）截断，prompt 改为"简短回复，不超过 100 字"。

### 5.2 P2-2：should_search_for_video 未集成

**文件**：`services/web_search.py:292-327`

**问题**：方法存在但 `_do_proactive_video` 未调用，主动看视频不会联网补充背景知识。

**修复方案**：在 `_do_proactive_video` 中视频理解前调用：
```python
# scheduler.py _do_proactive_video()
if self.web_search and self.web_search.is_available():
    try:
        search_query = await self.web_search.should_search_for_video(
            video_info={"title": title, "desc": desc, "tname": "", "owner_name": owner},
        )
        if search_query:
            search_result = await self.web_search.search(search_query)
            if search_result:
                video_content += f"\n\n【背景知识】{search_result[:500]}"
    except Exception:
        pass
```

### 5.3 P2-3：主动评论截断逻辑不一致

**文件**：`humanized_behavior.py:581` vs `humanized_behavior.py:759-760`

**问题**：`evaluate_video` 的 comment 截断到 100 字，`generate_proactive_comment` 截断到 40 字。

**修复方案**：统一截断到 100 字（B站评论限制 233 字，留余量）。

### 5.4 P2-4：动态内容加"..."截断

**文件**：`scheduler.py:1319-1320`

**问题**：`content[:200] + "..."`，动态被截断时加省略号不自然。

**修复方案**：改为直接截断，不加省略号：
```python
if len(content) > 200:
    content = content[:200]
```

### 5.5 P2-5：start_all 永久阻塞

**文件**：`account/manager.py:257-262`

**问题**：`asyncio.gather` 等待所有 `acc.start()`（每个是 while 循环），会永久阻塞。

**修复方案**：保持阻塞行为（设计意图），但改用 `asyncio.gather(*tasks, return_exceptions=True)` + 注释说明这是有意阻塞。

---

## 6. 配置页补全

### 6.1 memory schema 缺少 8 个配置项

**文件**：`api/config.py:305-326` vs `config.example.yaml:220-246`

**缺失字段**：`thread_compress_threshold`, `oid_compress_threshold`, `oid_keep_recent`, `user_compress_threshold`, `user_keep_recent`, `max_semantic_results`, `consolidation.batch_size`, `consolidation.long_term_age_days`

**修复方案**：补全 schema：
```python
"memory": {
    "type": "object",
    "label": "记忆系统",
    "fields": {
        # ... 已有字段 ...
        "thread_compress_threshold": {"type": "number", "label": "评论线压缩阈值"},
        "oid_compress_threshold": {"type": "number", "label": "评论区压缩阈值"},
        "oid_keep_recent": {"type": "number", "label": "OID保留条数"},
        "user_compress_threshold": {"type": "number", "label": "用户记忆压缩阈值"},
        "user_keep_recent": {"type": "number", "label": "用户记忆保留条数"},
        "max_semantic_results": {"type": "number", "label": "语义搜索结果数"},
        "consolidation": {
            "type": "object",
            "fields": {
                # ... 已有字段 ...
                "batch_size": {"type": "number", "label": "批量大小"},
                "long_term_age_days": {"type": "number", "label": "长期记忆天数"},
            }
        }
    }
}
```

### 6.2 proactive schema 与 yaml 不一致

**文件**：`api/config.py:338-352` vs `config.example.yaml:163-179`

**问题**：schema 有 `like/coin/favorite/comment/follow` 布尔开关，但 yaml 没有这些字段，代码也没使用。

**修复方案**：从 schema 中移除未实现的 `like/coin/favorite/comment/follow` 字段（避免误导用户）。如果未来要实现，再补回。

### 6.3 personality schema 缺少核心人设字段

**文件**：`api/config.py:254-263` vs `personality.py:29-33`

**问题**：`personality.py` 读取 `base_prompt/speaking_style/boundaries`，但 schema 没有。

**修复方案**：补全 schema：
```python
"personality": {
    "type": "object",
    "fields": {
        # ... 已有字段 ...
        "base_prompt": {"type": "string", "label": "基础人设提示词"},
        "speaking_style": {"type": "string", "label": "说话风格"},
        "boundaries": {"type": "string", "label": "禁止事项"},
    }
}
```

---

## 7. 实施计划

### 7.1 阶段划分

| 阶段 | 内容 | 优先级 |
|------|------|--------|
| Phase 1 | 记忆系统重构（第 2 节） | 高 |
| Phase 2 | P0 修复（第 3 节） | 高 |
| Phase 3 | P1 修复（第 4 节） | 中 |
| Phase 4 | P2 修复 + 配置页补全（第 5-6 节） | 低 |

### 7.2 Phase 1：记忆系统重构

**步骤**：

1. 新建 `bilibot/user_state.py`，从 `MemorySystem` 拆出用户画像/好感度/心情
2. 修改 `account/instance.py`：`MemorySystem` → `UserStateSystem`
3. 修改 `scheduler.py`：
   - 构造参数 `memory` → `user_state`
   - 删除所有 `if self.memory:` 双写代码块
   - 日终清算改为 `knowledge_memory.cleanup_expired()`
4. 修改 `reply.py`：`self.memory` → `self.user_state`
5. 修改 `context_builder.py`：`self.memory` → `self.user_state`
6. 修改 `comment_context.py`：`memory_system` → `user_state`
7. 修改 `app.py`：移除 `MemorySystem` 初始化
8. 删除 `bilibot/memory.py`
9. 删除/重写 `tests/test_memory_system.py`
10. 运行全部测试验证

**验证标准**：
- 所有现有测试通过
- 回复链路：用户画像/好感度/心情正常工作
- 记忆写入：只写 `KnowledgeBaseMemory`，不再双写
- 日终清算：`cleanup_expired()` 正常执行

### 7.3 Phase 2：P0 修复

| 任务 | 文件 | 验证标准 |
|------|------|----------|
| P0-1 QR登录V2兼容 | `bilibili_qrlogin.py` | 多账号下扫码登录写入正确账号 |
| P0-2 记忆写入不阻塞 | `scheduler.py` | 回复链路不被记忆写入阻塞 |
| P0-3 配置热重载 | `instance.py`, `bilibili_api.py` | Web改凭据后立即生效 |

### 7.4 Phase 3：P1 修复

| 任务 | 文件 | 验证标准 |
|------|------|----------|
| P1-1 动态发布不阻塞 | `scheduler.py` | 发动态时评论检查正常运行 |
| P1-2 任务异常回调 | `scheduler.py` | 后台任务异常被记录 |
| P1-3 联网搜索预筛 | `web_search.py` | 规则命中跳过LLM判断 |
| P1-6 SQLite异步 | `knowledge_memory.py` | 搜索不阻塞事件循环 |
| P1-8 失败计数持久化 | `scheduler.py` | 重启后计数器保留 |
| P1-9 搜索结果注入 | `reply.py` | 搜索结果在system_prompt |
| P1-10 人格竞态 | `context_builder.py` | 多账号并发人格正确 |

### 7.5 Phase 4：P2 + 配置页

| 任务 | 文件 |
|------|------|
| P2-1 回复截断统一 | `reply.py` |
| P2-2 视频搜索集成 | `scheduler.py` |
| P2-3 评论截断统一 | `humanized_behavior.py` |
| P2-4 动态截断 | `scheduler.py` |
| P2-5 start_all注释 | `manager.py` |
| 配置-1 memory补全 | `api/config.py` |
| 配置-2 proactive清理 | `api/config.py` |
| 配置-3 personality补全 | `api/config.py` |

---

## 8. 风险与回滚

### 8.1 风险

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 记忆迁移丢失数据 | 中 | 高 | 保留 `memory.json` 不删除，只停止写入 |
| `UserStateSystem` 接口不一致 | 低 | 中 | 保持方法签名与 `MemorySystem` 一致 |
| SQLite 异步包装遗漏 | 中 | 中 | 逐方法改造，加测试覆盖 |
| 配置热重载边界 case | 中 | 低 | persona/llm 切换提示重启 |

### 8.2 回滚

- 记忆重构前备份 `memory.py` 和 `memory.json`
- 如果 `KnowledgeBaseMemory` 出问题，恢复 `MemorySystem` + `memory.json`
- 配置页改动可通过 git revert 回滚

---

## 9. 验收标准

### 9.1 功能验收

- [ ] 多账号扫码登录写入正确账号凭据
- [ ] Web 配置修改凭据后立即生效（不重启）
- [ ] 回复链路正常工作（画像/好感度/心情）
- [ ] 记忆只写入 `KnowledgeBaseMemory`，无双写
- [ ] 日终清算执行 `cleanup_expired()`
- [ ] 发布动态时主循环不阻塞
- [ ] 联网搜索规则预筛生效
- [ ] SQLite 操作不阻塞事件循环
- [ ] 评论失败计数器重启后保留
- [ ] 多账号并发人格正确

### 9.2 测试验收

- [ ] 全部现有测试通过（177+）
- [ ] 新增 `test_user_state.py` 覆盖画像/好感度/心情
- [ ] 新增 `test_qrlogin_v2.py` 覆盖多账号登录
- [ ] 新增 `test_config_reload.py` 覆盖热重载

---

## 10. 附录

### 10.1 文件变更清单

| 文件 | 操作 |
|------|------|
| `bilibot/user_state.py` | 新建 |
| `bilibot/memory.py` | 删除 |
| `bilibot/scheduler.py` | 修改 |
| `bilibot/reply.py` | 修改 |
| `bilibot/account/instance.py` | 修改 |
| `bilibot/context_builder.py` | 修改 |
| `bilibot/services/comment_context.py` | 修改 |
| `bilibot/bilibili_qrlogin.py` | 修改 |
| `bilibot/bilibili_api.py` | 修改 |
| `bilibot/knowledge_memory.py` | 修改 |
| `bilibot/services/web_search.py` | 修改 |
| `bilibot/humanized_behavior.py` | 修改 |
| `bilibot/api/config.py` | 修改 |
| `bilibot/app/app.py` | 修改 |
| `bilibot/account/manager.py` | 修改 |
| `tests/test_memory_system.py` | 删除/重写 |
| `tests/test_user_state.py` | 新建 |

### 10.2 配置变更

无新增配置项。记忆系统的 `max_today/max_recent/max_long_term/enable_forgetting/forgetting_score/thread_compress_threshold` 等字段在重构后部分失效（`KnowledgeBaseMemory` 用 TTL 衰减），但保留在 yaml 中以备未来实现。
