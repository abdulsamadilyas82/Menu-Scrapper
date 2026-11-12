class PartnerError(Exception):
    """A delivery-partner lookup failed (blocked, malformed response, network)."""


class LoginWallError(Exception):
    """Google asked for a login, so the current session is unusable."""
