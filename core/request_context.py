"""Per-request actor and client IP, so audit records can be written anywhere."""

from contextvars import ContextVar

_user = ContextVar("govbills_user", default=None)
_ip = ContextVar("govbills_ip", default=None)


def set_context(user, ip):
    return _user.set(user), _ip.set(ip)


def reset_context(tokens):
    _user.reset(tokens[0])
    _ip.reset(tokens[1])


def current_user():
    return _user.get()


def current_ip():
    return _ip.get()


def client_ip(request):
    # Only REMOTE_ADDR is trusted. Behind a reverse proxy, the proxy must be
    # configured to set REMOTE_ADDR (e.g. gunicorn --forwarded-allow-ips).
    return request.META.get("REMOTE_ADDR")
