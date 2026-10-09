"""Approval decisions for tools that require user consent."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ApprovalDecision:
    """Structured result from a tool approval prompt."""

    approved: bool
    policy: str | None = None
    user_approved: bool = False
    upgrade_policy: bool = False
    auto_approve_session: bool = False
    tool_scope: str = ""
    command_prefix: tuple[str, ...] = ()
    reason: str = ""
    # What the user said instead of approving. A denial with feedback does
    # not end the turn: the call is skipped and the model reads this.
    feedback: str = ""

    @classmethod
    def allow(
        cls,
        *,
        policy: str | None = None,
        user_approved: bool = False,
        upgrade_policy: bool = False,
        auto_approve_session: bool = False,
        tool_scope: str = "",
        command_prefix: tuple[str, ...] = (),
        reason: str = "",
    ) -> "ApprovalDecision":
        return cls(
            approved=True,
            policy=policy,
            user_approved=user_approved,
            upgrade_policy=upgrade_policy,
            auto_approve_session=auto_approve_session,
            tool_scope=tool_scope,
            command_prefix=command_prefix,
            reason=reason,
        )

    @classmethod
    def deny(cls, reason: str = "", *, feedback: str = "") -> "ApprovalDecision":
        return cls(approved=False, reason=reason, feedback=feedback.strip())

    def as_declined_result(self) -> dict:
        """What the model reads in place of a call the user declined with feedback."""
        return {
            "success": False,
            "error": (
                "The user declined this call and said: "
                f"\"{self.feedback}\". Do what they asked instead; do not retry the same call."
            ),
            "declined_by_user": True,
        }


def apply_approval_decision(params: dict, decision: ApprovalDecision) -> dict:
    """Apply execution-facing approval fields to tool params."""
    if decision.policy is not None:
        params["policy"] = decision.policy
    if decision.user_approved:
        params["user_approved"] = True
    if decision.upgrade_policy:
        params["_upgrade_policy"] = True
    return params
