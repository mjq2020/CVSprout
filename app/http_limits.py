"""Enforce request budgets before and during parsing, including chunked bodies."""
from fastapi import HTTPException
from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse


class BodyLimitExceeded(MultiPartException):
    # Multipart parsers close already-spooled files when this exception is raised.
    pass


class RequestBodyLimit:
    def __init__(self, app, policy):
        self.app = app
        self.policy = policy

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        try:
            limit = await self.policy(scope)
        except HTTPException as exc:
            return await JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                      headers=exc.headers)(scope, receive, send)
        lengths = [value for name, value in scope.get("headers", []) if name == b"content-length"]
        if lengths and (len(lengths) != 1 or not lengths[0].isdigit() or len(lengths[0]) > 20):
            return await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
        error = JSONResponse({"detail": "请求体过大，请减小文件或分批上传。"}, status_code=413,
                             headers={"Cache-Control": "no-store"})
        if lengths and int(lengths[0]) > limit:
            return await error(scope, receive, send)
        received = 0
        exceeded = False
        answered = False

        async def limited_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise BodyLimitExceeded("Request body too large")
            return message

        async def limited_send(message):
            nonlocal answered
            if exceeded:
                # Request.form() converts MultiPartException into a 400. Keep the
                # parser's file cleanup, but return the accurate 413 to the client.
                if not answered:
                    answered = True
                    await error(scope, receive, send)
            else:
                await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except BodyLimitExceeded:
            if not answered:
                await error(scope, receive, send)
