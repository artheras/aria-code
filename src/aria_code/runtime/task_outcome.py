"""Transport-neutral view of host task results; never a source of authority."""
from collections.abc import Mapping


def task_outcome(result: Mapping, *, fresh: bool = False) -> dict:
    """Separate execution from declared acceptance, without exposing raw evidence.

    Call only on host task_status/task_result records, never on model prose.
    This projection does not refresh artifacts, authenticate a caller or resume work.
    """
    execution = str(result.get('status') or 'unknown')
    report = result.get('verification')
    report = report if isinstance(report, Mapping) else {}
    checks = report.get('checks')
    checks = checks if isinstance(checks, list) else []
    state = 'unverified'
    eligible = fresh and bool(checks) and execution == 'done'
    if (eligible and report.get('status') == 'verified'
            and report.get('verified') is True and result.get('verified') is True
            and result.get('success') is not False):
        state = 'verified'
    elif (eligible and report.get('status') == 'failed'
          and report.get('verified') is False and result.get('verified') is False):
        state = 'failed'
    return {
        'version': 1,
        'requires_refresh': not fresh,
        'execution_state': execution,
        'verification_state': state,
        'verified': {'verified': True, 'failed': False}.get(state),
        'checks_total': len(checks),
        'acceptance_scope': 'declared_predicates',
    }
