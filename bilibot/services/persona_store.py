"""
人格库服务

支持：
- 保存多个人格
- 手动切换当前人格
- 人格CRUD操作
- 导入/导出
"""
import json
import logging
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any

from ..models import Persona, PersonaExample

logger = logging.getLogger("bilibot.persona_store")


class PersonaStore:
    """人格库管理器

    支持两种账号绑定模式：
    1. 旧模式（V2）：account_id -> persona_id（字符串），一对一
    2. 新模式（profile）：account_id -> {active_persona_id, profile_id}
       - profile_id 引用 config.yaml 中的 profiles[] 项
       - active_persona_id 是 profile.personas 之一，可运行时切换
       - 未设 active_persona_id 时回退到 profile.default_persona

    account_personas.json 新格式：
    {
      "acc_001": {"active_persona_id": "tech", "profile_id": "tech_bot"},
      "acc_002": "casual"  ← 旧格式兼容（视为单人格绑定）
    }
    """

    def __init__(self, data_dir: str = "./data", config_loader=None):
        self.data_dir = data_dir
        # PRD V3 §7（profiles 多人格组）：注入 config_loader 用于读取 profiles 配置
        self._config_loader = config_loader
        self._personas: Dict[str, Persona] = {}
        self._current_id: Optional[str] = None
        self._load()
    
    def _load(self):
        """从文件加载人格数据"""
        import os
        os.makedirs(self.data_dir, exist_ok=True)
        personas_file = os.path.join(self.data_dir, "personas.json")
        
        if os.path.exists(personas_file):
            try:
                with open(personas_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._current_id = data.get("current_persona_id")
                    for pdata in data.get("personas", []):
                        p = Persona.from_dict(pdata)
                        self._personas[p.id] = p
                    logger.info(f"加载了 {len(self._personas)} 个人格")
            except Exception as e:
                logger.error(f"加载人格数据失败: {e}")
        
        # 如果没有人格，创建默认人格
        if not self._personas:
            self._create_default_persona()
    
    def _save(self):
        """保存人格数据"""
        import os
        os.makedirs(self.data_dir, exist_ok=True)
        personas_file = os.path.join(self.data_dir, "personas.json")
        
        data = {
            "current_persona_id": self._current_id,
            "personas": [p.to_dict() for p in self._personas.values()]
        }
        
        with open(personas_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def _create_default_persona(self):
        """创建默认人格"""
        now = datetime.now().isoformat()
        reply_rules = (
            "回复规则：\n"
            "1. 回复要简短自然，不超过100字\n"
            "2. 不要重复之前说过的内容\n"
            "3. 根据视频内容和评论主题回复\n"
            "4. 对熟悉的用户可以稍微调侃"
        )
        proactive_rules = (
            "主动评论规则：\n"
            "1. 评论要有具体内容，不要万能回复\n"
            "2. 要引用视频的具体部分\n"
            "3. 表达真实主观感受"
        )
        dynamic_rules = (
            "动态发布规则：\n"
            "1. 文字要自然，不要像广告\n"
            "2. 可以分享看视频的感受\n"
            "3. 适当互动，不要只发链接"
        )
        weekly_rules = (
            "周总结规则：\n"
            "1. 总结本周看过的好视频\n"
            "2. 分享一些感悟\n"
            "3. 语言要轻松自然"
        )
        
        default = Persona(
            id="default",
            name="默认人格",
            description="BiliBot 默认互动人格",
            base_prompt=(
                "你是一个活泼可爱的B站用户，喜欢在B站看视频、评论、和大家聊天。\n"
                "你的性格特点：\n"
                "- 说话自然，像真人一样\n"
                "- 会用一些网络用语\n"
                "- 有自己的观点和喜好\n"
                "- 对喜欢的事物会热情表达"
            ),
            speaking_style="活泼、自然、有趣",
            boundaries="不讨论政治敏感话题，不透露个人信息",
            reply_rules=reply_rules,
            proactive_comment_rules=proactive_rules,
            dynamic_rules=dynamic_rules,
            weekly_rules=weekly_rules,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        self._personas["default"] = default
        self._current_id = "default"
        self._save()
        logger.info("创建了默认人格")
    
    # ===== CRUD 操作 =====
    
    def list_personas(self) -> List[Dict[str, Any]]:
        """列出所有人格"""
        return [
            {
                **p.to_dict(),
                "is_current": p.id == self._current_id
            }
            for p in self._personas.values()
        ]
    
    def get_persona(self, persona_id: str) -> Optional[Dict[str, Any]]:
        """获取指定人格"""
        p = self._personas.get(persona_id)
        if p:
            return {**p.to_dict(), "is_current": p.id == self._current_id}
        return None
    
    def create_persona(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """创建新人格"""
        # 生成唯一ID
        pid = data.get("id") or data.get("name", "persona").lower().replace(" ", "_")
        if pid in self._personas:
            pid = f"{pid}_{uuid.uuid4().hex[:6]}"
        
        now = datetime.now().isoformat()
        examples = [
            PersonaExample(**e) for e in data.get("examples", [])
        ]
        
        persona = Persona(
            id=pid,
            name=data.get("name", "新人格"),
            description=data.get("description", ""),
            base_prompt=data.get("base_prompt", ""),
            speaking_style=data.get("speaking_style", ""),
            boundaries=data.get("boundaries", ""),
            relationship_rules=data.get("relationship_rules", ""),
            reply_rules=data.get("reply_rules", ""),
            proactive_comment_rules=data.get("proactive_comment_rules", ""),
            dynamic_rules=data.get("dynamic_rules", ""),
            weekly_rules=data.get("weekly_rules", ""),
            examples=examples,
            enabled=data.get("enabled", True),
            created_at=now,
            updated_at=now,
        )
        
        self._personas[pid] = persona
        self._save()
        logger.info(f"创建了人格: {persona.name}")
        return persona.to_dict()
    
    def update_persona(self, persona_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """更新人格"""
        if persona_id not in self._personas:
            return None
        
        p = self._personas[persona_id]
        
        # 更新字段
        for field in ["name", "description", "base_prompt", "speaking_style", 
                      "boundaries", "relationship_rules", "reply_rules",
                      "proactive_comment_rules", "dynamic_rules", "weekly_rules",
                      "enabled"]:
            if field in data:
                setattr(p, field, data[field])
        
        if "examples" in data:
            p.examples = [PersonaExample(**e) for e in data["examples"]]
        
        p.updated_at = datetime.now().isoformat()
        self._save()
        logger.info(f"更新了人格: {p.name}")
        return p.to_dict()
    
    def delete_persona(self, persona_id: str) -> bool:
        """删除人格"""
        if persona_id not in self._personas:
            return False
        
        # 不能删除当前人格
        if persona_id == self._current_id:
            logger.warning(f"不能删除当前人格: {persona_id}")
            return False
        
        p = self._personas.pop(persona_id)
        self._save()
        logger.info(f"删除了人格: {p.name}")
        return True
    
    def copy_persona(self, persona_id: str, new_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """复制人格"""
        if persona_id not in self._personas:
            return None
        
        p = self._personas[persona_id]
        new_id = f"copy_{uuid.uuid4().hex[:6]}"
        
        now = datetime.now().isoformat()
        copied = Persona(
            id=new_id,
            name=new_name or f"{p.name} (副本)",
            description=p.description,
            base_prompt=p.base_prompt,
            speaking_style=p.speaking_style,
            boundaries=p.boundaries,
            relationship_rules=p.relationship_rules,
            reply_rules=p.reply_rules,
            proactive_comment_rules=p.proactive_comment_rules,
            dynamic_rules=p.dynamic_rules,
            weekly_rules=p.weekly_rules,
            examples=list(p.examples),
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        
        self._personas[new_id] = copied
        self._save()
        logger.info(f"复制了人格: {p.name} -> {copied.name}")
        return copied.to_dict()
    
    # ===== 当前人格 =====
    
    def get_current(self) -> Optional[Persona]:
        """获取当前人格"""
        if self._current_id and self._current_id in self._personas:
            return self._personas[self._current_id]
        # 返回第一个可用人格
        for p in self._personas.values():
            if p.enabled:
                return p
        return None
    
    def get_current_dict(self) -> Optional[Dict[str, Any]]:
        """获取当前人格（字典格式）"""
        p = self.get_current()
        if p:
            return {**p.to_dict(), "is_current": True}
        return None
    
    def set_current(self, persona_id: str) -> bool:
        """设置当前人格"""
        if persona_id not in self._personas:
            return False
        
        if not self._personas[persona_id].enabled:
            logger.warning(f"不能激活禁用的的人格: {persona_id}")
            return False
        
        self._current_id = persona_id
        self._save()
        logger.info(f"切换当前人格: {self._personas[persona_id].name}")
        return True
    
    # ===== 导入/导出 =====
    
    def export_persona(self, persona_id: str) -> Optional[Dict[str, Any]]:
        """导出人格"""
        if persona_id not in self._personas:
            return None
        return self._personas[persona_id].to_dict()
    
    def import_persona(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """导入人格"""
        # 生成新ID避免冲突
        data["id"] = f"import_{uuid.uuid4().hex[:6]}"
        return self.create_persona(data)
    
    # ===== Prompt 预览 =====
    
    def preview_system_prompt(self, persona_id: Optional[str] = None,
                              scene: str = "reply_comment") -> str:
        """预览最终系统提示词

        PRD V4 §4.6.3：与真实 Orchestrator 一致。
        内部调用 normalize_scene 规范化字符串 scene，确保 scene 规则不漏注入。
        """
        # PRD V4 §4.6.3：与真实 Orchestrator 一致，先规范化 scene
        try:
            from ..prompts.orchestrator import normalize_scene
            scene_enum = normalize_scene(scene)
        except Exception:
            from ..models import SceneType
            scene_enum = SceneType.REPLY_COMMENT

        if persona_id:
            p = self._personas.get(persona_id)
        else:
            p = self.get_current()

        if not p:
            return "当前没有激活的人格"

        parts = []
        parts.append(f"【人格名称】{p.name}")
        if p.description:
            parts.append(f"【人格描述】{p.description}")
        parts.append(f"\n【基础人设】\n{p.base_prompt}")

        if p.speaking_style:
            parts.append(f"\n【说话风格】\n{p.speaking_style}")

        if p.boundaries:
            parts.append(f"\n【禁止事项】\n{p.boundaries}")

        if p.relationship_rules:
            parts.append(f"\n【关系规则】\n{p.relationship_rules}")

        # 场景特定规则（用规范化后的 scene_enum）
        scene_rules = p.get_rules_for_scene(scene_enum)
        if scene_rules:
            parts.append(f"\n【{scene_enum.value} 规则】\n{scene_rules}")

        if p.examples:
            parts.append("\n【示例】")
            for ex in p.examples[:3]:
                parts.append(f"输入: {ex.input}")
                parts.append(f"输出: {ex.output}")

        return "\n".join(parts)

    # ===== 账号-人格绑定（PRD V2 多账号架构 + PRD V3 §7 profiles 多人格组） =====

    def set_config_loader(self, config_loader):
        """注入 config_loader（用于读取 profiles 配置，可在 app 启动后补注入）"""
        self._config_loader = config_loader

    def _account_bindings_path(self) -> str:
        import os
        return os.path.join(self.data_dir, "account_personas.json")

    def _load_account_bindings(self) -> Dict[str, Any]:
        """加载 account_id -> 绑定信息的映射

        兼容两种格式：
        - 旧：{"acc_001": "persona_id"}（字符串）
        - 新：{"acc_001": {"active_persona_id": "...", "profile_id": "..."}}
        返回时统一为 dict 格式（旧格式转换为 {"active_persona_id": pid, "profile_id": None}）
        """
        import os
        path = self._account_bindings_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f) or {}
                # 归一化旧格式
                normalized: Dict[str, Any] = {}
                for acc_id, val in raw.items():
                    if isinstance(val, str):
                        normalized[acc_id] = {"active_persona_id": val, "profile_id": None}
                    elif isinstance(val, dict):
                        normalized[acc_id] = {
                            "active_persona_id": val.get("active_persona_id"),
                            "profile_id": val.get("profile_id"),
                        }
                    else:
                        normalized[acc_id] = {"active_persona_id": None, "profile_id": None}
                return normalized
            except Exception as e:
                logger.warning(f"加载账号人格绑定失败: {e}")
        return {}

    def _save_account_bindings(self, bindings: Dict[str, Any]):
        import os
        os.makedirs(self.data_dir, exist_ok=True)
        path = self._account_bindings_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(bindings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存账号人格绑定失败: {e}")

    # ── Profile 相关 ──

    def _load_profiles_from_config(self) -> List[Dict[str, Any]]:
        """从 config_loader 读取 profiles 列表"""
        if not self._config_loader:
            return []
        try:
            raw = self._config_loader.get_raw_config()
            profiles = raw.get("profiles", []) or []
            return [p for p in profiles if isinstance(p, dict) and p.get("id")]
        except Exception as e:
            logger.warning(f"读取 profiles 配置失败: {e}")
            return []

    def get_profile(self, profile_id: str) -> Optional[Dict[str, Any]]:
        """获取指定 profile 配置"""
        for p in self._load_profiles_from_config():
            if p.get("id") == profile_id:
                return p
        return None

    def list_profiles(self) -> List[Dict[str, Any]]:
        """列出所有 profile（附带可用人格详情）"""
        profiles = self._load_profiles_from_config()
        result = []
        for p in profiles:
            item = {
                "id": p.get("id"),
                "name": p.get("name", p.get("id")),
                "default_persona": p.get("default_persona", ""),
                "personas": [],
            }
            for pid in p.get("personas", []) or []:
                persona_dict = self.get_persona(pid)
                if persona_dict:
                    item["personas"].append({
                        "id": pid,
                        "name": persona_dict.get("name", pid),
                        "is_default": pid == item["default_persona"],
                    })
            result.append(item)
        return result

    def get_available_personas_for_account(self, account_id: str) -> List[Dict[str, Any]]:
        """返回账号可用的人格列表（基于绑定的 profile.personas）

        如果账号未绑定 profile，返回当前所有人格列表。
        """
        bindings = self._load_account_bindings()
        b = bindings.get(account_id, {})
        profile_id = b.get("profile_id") if isinstance(b, dict) else None
        if profile_id:
            profile = self.get_profile(profile_id)
            if profile:
                persona_ids = profile.get("personas", []) or []
                result = []
                for pid in persona_ids:
                    p = self._personas.get(pid)
                    if p:
                        result.append({
                            "id": p.id,
                            "name": p.name,
                            "is_default": pid == profile.get("default_persona"),
                        })
                return result
        # 未绑定 profile 或 profile 不存在 → 返回所有人格
        return [
            {"id": p.id, "name": p.name, "is_default": p.id == self._current_id}
            for p in self._personas.values() if p.enabled
        ]

    # ── 账号-人格绑定（旧接口兼容） ──

    def set_account_persona(self, account_id: str, persona_id: str) -> bool:
        """绑定账号到指定人格（旧接口：单人格绑定）

        新版行为：清除 profile_id 绑定，仅设置 active_persona_id。
        """
        if persona_id not in self._personas:
            logger.warning(f"人格不存在: {persona_id}")
            return False
        bindings = self._load_account_bindings()
        bindings[account_id] = {"active_persona_id": persona_id, "profile_id": None}
        self._save_account_bindings(bindings)
        logger.info(f"账号 {account_id} 绑定人格: {persona_id}")
        return True

    def set_account_profile(self, account_id: str, profile_id: str,
                            persona_id: Optional[str] = None) -> bool:
        """绑定账号到 profile（PRD V3 §7 新接口）

        Args:
            account_id: 账号 ID
            profile_id: profile ID
            persona_id: 可选的激活人格 ID；未指定时用 profile.default_persona
        """
        profile = self.get_profile(profile_id)
        if not profile:
            logger.warning(f"profile 不存在: {profile_id}")
            return False
        # 决定 active_persona_id
        active = persona_id or profile.get("default_persona", "")
        # 校验 active 在 profile.personas 内
        personas = profile.get("personas", []) or []
        if active and active not in personas:
            logger.warning(f"人格 {active} 不在 profile {profile_id} 的 personas 列表中")
            return False
        if active and active not in self._personas:
            logger.warning(f"人格 {active} 在 personas.json 中不存在")
            return False
        bindings = self._load_account_bindings()
        bindings[account_id] = {"active_persona_id": active, "profile_id": profile_id}
        self._save_account_bindings(bindings)
        logger.info(f"账号 {account_id} 绑定 profile={profile_id}, active={active}")
        return True

    def set_account_active_persona(self, account_id: str, persona_id: str) -> bool:
        """切换账号当前激活的人格（web 面板切换人格用）

        校验：
        - 人格必须存在
        - 如账号绑定了 profile，人格必须在 profile.personas 列表内
        """
        if persona_id not in self._personas:
            logger.warning(f"人格不存在: {persona_id}")
            return False
        bindings = self._load_account_bindings()
        b = bindings.get(account_id, {})
        profile_id = b.get("profile_id") if isinstance(b, dict) else None
        if profile_id:
            profile = self.get_profile(profile_id)
            if profile and persona_id not in (profile.get("personas") or []):
                logger.warning(f"人格 {persona_id} 不在 profile {profile_id} 的 personas 列表内")
                return False
        # 更新 active_persona_id
        new_binding = {
            "active_persona_id": persona_id,
            "profile_id": profile_id,
        }
        bindings[account_id] = new_binding
        self._save_account_bindings(bindings)
        logger.info(f"账号 {account_id} 切换激活人格: {persona_id}")
        return True

    def unset_account_persona(self, account_id: str) -> bool:
        """解除账号的人格绑定（回退到当前默认人格）"""
        bindings = self._load_account_bindings()
        if account_id in bindings:
            del bindings[account_id]
            self._save_account_bindings(bindings)
            return True
        return False

    def get_account_persona_id(self, account_id: str) -> Optional[str]:
        """获取账号当前激活的人格 ID

        优先级：
        1. account_personas.json 中的 active_persona_id（web 可切换）
        2. 账号绑定 profile 的 default_persona
        3. 旧格式 account_personas.json 中的 persona_id 字符串
        4. 当前默认人格 _current_id
        """
        bindings = self._load_account_bindings()
        b = bindings.get(account_id)
        if b:
            # 新格式 dict
            if isinstance(b, dict):
                active = b.get("active_persona_id")
                if active and active in self._personas:
                    return active
                # active 缺失或失效 → 回退到 profile.default_persona
                profile_id = b.get("profile_id")
                if profile_id:
                    profile = self.get_profile(profile_id)
                    if profile:
                        default_pid = profile.get("default_persona", "")
                        if default_pid and default_pid in self._personas:
                            return default_pid
            # 旧格式字符串（已被 _load_account_bindings 归一化为 dict，这里是防御性代码）
            elif isinstance(b, str) and b in self._personas:
                return b
        # 回退到当前默认
        return self._current_id

    def get_account_persona_status(self, account_id: str) -> Dict[str, Any]:
        """返回账号人格绑定的完整状态（web 面板用）"""
        bindings = self._load_account_bindings()
        b = bindings.get(account_id, {}) or {}
        profile_id = b.get("profile_id") if isinstance(b, dict) else None
        active_id = self.get_account_persona_id(account_id)
        return {
            "account_id": account_id,
            "profile_id": profile_id,
            "active_persona_id": active_id,
            "available_personas": self.get_available_personas_for_account(account_id),
        }

    def get_persona_for_account(self, account_id: str) -> Optional[Persona]:
        """获取账号对应的 Persona 对象"""
        pid = self.get_account_persona_id(account_id)
        if pid:
            return self._personas.get(pid)
        return self.get_current()

    def resolve_persona(self, account_id: str = "", task_persona_id: str = "") -> Optional[Persona]:
        """PRD V4 ACC-002：统一人格解析（全链路接入）

        优先级：
        1. 任务显式指定的 persona_id，且属于账号可用人格池
        2. account_personas.json 中账号当前激活人格
        3. 账号绑定 Profile 的 default_persona
        4. 账号兼容字段 persona_id
        5. 全局默认人格（最后降级）

        Args:
            account_id: 账号 ID
            task_persona_id: 任务显式指定的人格 ID（优先级最高，但需在可用池内）
        """
        # 1. 任务显式指定（需属于账号可用人格池）
        if task_persona_id and task_persona_id in self._personas:
            if account_id:
                available = self.get_available_personas_for_account(account_id)
                available_ids = {p.get("id") for p in available}
                # 可用池为空表示未绑定 profile，允许使用任意人格
                if not available_ids or task_persona_id in available_ids:
                    return self._personas[task_persona_id]
            else:
                return self._personas[task_persona_id]

        # 2-4. 账号级解析（get_persona_for_account 已实现优先级 2→5）
        if account_id:
            persona = self.get_persona_for_account(account_id)
            if persona:
                return persona

        # 5. 全局默认
        return self.get_current()

    def build_scene_prompt(
        self,
        scene: str = "reply_comment",
        account_id: Optional[str] = None,
        persona_id: Optional[str] = None,
    ) -> str:
        """
        构建场景系统提示词

        优先级：persona_id > account_id 绑定 > 当前默认人格
        """
        # 解析 persona_id
        resolved_pid = persona_id
        if not resolved_pid and account_id:
            resolved_pid = self.get_account_persona_id(account_id)
        # 委托给 preview_system_prompt（已实现 scene 规范化与拼装）
        return self.preview_system_prompt(persona_id=resolved_pid, scene=scene)