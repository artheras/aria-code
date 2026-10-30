import io,json,unittest
from nebius_smoke import smoke
class Response(io.BytesIO):
 status=200
 def __enter__(self):return self
 def __exit__(self,*a):self.close()
class TestSmoke(unittest.TestCase):
 def test_request_and_result(self):
  def open_request(req,timeout):
   self.assertEqual('https://api.tokenfactory.nebius.com/v1/chat/completions',req.full_url)
   self.assertEqual('Bearer fake-key',req.get_header('Authorization'))
   self.assertEqual('nvidia/test',json.loads(req.data)['model'])
   return Response(b'{"model":"nvidia/test","choices":[{"message":{"content":"ARIA_OK"}}]}')
  result=smoke('fake-key','nvidia/test',open_request);self.assertTrue(result['exact_match']);self.assertNotIn('fake-key',str(result))
 def test_key_and_model_required(self):
  with self.assertRaises(ValueError):smoke('','nvidia/test')
  with self.assertRaises(ValueError):smoke('key','other/test')
if __name__=='__main__':unittest.main()
