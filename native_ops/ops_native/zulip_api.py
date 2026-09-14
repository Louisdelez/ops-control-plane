"""Official Zulip REST API over verified TLS; no redirects or ambient proxy."""
import base64
import json
import ssl
import urllib.parse
import urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('redirect refused')

class Zulip:
    def __init__(self, origin, ca, email, key):
        parsed = urllib.parse.urlsplit(origin)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment or parsed.path not in ('','/'):
            raise ValueError('HTTPS origin required')
        self.origin = origin.rstrip('/')
        self.auth = 'Basic '+base64.b64encode((email+':'+key).encode()).decode()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca)))

    def call(self, method, path, fields=None):
        if not path.startswith('/api/v1/') or '..' in path:
            raise ValueError('invalid API path')
        fields = fields or {}
        encoded = urllib.parse.urlencode({k:json.dumps(v) if isinstance(v,(list,dict,bool)) else v for k,v in fields.items()})
        url = self.origin+path
        data = None
        if method == 'GET' and encoded:
            url += '?'+encoded
        elif method != 'GET':
            data = encoded.encode()
        req = urllib.request.Request(url, data=data, method=method,
            headers={'Authorization':self.auth,'Content-Type':'application/x-www-form-urlencoded'})
        with self.opener.open(req, timeout=15) as response:
            body = response.read(2_000_001)
        if len(body)>2_000_000:
            raise RuntimeError('Zulip response too large')
        payload=json.loads(body)
        if payload.get('result') != 'success':
            raise RuntimeError('Zulip API operation failed')
        return payload

    def messages(self, stream_id, anchor, before=0, after=100):
        return self.call('GET','/api/v1/messages',{
            'anchor':anchor,'num_before':before,'num_after':after,
            'apply_markdown':False,'narrow':[{'operator':'channel','operand':stream_id}]})['messages']

    def send(self, stream_id, topic, content):
        return self.call('POST','/api/v1/messages',{'type':'stream','to':[stream_id],'topic':topic,'content':content})['id']

    def trusted_message(self, message, stream_id, human_ids):
        if message.get('type') != 'stream' or message.get('stream_id') != stream_id or type(message.get('sender_id')) is not int or message['sender_id'] not in human_ids:
            return False
        user=self.call('GET',f"/api/v1/users/{message['sender_id']}")['user']
        return user.get('user_id') == message['sender_id'] and user.get('is_active') is True and user.get('is_bot') is False
