"""对称加解密（Fernet）：用于 GitHub token 与 LLM API Key 落库前的加密。

密钥来自 `FERNET_KEY`。除本模块外，任何地方都不得持久化明文密钥；
展示给用户的密钥一律先经 `mask()` 脱敏。
"""
from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    def __init__(self, key: str):
        if not key:
            raise ValueError(
                "FERNET_KEY 未配置。生成命令："
                'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
        self._f = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, plaintext: str) -> bytes:
        return self._f.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        try:
            return self._f.decrypt(token).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("token 解密失败：FERNET_KEY 是否与写入时一致？") from exc


def mask(value: str | None) -> str:
    """把敏感串脱敏成可展示的形态：保留首 4 位与末 4 位，中间以 * 代替。"""
    if not value:
        return "（未设置）"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"
