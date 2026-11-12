"""The agent must be able to check its own work under the default policy.

`safe` blocked pytest, py_compile, npm test and tsc --noEmit, so the loop could
write a fix and then had no way to learn whether it worked. Seen verbatim from
a real Gemini run: "由于安全策略的限制，我无法执行 pytest ... 但我相信测试应该
能够通过". An agent that cannot verify is guessing, and the acceptance gate is
built entirely on running these commands.
"""

import unittest

from aria_code.safety.permissions import evaluate_command_policy


def _allowed(command, policy="safe", **kwargs):
    return evaluate_command_policy(command, policy, **kwargs).allowed


class VerificationUnderSafePolicyTests(unittest.TestCase):
    def test_the_common_verification_runners_are_allowed(self):
        for command in (
            "python3 -m pytest -q",
            "pytest -q",
            "python3 -m py_compile calc.py",
            "npm test",
            "npm run build",
            "npx tsc --noEmit",
            "go test ./...",
            "cargo test",
            "ruff check src",
            "mypy src",
        ):
            with self.subTest(command=command):
                self.assertTrue(_allowed(command), f"{command} should run under safe")

    def test_it_does_not_open_the_door_wider(self):
        # The exemption is exactly what is_verification_command matches.
        for command in (
            "pip3 install requests",
            "python3 evil.py",
            "npm install left-pad",
            "curl https://example.com",
            "git push origin main",
        ):
            with self.subTest(command=command):
                self.assertFalse(_allowed(command), f"{command} must stay blocked under safe")

    def test_a_chained_destructive_command_is_still_rejected(self):
        # High risk is classified before the exemption is reached, so
        # smuggling rm through a verification prefix does not work.
        self.assertFalse(_allowed("pytest -q; rm -rf /tmp/x"))
        self.assertFalse(_allowed("npm test && sudo shutdown"))

    def test_read_only_mode_still_wins(self):
        # Verification writes .pyc files and can run arbitrary test code; a
        # session that asked for read-only meant it.
        self.assertFalse(_allowed("pytest -q", mode="read-only"))

    def test_the_network_rule_still_wins(self):
        self.assertFalse(_allowed("pip3 install pytest", network_enabled=False))

    def test_looser_policies_are_unchanged(self):
        for policy in ("balanced", "full"):
            with self.subTest(policy=policy):
                self.assertTrue(_allowed("pytest -q", policy))

    def test_an_allowed_verification_needs_no_approval_prompt(self):
        # An approval prompt between "the model finished" and "did it work" is
        # the one place a prompt cannot help — nobody is watching a headless run.
        decision = evaluate_command_policy("python3 -m pytest -q", "safe")
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.requires_approval)
        self.assertEqual(decision.reason, "")


if __name__ == "__main__":
    unittest.main()
