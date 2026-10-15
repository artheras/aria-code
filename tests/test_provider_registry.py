"""
Layer 1 — 单元测试：Provider 注册中心
======================================
• _load_provider_cfg_from_file  — providers.json 读取
• _build_cfg                    — env + file 合并
• list_available_providers      — 可用列表
• 新 provider 注册验证

全部无网络、无真实 API key。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

_CLI_DIR = str(pathlib.Path(__file__).parents[1])
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

# conftest 中的辅助函数
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from conftest import make_providers_file

# Must be the *same* module object the names below are imported from. The
# package is importable under two roots (`providers.…` via src/aria_code and
# `aria_code.providers.…` via src), which are distinct module objects — patching
# one while the function under test reads the other silently does nothing.
import aria_code.providers.llm.registry as _reg
from aria_code.providers.llm.registry import (
    _DEFAULT_FALLBACK_CHAIN,
    _PROVIDER_CLASSES,
    _build_cfg,
    _load_provider_cfg_from_file,
    list_available_providers,
)

# 所有云端 provider 的 env 变量（清空以保证测试隔离）
_ALL_KEY_ENV_VARS = [
    "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "GROQ_API_KEY", "TOGETHER_API_KEY", "DASHSCOPE_API_KEY",
    "SILICONFLOW_API_KEY", "MOONSHOT_API_KEY", "ZHIPUAI_API_KEY",
]


# ── TestLoadProviderCfgFromFile ───────────────────────────────────────────────

class TestLoadProviderCfgFromFile(unittest.TestCase):
    """验证 _load_provider_cfg_from_file 正确读取临时文件。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _with_file(self, llm_section: dict):
        """写临时 providers.json 并返回 patch context（屏蔽真实 home）。"""
        p = make_providers_file(self.tmp, llm_section)
        return (
            patch.object(_reg, "_CONFIG_PATHS", [p]),
            patch("pathlib.Path.home", return_value=self.tmp / "_nohome"),
        )

    def test_reads_api_key(self):
        ctx1, ctx2 = self._with_file({"deepseek": {"api_key": "sk-test-123"}})
        with ctx1, ctx2:
            result = _load_provider_cfg_from_file("deepseek")
        self.assertEqual(result.get("api_key"), "sk-test-123")

    def test_reads_base_url(self):
        ctx1, ctx2 = self._with_file({"openai": {"api_key": "sk-x", "base_url": "https://myproxy.com"}})
        with ctx1, ctx2:
            result = _load_provider_cfg_from_file("openai")
        self.assertEqual(result.get("base_url"), "https://myproxy.com")

    def test_missing_provider_returns_empty(self):
        ctx1, ctx2 = self._with_file({"deepseek": {"api_key": "sk-x"}})
        with ctx1, ctx2:
            result = _load_provider_cfg_from_file("siliconflow")
        self.assertEqual(result, {})

    def test_nonexistent_file_returns_empty(self):
        with patch.object(_reg, "_CONFIG_PATHS", [self.tmp / "no_such_file.json"]), \
             patch("pathlib.Path.home", return_value=self.tmp / "_nohome"):
            result = _load_provider_cfg_from_file("deepseek")
        self.assertEqual(result, {})

    def test_empty_string_values_filtered(self):
        ctx1, ctx2 = self._with_file({"deepseek": {"api_key": "sk-real", "base_url": ""}})
        with ctx1, ctx2:
            result = _load_provider_cfg_from_file("deepseek")
        self.assertIn("api_key", result)
        self.assertNotIn("base_url", result)    # 空字符串被 {k:v for k,v if v} 过滤

    def test_multiple_providers_independent(self):
        """文件含多个 provider，只返回被查询的那个。"""
        ctx1, ctx2 = self._with_file({
            "deepseek":    {"api_key": "sk-deepseek"},
            "siliconflow": {"api_key": "sk-sf"},
            "openai":      {"api_key": "sk-openai"},
        })
        with ctx1, ctx2:
            r_ds = _load_provider_cfg_from_file("deepseek")
            r_sf = _load_provider_cfg_from_file("siliconflow")
        self.assertEqual(r_ds["api_key"], "sk-deepseek")
        self.assertEqual(r_sf["api_key"], "sk-sf")


# ── TestBuildCfg ─────────────────────────────────────────────────────────────

class TestBuildCfg(unittest.TestCase):
    """验证 _build_cfg 正确合并 env 变量与 providers.json。"""

    def test_env_key_wins_over_file_key(self):
        """环境变量优先级 > providers.json。"""
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-from-env"}), \
             patch.object(_reg, "_load_provider_cfg_from_file",
                          return_value={"api_key": "sk-from-file"}):
            cfg = _build_cfg("deepseek")
        self.assertEqual(cfg.api_key, "sk-from-env")

    def test_file_key_used_when_env_absent(self):
        """env 变量为空时，使用 providers.json 里的 key。"""
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}), \
             patch.object(_reg, "_load_provider_cfg_from_file",
                          return_value={"api_key": "sk-from-file"}):
            cfg = _build_cfg("deepseek")
        self.assertEqual(cfg.api_key, "sk-from-file")

    def test_base_url_from_file(self):
        """base_url 从 providers.json 补充（env 侧没有 BASE_URL 变量）。"""
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}), \
             patch.object(_reg, "_load_provider_cfg_from_file",
                          return_value={"base_url": "https://proxy.example.com"}):
            cfg = _build_cfg("deepseek")
        self.assertEqual(cfg.base_url, "https://proxy.example.com")

    def test_model_param_applied(self):
        """model 参数覆盖 provider 默认值。"""
        with patch.object(_reg, "_load_provider_cfg_from_file", return_value={}):
            cfg = _build_cfg("deepseek", model="deepseek-reasoner")
        self.assertEqual(cfg.model, "deepseek-reasoner")

    def test_china_providers_only_from_file(self):
        """siliconflow/moonshot/zhipu 没有对应 env 变量，只能从 file 获取 key。"""
        for name in ("siliconflow", "moonshot", "zhipu"):
            with patch.object(_reg, "_load_provider_cfg_from_file",
                              return_value={"api_key": f"sk-{name}"}):
                cfg = _build_cfg(name)
            self.assertEqual(cfg.api_key, f"sk-{name}", f"{name} key 未从 file 读取")


# ── TestListAvailableProviders ────────────────────────────────────────────────

class TestListAvailableProviders(unittest.TestCase):
    """验证 list_available_providers 正确反映 key 存在性。"""

    def _list_with_keys(self, key_map: dict) -> list:
        """
        清空所有 env key，用 key_map {provider_name: api_key} 模拟 file 配置，
        返回 list_available_providers() 结果。
        """
        def _fake_load(name):
            return {"api_key": key_map[name]} if name in key_map else {}

        env_cleared = {k: "" for k in _ALL_KEY_ENV_VARS}
        with patch.dict(os.environ, env_cleared), \
             patch.object(_reg, "_load_provider_cfg_from_file", side_effect=_fake_load):
            return list_available_providers()

    def test_openai_available_with_file_key(self):
        providers = self._list_with_keys({"openai": "sk-test"})
        openai = next(p for p in providers if p["name"] == "openai")
        self.assertTrue(openai["available"])

    def test_siliconflow_available_with_file_key(self):
        providers = self._list_with_keys({"siliconflow": "sk-sf-test"})
        sf = next(p for p in providers if p["name"] == "siliconflow")
        self.assertTrue(sf["available"])

    def test_no_key_not_available(self):
        providers = self._list_with_keys({})
        deepseek = next(p for p in providers if p["name"] == "deepseek")
        self.assertFalse(deepseek["available"])

    def test_local_providers_always_available(self):
        """ollama / lmstudio 是本地 provider，不需要 key，始终可用。"""
        providers = self._list_with_keys({})  # no keys at all
        for local_name in ("ollama", "lmstudio"):
            entry = next((p for p in providers if p["name"] == local_name), None)
            if entry:   # 如果 provider 存在
                self.assertTrue(entry["available"],
                                f"{local_name} 应始终可用")

    def test_contains_all_registered_providers(self):
        """返回列表应包含 _PROVIDER_CLASSES 里的全部 provider。"""
        providers = self._list_with_keys({})
        names = {p["name"] for p in providers}
        for name in _PROVIDER_CLASSES:
            self.assertIn(name, names)

    def test_entries_have_required_fields(self):
        """每个条目有 name / available / local / tools / thinking 字段。"""
        providers = self._list_with_keys({})
        for entry in providers:
            for field in ("name", "available", "local", "tools", "thinking"):
                self.assertIn(field, entry, f"缺少字段 {field}")


# ── TestNewProvidersRegistered ────────────────────────────────────────────────

class TestNewProvidersRegistered(unittest.TestCase):
    """验证新增的中国可访问 provider 被正确注册。"""

    def test_siliconflow_in_provider_classes(self):
        self.assertIn("siliconflow", _PROVIDER_CLASSES)

    def test_moonshot_in_provider_classes(self):
        self.assertIn("moonshot", _PROVIDER_CLASSES)

    def test_zhipu_in_provider_classes(self):
        self.assertIn("zhipu", _PROVIDER_CLASSES)

    def test_siliconflow_base_url(self):
        from providers.llm.openai_compat import SiliconFlowProvider
        self.assertIn("siliconflow.cn", SiliconFlowProvider.DEFAULT_BASE_URL)

    def test_moonshot_base_url(self):
        from providers.llm.openai_compat import MoonshotProvider
        self.assertIn("moonshot.cn", MoonshotProvider.DEFAULT_BASE_URL)

    def test_zhipu_base_url(self):
        from providers.llm.openai_compat import ZhiPuProvider
        self.assertIn("bigmodel.cn", ZhiPuProvider.DEFAULT_BASE_URL)

    def test_siliconflow_before_openai_in_fallback(self):
        """SiliconFlow 应在 OpenAI 之前，确保国内环境优先走国内服务。"""
        names = [name for name, _, _ in _DEFAULT_FALLBACK_CHAIN]
        sf_idx = names.index("siliconflow")
        oa_idx = names.index("openai")
        self.assertLess(sf_idx, oa_idx,
                        "siliconflow 应排在 openai 前面")

    def test_deepseek_first_among_the_providers_that_need_a_key(self):
        """DeepSeek 应是第一个需要 API key 的云端 provider。

        这条断言原本写作「第一个云端 provider」，当时每个云端 provider 都要
        用户自己提供 key，两种说法等价。Arthera 网关打破了这个前提：它是云端
        的，但凭的是用户已经登录的会话，不需要 key。所以这里要问的是「需要
        key 的那些里谁第一」——元组第二位就是 key 名，区分本来就在数据里。
        """
        from aria_code.providers.llm.registry import _PROVIDER_CLASSES
        keyed_cloud = [
            name for name, env_key, _ in _DEFAULT_FALLBACK_CHAIN
            if env_key and not _PROVIDER_CLASSES[name].local
        ]
        self.assertEqual(keyed_cloud[0], "deepseek")

    def test_a_signed_in_user_is_not_asked_for_a_key_first(self):
        """不需要 key 的云端 provider 要排在需要 key 的之前。

        否则一个已经登录、已经能用 Google 模型的用户，会先被要求去申请一个
        他并不需要的 key。
        """
        from aria_code.providers.llm.registry import _PROVIDER_CLASSES
        names = [name for name, _, _ in _DEFAULT_FALLBACK_CHAIN]
        keyless_cloud = [
            name for name, env_key, _ in _DEFAULT_FALLBACK_CHAIN
            if not env_key and not _PROVIDER_CLASSES[name].local
        ]
        keyed_cloud = [
            name for name, env_key, _ in _DEFAULT_FALLBACK_CHAIN
            if env_key and not _PROVIDER_CLASSES[name].local
        ]
        for keyless in keyless_cloud:
            for keyed in keyed_cloud:
                self.assertLess(names.index(keyless), names.index(keyed),
                                f"{keyless} 不需要 key，应排在 {keyed} 之前")


if __name__ == "__main__":
    unittest.main()
