"""Reject excessive bodies and unwanted multipart uploads before form parsing."""
from starlette.responses import JSONResponse
from starlette.types import Message


class BodyTooLarge(Exception):
    pass


class RequestLimitsMiddleware:
    def __init__(self, app, *, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        path = scope.get('path', '')
        multipart = headers.get(b'content-type', b'').lower().startswith(b'multipart/form-data')
        upload = path == '/docker/stack-create/review' or path.startswith('/docker/stack-models/') and path.endswith('/import')
        if multipart and not upload:
            return await JSONResponse({'detail': 'Multipart uploads are not accepted here'}, status_code=415)(scope, receive, send)
        limit = self.settings.request_max_bytes
        if scope.get('method') in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            limit = min(limit, self.settings.post_max_bytes)
        if path == '/login':
            limit = min(limit, self.settings.login_max_bytes)
        if upload:
            limit = min(self.settings.request_max_bytes, self.settings.upload_max_bytes + 65536)
        try:
            length = int(headers.get(b'content-length', b'0'))
            if length < 0 or length > limit:
                raise BodyTooLarge
        except (ValueError, BodyTooLarge):
            return await JSONResponse({'detail': 'Request body exceeds the configured limit'}, status_code=413)(scope, receive, send)
        # Buffer a bounded body before calling downstream parsers, including chunked requests.
        body, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            size += len(message.get('body', b''))
            if size > limit:
                return await JSONResponse({'detail': 'Request body exceeds the configured limit'}, status_code=413)(scope, receive, send)
            body.append(message.get('body', b''))
            if not message.get('more_body', False):
                break
        raw, delivered = b''.join(body), False
        async def bounded_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': raw, 'more_body': False}
            return await receive()
        await self.app(scope, bounded_receive, send)
