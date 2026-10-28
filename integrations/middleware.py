from integrations import config


class RuntimeConfigMiddleware:
    """Apply console-managed settings (cheap: a cache lookup unless something changed)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        config.refresh()
        return self.get_response(request)
