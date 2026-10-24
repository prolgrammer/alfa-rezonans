from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# Параметры argon2-cffi по умолчанию — argon2id с профилем RFC 9106 (низкая память).
_hasher = PasswordHasher()
# Хеш для несуществующего логина: проверка идёт так же долго, и по времени ответа логин не угадать.
_DUMMY_HASH = _hasher.hash("orientir-dummy-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        _hasher.verify(password_hash or _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False
    return password_hash is not None


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
