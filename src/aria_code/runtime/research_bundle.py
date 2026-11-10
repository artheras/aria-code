"""Static research evidence inventory. Integrity and scope are not scientific truth."""
from __future__ import annotations

import argparse
import hashlib
import json

from .task_verification import _read_file, _json_object, _json_float, _json_constant

KINDS = {'design', 'public_data', 'synthetic_data', 'analysis', 'code', 'simulation'}
SCOPES = {
    'proposal': KINDS,
    'analytical': {'analysis'},
    'software': {'code'},
    'simulation': {'simulation'},
    'real_world': set(),
}


def _text(value, limit=4096):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('Missing or oversized research field')
    return value


def audit_bundle(workspace: str, manifest_path: str) -> dict:
    """Read bounded workspace artifacts without executing code or opening URLs.

    All provenance kinds are declarations, not authenticated source attestations.
    This no-hardware inventory never establishes a real-world result.
    """
    manifest = json.loads(_read_file(workspace, manifest_path), object_pairs_hook=_json_object,
                          parse_float=_json_float, parse_constant=_json_constant)
    if not isinstance(manifest, dict) or type(manifest.get('version')) is not int or manifest['version'] != 1:
        raise ValueError('Unsupported research bundle version')
    _text(manifest.get('question'))
    _text(manifest.get('hypothesis'))
    limitations = manifest.get('limitations')
    if not isinstance(limitations, list) or not 1 <= len(limitations) <= 32:
        raise ValueError('Explicit research limitations are required')
    for item in limitations:
        _text(item)
    artifacts = manifest.get('artifacts')
    claims = manifest.get('claims')
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 16:
        raise ValueError('Expected 1..16 artifacts')
    if not isinstance(claims, list) or not 1 <= len(claims) <= 32:
        raise ValueError('Expected 1..32 claims')
    inventory = {}
    reports = []
    for item in artifacts:
        if not isinstance(item, dict):
            raise ValueError('Invalid artifact declaration')
        ident = _text(item.get('id'), 128)
        if ident in inventory or not isinstance(item.get('kind'), str) or item['kind'] not in KINDS:
            raise ValueError('Duplicate artifact ID or unsupported evidence kind')
        digest = item.get('sha256')
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Artifact requires a SHA-256 digest')
        data = _read_file(workspace, item.get('path'))
        matched = hashlib.sha256(data).hexdigest() == digest
        inventory[ident] = item['kind']
        reports.append({'id': ident, 'kind': item['kind'], 'digest_matches': matched})
    claim_ids = set()
    dispositions = []
    for claim in claims:
        if not isinstance(claim, dict):
            raise ValueError('Invalid claim declaration')
        ident = _text(claim.get('id'), 128)
        _text(claim.get('text'))
        scope = claim.get('scope')
        refs = claim.get('evidence')
        if ident in claim_ids or not isinstance(scope, str) or scope not in SCOPES:
            raise ValueError('Duplicate claim ID or unsupported claim scope')
        claim_ids.add(ident)
        if (not isinstance(refs, list) or not 1 <= len(refs) <= 16
                or any(not isinstance(ref, str) or ref not in inventory for ref in refs)):
            raise ValueError('Claim requires known evidence IDs')
        compatible = any(inventory[ref] in SCOPES[scope] for ref in refs)
        dispositions.append({'id': ident, 'scope': scope,
                             'scope_supported': compatible, 'scientifically_verified': None})
    integrity = all(report['digest_matches'] for report in reports)
    return {'version': 1, 'success': integrity and all(c['scope_supported'] for c in dispositions),
            'integrity_verified': integrity, 'scientifically_verified': None,
            'real_world_validation': 'not_established', 'semantic_review_required': True,
            'coverage': 'artifact_hashes_and_declared_evidence_scope_only',
            'artifacts': reports, 'claims': dispositions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace')
    parser.add_argument('manifest')
    args = parser.parse_args()
    try:
        report = audit_bundle(args.workspace, args.manifest)
    except (OSError, ValueError, TypeError, RecursionError):
        report = {'success': False, 'error': 'Missing, unsafe or invalid research bundle',
                  'scientifically_verified': None, 'real_world_validation': 'not_established'}
    print(json.dumps(report, indent=2))
    return 0 if report['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
