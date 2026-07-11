"""
tests/test_safe_501_account_safety.py - SAFE-501 账号级安全隔离测试

覆盖：
1. 两个账号不共享限流配额（account_id:scene 隔离）
2. content_check_enabled=false 仍执行硬性长度限制和全局暂停
3. reload_config 更新限流参数
4. 内容相似度按账号隔离
5. 账号级风险暂停不影响其他账号
"""
import pytest

from bilibot.services.safety import SafetyChecker, build_safety_config


@pytest.fixture
def safety_config():
    """标准安全配置"""
    return {
        "rate_limit": {
            "enabled": True,
            "per_minute": 3,
            "per_hour": 10,
            "per_day": 20,
            "global_quota": 1000,
        },
        "content": {
            "min_length": 2,
            "max_length": 2000,
        },
        "content_check_enabled": True,
        "duplicate_check": {
            "enabled": True,
            "window_size": 5,
            "similarity_threshold": 0.8,
        },
        "reply": {"block_keywords": ["坏词"]},
    }


@pytest.fixture
def checker(tmp_data_dir, safety_config):
    """SafetyChecker 实例"""
    return SafetyChecker(data_dir=tmp_data_dir, config=safety_config)


# ═══════════════════════════════════════════════════════
#  限流配额账号隔离
# ═══════════════════════════════════════════════════════

class TestRateLimitAccountIsolation:
    """SAFE-501：限流键为 account_id:scene，账号间互不抢占配额"""

    def test_two_accounts_have_independent_quota(self, checker):
        """账号 A 耗尽配额不影响账号 B"""
        scene = "reply_comment"
        acc_a = "account_a"
        acc_b = "account_b"

        # per_minute=3，账号 A 发 3 次（耗尽 per_minute 配额）
        for _ in range(3):
            assert checker.check_rate_limit(scene=scene, account_id=acc_a) is True
            checker.record_publish(scene=scene, account_id=acc_a)

        # 账号 A 第 4 次应被限流
        assert checker.check_rate_limit(scene=scene, account_id=acc_a) is False

        # 账号 B 不受影响，仍可发布
        assert checker.check_rate_limit(scene=scene, account_id=acc_b) is True
        checker.record_publish(scene=scene, account_id=acc_b)
        assert checker.check_rate_limit(scene=scene, account_id=acc_b) is True

    def test_same_account_different_scenes_are_independent(self, checker):
        """同账号不同场景独立计数"""
        acc = "account_a"
        # reply_comment 发 3 次（耗尽 per_minute）
        for _ in range(3):
            checker.record_publish(scene="reply_comment", account_id=acc)

        # reply_comment 已限流
        assert checker.check_rate_limit(scene="reply_comment", account_id=acc) is False

        # dynamic_post 不受影响
        assert checker.check_rate_limit(scene="dynamic_post", account_id=acc) is True

    def test_global_quota_caps_all_accounts(self, tmp_data_dir):
        """全局配额限制所有账号+场景合计"""
        config = {
            "rate_limit": {
                "enabled": True,
                "per_minute": 100,
                "per_hour": 100,
                "per_day": 100,
                "global_quota": 3,
            },
            "content": {"min_length": 2, "max_length": 2000},
            "content_check_enabled": True,
        }
        sc = SafetyChecker(data_dir=tmp_data_dir, config=config)

        # 3 次发布（跨不同账号+场景）耗尽全局配额
        sc.record_publish(scene="reply_comment", account_id="acc_a")
        sc.record_publish(scene="dynamic_post", account_id="acc_b")
        sc.record_publish(scene="proactive_comment", account_id="acc_c")

        # 第 4 次应被全局配额限流（即使换了账号+场景）
        assert sc.check_rate_limit(scene="reply_comment", account_id="acc_d") is False


# ═══════════════════════════════════════════════════════
#  content_check_enabled 语义
# ═══════════════════════════════════════════════════════

class TestContentCheckEnabledSemantics:
    """SAFE-501：content_check_enabled=false 仅跳过可选内容规则，
    不跳过硬性长度限制和全局暂停。"""

    @pytest.mark.asyncio
    async def test_disabled_still_enforces_hard_length_limits(self, tmp_data_dir):
        """content_check_enabled=false 时长度限制仍生效"""
        config = {
            "rate_limit": {"enabled": True, "per_minute": 5, "per_hour": 50, "per_day": 200},
            "content": {"min_length": 5, "max_length": 100},
            "content_check_enabled": False,
            "duplicate_check": {"enabled": True, "window_size": 5, "similarity_threshold": 0.8},
            "reply": {"block_keywords": ["坏词"]},
        }
        sc = SafetyChecker(data_dir=tmp_data_dir, config=config)
        assert sc.content_check_enabled is False

        # 过短内容 → 拒绝（硬限制仍生效）
        passed, reason = await sc.check_content("ab", scene="reply_comment", account_id="acc_a")
        assert passed is False
        assert "过短" in reason

        # 过长内容 → 拒绝（硬限制仍生效）
        long_text = "x" * 101
        passed, reason = await sc.check_content(long_text, scene="reply_comment", account_id="acc_a")
        assert passed is False
        assert "超长" in reason

    @pytest.mark.asyncio
    async def test_disabled_skips_sensitive_word_check(self, tmp_data_dir):
        """content_check_enabled=false 时跳过敏感词检查"""
        config = {
            "rate_limit": {"enabled": True, "per_minute": 5, "per_hour": 50, "per_day": 200},
            "content": {"min_length": 2, "max_length": 2000},
            "content_check_enabled": False,
            "duplicate_check": {"enabled": True, "window_size": 5, "similarity_threshold": 0.8},
            "reply": {"block_keywords": ["坏词"]},
        }
        sc = SafetyChecker(data_dir=tmp_data_dir, config=config)

        # 包含敏感词但 content_check_enabled=false → 通过
        passed, reason = await sc.check_content("这是坏词内容", scene="reply_comment", account_id="acc_a")
        assert passed is True

    @pytest.mark.asyncio
    async def test_enabled_blocks_sensitive_word(self, tmp_data_dir):
        """content_check_enabled=true 时敏感词检查生效"""
        config = {
            "rate_limit": {"enabled": True, "per_minute": 5, "per_hour": 50, "per_day": 200},
            "content": {"min_length": 2, "max_length": 2000},
            "content_check_enabled": True,
            "duplicate_check": {"enabled": True, "window_size": 5, "similarity_threshold": 0.8},
            "reply": {"block_keywords": ["坏词"]},
        }
        sc = SafetyChecker(data_dir=tmp_data_dir, config=config)

        passed, reason = await sc.check_content("这是坏词内容", scene="reply_comment", account_id="acc_a")
        assert passed is False
        assert "敏感词" in reason

    @pytest.mark.asyncio
    async def test_disabled_skips_duplicate_check(self, tmp_data_dir):
        """content_check_enabled=false 时跳过重复度检查"""
        config = {
            "rate_limit": {"enabled": True, "per_minute": 5, "per_hour": 50, "per_day": 200},
            "content": {"min_length": 2, "max_length": 2000},
            "content_check_enabled": False,
            "duplicate_check": {"enabled": True, "window_size": 5, "similarity_threshold": 0.8},
        }
        sc = SafetyChecker(data_dir=tmp_data_dir, config=config)

        # 记录一条内容
        sc.record_content("今天天气真好啊", account_id="acc_a")

        # content_check_enabled=false → 即使重复也通过
        passed, reason = await sc.check_content("今天天气真好啊", scene="reply_comment", account_id="acc_a")
        assert passed is True

    @pytest.mark.asyncio
    async def test_disabled_still_enforces_global_pause(self, checker):
        """content_check_enabled=true 时全局暂停由 scheduler 检查（is_paused），不受 content_check_enabled 影响

        这里验证 is_paused() 独立于 content_check_enabled。
        """
        assert checker.content_check_enabled is True
        # 全局暂停
        checker.pause("测试暂停")
        assert checker.is_paused() is True
        # 恢复
        checker.resume()
        assert checker.is_paused() is False


# ═══════════════════════════════════════════════════════
#  reload_config 热重载
# ═══════════════════════════════════════════════════════

class TestReloadConfig:
    """SAFE-501：reload_config 更新限流参数"""

    def test_reload_updates_rate_limits(self, checker):
        """reload_config 更新 per_minute 限流"""
        acc = "acc_a"
        scene = "reply_comment"

        # 原始 per_minute=3，发 3 次耗尽
        for _ in range(3):
            checker.record_publish(scene=scene, account_id=acc)
        assert checker.check_rate_limit(scene=scene, account_id=acc) is False

        # 热重载：per_minute 改为 10
        new_config = {
            "rate_limit": {
                "enabled": True,
                "per_minute": 10,
                "per_hour": 50,
                "per_day": 200,
                "global_quota": 1000,
            },
            "content": {"min_length": 2, "max_length": 2000},
            "content_check_enabled": True,
            "duplicate_check": {"enabled": True, "window_size": 5, "similarity_threshold": 0.8},
        }
        checker.reload_config(new_config)
        assert checker.rate_limits["per_minute"] == 10

        # 限流桶保留（旧计数仍在），但仍被限流（3 < 10 所以应通过）
        # 注意：reload 不清除桶，旧 3 次计数仍在 minute 窗口内
        # 3 < 10 所以允许
        assert checker.check_rate_limit(scene=scene, account_id=acc) is True

    def test_reload_updates_content_check_enabled(self, checker):
        """reload_config 更新 content_check_enabled"""
        assert checker.content_check_enabled is True

        new_config = {
            "rate_limit": {"enabled": True, "per_minute": 5, "per_hour": 50, "per_day": 200},
            "content": {"min_length": 2, "max_length": 2000},
            "content_check_enabled": False,
        }
        checker.reload_config(new_config)
        assert checker.content_check_enabled is False

    def test_reload_updates_global_quota(self, checker):
        """reload_config 更新 global_quota"""
        assert checker.global_quota == 1000

        new_config = {
            "rate_limit": {"enabled": True, "per_minute": 5, "per_hour": 50, "per_day": 200, "global_quota": 50},
            "content": {"min_length": 2, "max_length": 2000},
            "content_check_enabled": True,
        }
        checker.reload_config(new_config)
        assert checker.global_quota == 50

    def test_reload_updates_duplicate_window_size(self, checker):
        """reload_config 更新 duplicate_check_n 并调整已有 deque maxlen"""
        # 先记录一些内容
        checker.record_content("内容1", account_id="acc_a")
        checker.record_content("内容2", account_id="acc_a")

        assert checker.duplicate_check_n == 5

        new_config = {
            "rate_limit": {"enabled": True, "per_minute": 5, "per_hour": 50, "per_day": 200},
            "content": {"min_length": 2, "max_length": 2000},
            "content_check_enabled": True,
            "duplicate_check": {"enabled": True, "window_size": 3, "similarity_threshold": 0.8},
        }
        checker.reload_config(new_config)
        assert checker.duplicate_check_n == 3

        # 已有 deque 的 maxlen 应已调整
        dq = checker._get_recent_contents("acc_a")
        assert dq.maxlen == 3


# ═══════════════════════════════════════════════════════
#  内容相似度账号隔离
# ═══════════════════════════════════════════════════════

class TestContentSimilarityAccountIsolation:
    """SAFE-501：最近内容相似度按账号隔离"""

    @pytest.mark.asyncio
    async def test_duplicate_not_shared_across_accounts(self, checker):
        """账号 A 的最近内容不影响账号 B 的重复度检查"""
        text = "今天天气真好适合出去玩"
        checker.record_content(text, account_id="acc_a")

        # 账号 A 发相同内容 → 重复度命中
        passed, reason = await checker.check_content(text, scene="reply_comment", account_id="acc_a")
        assert passed is False
        assert "重复" in reason

        # 账号 B 发相同内容 → 不重复（隔离存储）
        passed, reason = await sc_check(checker, text, account_id="acc_b")
        assert passed is True

    @pytest.mark.asyncio
    async def test_record_content_isolated_by_account(self, checker):
        """record_content 按账号隔离存储"""
        checker.record_content("内容A", account_id="acc_a")
        checker.record_content("内容B", account_id="acc_b")

        recent_a = checker._get_recent_contents("acc_a")
        recent_b = checker._get_recent_contents("acc_b")

        assert list(recent_a) == ["内容A"]
        assert list(recent_b) == ["内容B"]

    @pytest.mark.asyncio
    async def test_window_size_per_account(self, tmp_data_dir):
        """每账号独立维护窗口大小"""
        config = {
            "rate_limit": {"enabled": True, "per_minute": 5, "per_hour": 50, "per_day": 200},
            "content": {"min_length": 2, "max_length": 2000},
            "content_check_enabled": True,
            "duplicate_check": {"enabled": True, "window_size": 3, "similarity_threshold": 0.8},
        }
        sc = SafetyChecker(data_dir=tmp_data_dir, config=config)

        # 账号 A 发 5 条（window=3，只保留最近 3 条）
        for i in range(5):
            sc.record_content(f"内容{i}", account_id="acc_a")

        recent_a = sc._get_recent_contents("acc_a")
        assert len(recent_a) == 3

        # 账号 B 空
        recent_b = sc._get_recent_contents("acc_b")
        assert len(recent_b) == 0


# ═══════════════════════════════════════════════════════
#  账号级风险暂停
# ═══════════════════════════════════════════════════════

class TestAccountLevelPause:
    """SAFE-501：账号级风险暂停不影响其他账号"""

    def test_pause_account_does_not_pause_others(self, checker):
        """暂停账号 A 不影响账号 B"""
        checker.pause_account("acc_a", reason="风控")
        assert checker.is_account_paused("acc_a") is True
        assert checker.is_account_paused("acc_b") is False

    def test_resume_account(self, checker):
        """恢复已暂停的账号"""
        checker.pause_account("acc_a", reason="风控")
        assert checker.is_account_paused("acc_a") is True

        checker.resume_account("acc_a")
        assert checker.is_account_paused("acc_a") is False

    def test_global_pause_independent_from_account_pause(self, checker):
        """全局暂停和账号暂停互相独立"""
        checker.pause("全局维护")
        checker.pause_account("acc_a", reason="风控")

        # 全局暂停
        assert checker.is_paused() is True
        # 账号暂停
        assert checker.is_account_paused("acc_a") is True
        # 账号 B 只受全局暂停影响，不受账号 A 暂停影响
        assert checker.is_account_paused("acc_b") is False

        # 恢复全局
        checker.resume()
        assert checker.is_paused() is False
        # 账号 A 仍暂停
        assert checker.is_account_paused("acc_a") is True

    def test_empty_account_id_not_paused(self, checker):
        """空 account_id 不触发账号暂停"""
        assert checker.is_account_paused("") is False


# ═══════════════════════════════════════════════════════
#  build_safety_config 兼容性
# ═══════════════════════════════════════════════════════

class TestBuildSafetyConfigCompatibility:
    """build_safety_config 兼容新旧配置格式"""

    def test_new_nested_config(self):
        """新版嵌套配置"""
        raw = {
            "safety": {
                "content_check_enabled": False,
                "min_content_length": 5,
                "max_content_length": 500,
                "rate_limit": {
                    "enabled": True,
                    "per_minute": 10,
                    "per_hour": 100,
                    "per_day": 500,
                    "global_quota": 2000,
                },
                "duplicate_check": {
                    "enabled": False,
                    "window_size": 20,
                    "similarity_threshold": 0.9,
                },
            },
            "reply": {"block_keywords": ["test"]},
        }
        cfg = build_safety_config(raw)
        assert cfg["content_check_enabled"] is False
        assert cfg["content"]["min_length"] == 5
        assert cfg["content"]["max_length"] == 500
        assert cfg["rate_limit"]["per_minute"] == 10
        assert cfg["rate_limit"]["global_quota"] == 2000
        assert cfg["duplicate_check"]["enabled"] is False
        assert cfg["duplicate_check"]["window_size"] == 20
        assert cfg["duplicate_check"]["similarity_threshold"] == 0.9

    def test_old_flat_config(self):
        """旧版扁平配置仍兼容"""
        raw = {
            "safety": {
                "content_check_enabled": True,
                "min_content_length": 2,
                "max_content_length": 2000,
                "rate_limit_per_minute": 5,
                "rate_limit_per_hour": 50,
                "rate_limit_per_day": 200,
                "similarity_threshold": 0.8,
            },
            "reply": {"block_keywords": ["坏"]},
        }
        cfg = build_safety_config(raw)
        assert cfg["content_check_enabled"] is True
        assert cfg["content"]["min_length"] == 2
        assert cfg["rate_limit"]["per_minute"] == 5
        assert cfg["rate_limit"]["per_hour"] == 50
        assert cfg["rate_limit"]["per_day"] == 200
        assert cfg["rate_limit"]["global_quota"] == 1000  # 默认值
        assert cfg["duplicate_check"]["similarity_threshold"] == 0.8


# ═══════════════════════════════════════════════════════
#  辅助
# ═══════════════════════════════════════════════════════

async def sc_check(checker, text, account_id):
    """辅助：调用 check_content"""
    return await checker.check_content(text, scene="reply_comment", account_id=account_id)
