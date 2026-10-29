"""Rate limits for public account endpoints."""
from rest_framework.throttling import AnonRateThrottle


class LoginRateThrottle(AnonRateThrottle):
    """Limit unauthenticated password login attempts before authentication work."""

    scope = "login"
