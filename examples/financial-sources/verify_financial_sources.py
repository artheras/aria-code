#!/usr/bin/env python3
"""Readiness by default; --live explicitly requests a real provider lookup."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from aria_code.tools.enterprise_finance_tools import tool_analyze_financial_statements


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--symbol',default='AAPL')
    args=parser.parse_args()
    if not args.live:
        # Configuration presence is not a claim that the service is reachable.
        print(json.dumps({'company':'Arthera','sec_identity_present':bool(os.getenv('EDGAR_USER_AGENT','').strip()),'live_verified':False,'next_step':'Configure your company/contact email, then run with --live --symbol AAPL. Identity values are not printed.'}))
        return 0
    start=time.monotonic()
    result=tool_analyze_financial_statements({'company_name':args.symbol})
    data=result.get('data',{})
    print(json.dumps({'success':result['success'],'symbol':args.symbol,'elapsed_seconds':round(time.monotonic()-start,2),
                      'source_attempts':result.get('source_attempts',[]),'error_code':result.get('error_code'),
                      'source':data.get('data_source'),'period':data.get('period'),'currency':data.get('currency'),
                      'accession':data.get('accession'),'source_url':data.get('source_url'),'cache_hit':data.get('cache_hit')},ensure_ascii=False))
    return 0 if result['success'] else 1


if __name__=='__main__':raise SystemExit(main())
