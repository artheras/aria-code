#!/usr/bin/env python3
"""Verify a Nebius NVIDIA model without printing credentials or request bodies."""
import json, os, sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
BASE='https://api.tokenfactory.nebius.com/v1'
def smoke(key, model, opener=urlopen):
 if not key: raise ValueError('NEBIUS_API_KEY is missing')
 if not model or not model.lower().startswith('nvidia/'):
  raise ValueError('NEBIUS_MODEL must be an NVIDIA model ID from the account model list')
 payload=json.dumps({'model':model,'messages':[{'role':'user','content':'Reply with exactly: ARIA_OK'}], 'max_tokens':32}).encode()
 req=Request(BASE+'/chat/completions',data=payload,headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},method='POST')
 try:
  with opener(req,timeout=60) as response:
   if response.status != 200: raise RuntimeError('Inference request failed')
   body=json.loads(response.read(1024*1024))
 except (HTTPError,URLError,TimeoutError,OSError,ValueError) as exc:
  raise RuntimeError('Nebius inference failed; check account access, model ID and network') from exc
 if not isinstance(body,dict) or not isinstance(body.get('choices'),list) or not body['choices']:
  raise RuntimeError('Nebius returned no completion')
 answer=body['choices'][0].get('message',{}).get('content')
 if not isinstance(answer,str) or not answer.strip(): raise RuntimeError('Nebius returned no text')
 return {'requested_model':model,'response_model':body.get('model'),'text_received':True,'exact_match':answer.strip()=='ARIA_OK'}
def main():
 try: print(json.dumps(smoke(os.getenv('NEBIUS_API_KEY',''),os.getenv('NEBIUS_MODEL',''))));return 0
 except (ValueError,RuntimeError) as e: print(str(e),file=sys.stderr);return 1
if __name__=='__main__':sys.exit(main())
