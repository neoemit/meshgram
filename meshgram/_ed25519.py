"""Minimal pure-Python Ed25519 signing for MeshCore "expanded" private keys.

MeshCore stores identities in the orlp/ed25519 format: the 64-byte private key
is ``clamped_scalar (32 bytes) || prefix (32 bytes)`` (i.e. the already-hashed
RFC 8032 seed), not the 32-byte seed most libraries expect. Signing therefore
has to start from the scalar directly. This is only used for the optional
MeshMapper auth-token fallback (once per token lifetime), so speed and
constant-time behaviour are not a concern here.
"""
from __future__ import annotations

import hashlib

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

_Point = tuple[int, int, int, int]  # extended homogeneous coordinates (X, Y, Z, T)


def _recover_x(y: int, sign: int) -> int:
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = (x * _SQRT_M1) % _P
    if x & 1 != sign:
        x = _P - x
    return x


_BY = (4 * pow(5, _P - 2, _P)) % _P
_BX = _recover_x(_BY, 0)
_BASE: _Point = (_BX, _BY, 1, (_BX * _BY) % _P)
_IDENTITY: _Point = (0, 1, 1, 0)


def _point_add(p: _Point, q: _Point) -> _Point:
    a = ((p[1] - p[0]) * (q[1] - q[0])) % _P
    b = ((p[1] + p[0]) * (q[1] + q[0])) % _P
    c = (2 * p[3] * q[3] * _D) % _P
    d = (2 * p[2] * q[2]) % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return ((e * f) % _P, (g * h) % _P, (f * g) % _P, (e * h) % _P)


def _scalar_mult(scalar: int, point: _Point) -> _Point:
    result = _IDENTITY
    while scalar > 0:
        if scalar & 1:
            result = _point_add(result, point)
        point = _point_add(point, point)
        scalar >>= 1
    return result


def _encode_point(point: _Point) -> bytes:
    z_inv = pow(point[2], _P - 2, _P)
    x = (point[0] * z_inv) % _P
    y = (point[1] * z_inv) % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _validate_private_key(private_key: bytes) -> None:
    if len(private_key) != 64:
        raise ValueError(f"MeshCore private key must be 64 bytes, got {len(private_key)}")


def public_key_from_expanded(private_key: bytes) -> bytes:
    """Derive the 32-byte public key for a 64-byte MeshCore private key."""
    _validate_private_key(private_key)
    scalar = int.from_bytes(private_key[:32], "little")
    return _encode_point(_scalar_mult(scalar, _BASE))


def sign_with_expanded_key(message: bytes, private_key: bytes, public_key: bytes) -> bytes:
    """Return a 64-byte Ed25519 signature, matching MeshCore's ``ed25519_sign``."""
    _validate_private_key(private_key)
    if len(public_key) != 32:
        raise ValueError(f"MeshCore public key must be 32 bytes, got {len(public_key)}")

    scalar = int.from_bytes(private_key[:32], "little")
    prefix = private_key[32:]

    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _L
    r_encoded = _encode_point(_scalar_mult(r, _BASE))
    k = int.from_bytes(hashlib.sha512(r_encoded + public_key + message).digest(), "little") % _L
    s = (r + k * scalar) % _L
    return r_encoded + s.to_bytes(32, "little")
