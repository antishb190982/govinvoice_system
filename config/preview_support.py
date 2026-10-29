"""Preview embedding exceptions, without weakening production headers."""

from core.middleware import CONTENT_SECURITY_POLICY, SecurityHeadersMiddleware

PREVIEW_ANCESTORS = (
    "frame-ancestors 'self' https://replit.com https://*.replit.com https://*.replit.dev"
)


class PreviewHeadersMiddleware(SecurityHeadersMiddleware):
    def __call__(self, request):
        response = super().__call__(request)
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY.replace(
            "frame-ancestors 'none'", PREVIEW_ANCESTORS
        )
        response.headers.pop("X-Frame-Options", None)
        return response


def preview_context(request):
    return {"govbills_preview": True}
