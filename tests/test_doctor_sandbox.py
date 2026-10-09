from aria_code.doctor import sandbox_check
from aria_code.safety import sandbox


def test_doctor_reports_policy_only_instead_of_claiming_os_confinement(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: False)
    check = sandbox_check({"permission_mode": "workspace-write"})
    assert check.status == "warn" and "no OS filesystem/network confinement" in check.detail


def test_doctor_respects_full_access_and_explicit_disable(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: True)
    assert sandbox_check({"permission_mode": "full-access"}).status == "skip"
    assert sandbox_check({"os_sandbox": "off"}).status == "skip"


def test_doctor_names_the_available_sandbox(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    check = sandbox_check({})
    assert check.status == "ok" and "bubblewrap" in check.detail
