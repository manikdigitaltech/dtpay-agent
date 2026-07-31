"""
Authentication and authorization for the on-demand summary API.

authenticate(uid, token) confirms a users_jwt_tokens row exists
matching BOTH the uid (from the request body) and the token (from the
Authorization header), not expired - requiring both together, rather
than looking the token up alone and comparing the uid it resolves to
afterward, is what makes it impossible to reuse someone else's valid
token by simply claiming a different uid in the body.

resolve_cp_product_ids(uid, role_name, cp_product_id) decides which
cp_product_ids a request is allowed to touch: everything (or just one,
if given) for ADMIN, since an admin can look at any product; only this
uid's own products (verified via dtpay_cp_products.cp_id) for anyone
else, and a cp_product_id that isn't theirs is rejected outright
rather than silently ignored or substituted.
"""
from extract import get_connection

ADMIN_ROLE = "ADMIN"


class AuthError(Exception):
    """Raised for any authentication/authorization failure - the API
    layer maps this straight to the matching HTTP status."""
    def __init__(self, status_code, message):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def authenticate(uid, token):
    """Raises AuthError(401, ...) unless a matching, unexpired
    users_jwt_tokens row exists for this exact (uid, token) pair.
    expiry_date > NOW() is evaluated in MySQL rather than compared
    against a Python datetime, to sidestep any naive/aware or
    client/server clock-skew mismatch entirely."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM users_jwt_tokens "
                "WHERE uid = %(uid)s AND jwt_token = %(token)s AND expiry_date > NOW()",
                {"uid": uid, "token": token},
            )
            row = cur.fetchone()
    if row is None:
        raise AuthError(401, "Invalid or expired credentials")


def get_role(uid):
    """Raises AuthError(401, ...) if uid doesn't resolve to a real
    dtpay_users row (shouldn't happen if authenticate() just passed,
    but not assumed)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT role_name FROM dtpay_users WHERE id = %(uid)s", {"uid": uid})
            row = cur.fetchone()
    if row is None:
        raise AuthError(401, "Unknown user")
    return row["role_name"]


def resolve_cp_product_ids(uid, role_name, cp_product_id=None):
    """
    Returns the tuple of cp_product_ids this request may summarize.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            if role_name == ADMIN_ROLE:
                if cp_product_id is not None:
                    return (cp_product_id,)
                cur.execute("SELECT id FROM dtpay_cp_products WHERE UPPER(status) = 'APPROVED'")
                return tuple(row["id"] for row in cur.fetchall())

            if cp_product_id is not None:
                cur.execute(
                    "SELECT id FROM dtpay_cp_products "
                    "WHERE id = %(id)s AND cp_id = %(uid)s AND UPPER(status) = 'APPROVED'",
                    {"id": cp_product_id, "uid": uid},
                )
                row = cur.fetchone()
                if row is None:
                    raise AuthError(403, "That product does not belong to this user")
                return (cp_product_id,)

            cur.execute(
                "SELECT id FROM dtpay_cp_products WHERE cp_id = %(uid)s AND UPPER(status) = 'APPROVED'",
                {"uid": uid},
            )
            return tuple(row["id"] for row in cur.fetchall())