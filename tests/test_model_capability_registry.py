"""Regression tests for cloud-model capability lookup.

Background: the CLI stores the provider-qualified model id ("google/gemini-2.5-pro")
in its config, but `resolve_model_key` only matched MODELS *keys* ("gemini-pro")
and `_CAPABILITY_TABLE` held no Gemini/OpenAI entries at all.  Gemini therefore
resolved to the unknown-model fallback: no tool calls and a 4096-token context.

That downgrade was silent and had three visible consequences:
  - repository questions were answered from memory, because the model was never
    given file tools and so never read the repo;
  - the deterministic market chain took over (it only runs for tool-less models)
    and answered a code request with a stock quote;
  - the context gauge measured a 1M-token window against a 4096 denominator.

Hermetic — no network, no model calls.
"""

import unittest

from aria_code.model_capability import (
    can_handle_coding,
    get_model_capability,
    is_unknown_model,
    strip_vendor_prefix,
)


class VendorPrefixTests(unittest.TestCase):
    def test_strips_known_vendor_prefixes(self):
        self.assertEqual(strip_vendor_prefix("google/gemini-2.5-pro"), "gemini-2.5-pro")
        self.assertEqual(strip_vendor_prefix("openai/gpt-4o"), "gpt-4o")
        self.assertEqual(strip_vendor_prefix("anthropic/claude-opus-4"), "claude-opus-4")

    def test_leaves_unprefixed_names_alone(self):
        self.assertEqual(strip_vendor_prefix("qwen2.5:7b"), "qwen2.5:7b")

    def test_local_model_with_slash_is_not_mangled(self):
        # Ollama users can pull namespaced community models; an unknown
        # namespace must not be stripped down to a wrong capability entry.
        self.assertEqual(strip_vendor_prefix("hf.co/someone/qwen"), "hf.co/someone/qwen")


class CloudModelCapabilityTests(unittest.TestCase):
    def test_gemini_pro_has_tools_and_full_context(self):
        cap = get_model_capability("google/gemini-2.5-pro")
        self.assertTrue(cap.tool_calls)
        self.assertGreaterEqual(cap.context_window, 1_000_000)
        self.assertFalse(is_unknown_model(cap))

    def test_gemini_resolves_with_and_without_vendor_prefix(self):
        self.assertEqual(
            get_model_capability("google/gemini-2.5-pro"),
            get_model_capability("gemini-2.5-pro"),
        )

    def test_dated_preview_suffix_still_resolves(self):
        cap = get_model_capability("gemini-2.5-pro-preview-06-05")
        self.assertTrue(cap.tool_calls)
        self.assertFalse(is_unknown_model(cap))

    def test_openai_models_have_tools(self):
        for name in ("gpt-4o", "openai/gpt-4o", "o3-mini"):
            with self.subTest(model=name):
                cap = get_model_capability(name)
                self.assertTrue(cap.tool_calls)
                self.assertFalse(is_unknown_model(cap))

    def test_cloud_models_are_eligible_for_coding(self):
        # openai_native was a new format value; can_handle_coding whitelists
        # formats explicitly, so omitting it would have declared Gemini unable
        # to write code.
        for name in ("google/gemini-2.5-pro", "gpt-4o", "claude-opus-4"):
            with self.subTest(model=name):
                self.assertTrue(can_handle_coding(get_model_capability(name)))

    def test_tool_gate_admits_gemini(self):
        # Mirrors the check in aria_cli that decides between the LLM tool loop
        # and the deterministic fallback chain.
        cap = get_model_capability("google/gemini-2.5-pro")
        self.assertTrue(cap.tool_calls and cap.context_window >= 8192)


class ExistingEntriesUnchangedTests(unittest.TestCase):
    """The new gpt-* entries must not shadow the Ollama-hosted gpt-oss models."""

    def test_gpt_oss_still_ollama_native(self):
        for name in ("gpt-oss", "gpt-oss:120b"):
            with self.subTest(model=name):
                cap = get_model_capability(name)
                self.assertEqual(cap.format, "ollama_native")
                self.assertGreaterEqual(cap.context_window, 131072)

    def test_local_and_anthropic_models_unchanged(self):
        self.assertEqual(get_model_capability("qwen2.5:7b").format, "ollama_native")
        self.assertEqual(get_model_capability("claude-haiku-4-5").format, "anthropic_native")
        self.assertEqual(get_model_capability("aria-prelude").format, "router_only")


class UnknownModelTests(unittest.TestCase):
    def test_unknown_model_is_flagged_not_silently_degraded(self):
        cap = get_model_capability("totally-made-up-model:9b")
        self.assertFalse(cap.tool_calls)
        self.assertTrue(is_unknown_model(cap))

    def test_registered_model_is_not_flagged(self):
        self.assertFalse(is_unknown_model(get_model_capability("qwen2.5:7b")))


class ModelKeyResolutionTests(unittest.TestCase):
    """`resolve_model_key` must accept the id form stored in the config."""

    def test_registered_id_resolves_to_its_catalogue_entry(self):
        from aria_code.aria_cli import get_model_cfg, resolve_model_key

        self.assertEqual(resolve_model_key("google/gemini-3.8-flash"), "gemini-flash")
        cfg = get_model_cfg("google/gemini-3.8-flash")
        self.assertTrue(cfg.get("tools"))
        self.assertGreaterEqual(int(cfg.get("num_ctx", 0)), 1_000_000)

    def test_key_and_alias_forms_still_resolve(self):
        from aria_code.aria_cli import resolve_model_key

        self.assertEqual(resolve_model_key("gemini-pro"), "gemini-pro")
        # "gemini" means the stable default, not the short-term latest model.
        self.assertEqual(resolve_model_key("gemini"), "gemini-flash")
        self.assertEqual(resolve_model_key("gemini-lite"), "gemini-lite")

    def test_unregistered_model_still_reports_community(self):
        from aria_code.aria_cli import resolve_model_key

        self.assertEqual(resolve_model_key("totally-made-up-model:9b"), "_community_")


if __name__ == "__main__":
    unittest.main()
