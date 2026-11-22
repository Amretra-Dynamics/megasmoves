"""Actual RSA signature and claim validation against synthetic OpenID JWKS."""
import base64
import json
import time
import unittest
import httpx
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes
from megas.siwc_oss import AUTH, _verify_signed_identity, ChatGPTConnectionError

def encode(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")

class CryptoTests(unittest.TestCase):
    def setUp(self):
        self.key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
        numbers=self.key.public_key().public_numbers()
        self.jwk={'kid':'fixture-key','kty':'RSA','use':'sig',
                  'n':encode(numbers.n.to_bytes((numbers.n.bit_length()+7)//8,'big')),
                  'e':encode(numbers.e.to_bytes((numbers.e.bit_length()+7)//8,'big'))}
        def handler(request):
            if request.url.path=='/.well-known/openid-configuration':
                return httpx.Response(200,json={'issuer':AUTH,'jwks_uri':AUTH+'/keys'})
            if request.url.path=='/keys':
                return httpx.Response(200,json={'keys':[self.jwk]})
            return httpx.Response(404)
        self.http=httpx.Client(transport=httpx.MockTransport(handler))
        self.claims={'iss':AUTH,'aud':'oaiapp_testclient12345','sub':'sub-fixture',
                     'nonce':'expected-nonce','exp':int(time.time())+3600,
                     'iat':int(time.time())-5,'email':'owner@example.test'}

    def sign(self,claims):
        header=encode(json.dumps({'alg':'RS256','kid':'fixture-key'}).encode())
        payload=encode(json.dumps(claims).encode())
        raw=(header+'.'+payload).encode()
        sig=self.key.sign(raw,padding.PKCS1v15(),hashes.SHA256())
        return header+'.'+payload+'.'+encode(sig)

    def validate(self,claims):
        return _verify_signed_identity(self.sign(claims),'oaiapp_testclient12345',
                                       'expected-nonce',self.http)

    def test_valid_identity_and_signature(self):
        self.assertEqual(self.validate(self.claims)['subject'],'sub-fixture')

    def test_rejects_wrong_signed_claims(self):
        invalid=[('nonce','forged'),('aud','another-client'),
                 ('iss','https://untrusted.example'),('exp',time.time()-20)]
        for field,value in invalid:
            with self.subTest(field=field):
                with self.assertRaises(ChatGPTConnectionError):
                    self.validate({**self.claims,field:value})

    def test_rejects_signature_tampering(self):
        token=self.sign(self.claims)
        header,payload,signature=token.split('.')
        raw=bytearray(base64.urlsafe_b64decode(signature+'='*(-len(signature)%4)))
        raw[0]^=1
        altered=header+'.'+payload+'.'+encode(raw)
        with self.assertRaises(ChatGPTConnectionError):
            _verify_signed_identity(altered,'oaiapp_testclient12345',
                                    'expected-nonce',self.http)
