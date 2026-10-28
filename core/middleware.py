from core.request_context import client_ip, reset_context, set_context

CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if request.user.is_authenticated if hasattr(request, "user") else False:
            # Financial pages must not linger in shared-browser caches.
            response.headers["Cache-Control"] = "no-store"
        return response


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tokens = set_context(getattr(request, "user", None), client_ip(request))
        try:
            return self.get_response(request)
        finally:
            reset_context(tokens)
