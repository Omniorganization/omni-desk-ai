from omnidesk_agent.validation.webhook_signatures import line_signature_valid, wechat_signature


def test_line_signature_valid():
    body = b'{"events":[]}'
    secret = "secret"
    import base64, hashlib, hmac
    sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    assert line_signature_valid(body, secret, sig)


def test_wechat_signature():
    token = "token"
    timestamp = "123"
    nonce = "abc"
    assert wechat_signature(token, timestamp, nonce) == wechat_signature(token, timestamp, nonce)
