"""仪表盘和名单共用的号码校验，不把布尔、全角或特殊数字当作 账号 ID。"""
import re


def positive_id(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,20}", text) or not int(text):
        return None
    return str(int(text))


def entity_id(value) -> str:
    """平台无关的稳定标识；不用于拼接文件路径，允许非数字 ID。"""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if value and len(value) <= 200 and not any(ord(c) < 32 for c in value) else ""
