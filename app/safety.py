"""Mask sensitive text before persistence or model use; never log user input."""

import re

PATTERNS = (
    re.compile(r"(?<!\d)\d{6}[\s-]*\d{7}(?!\d)"),
    re.compile(
        r"(?<!\d)(?:\+82[\s-]?)?0?(?:1[016789]|2|[3-6]\d)[\s.-]?\d{3,4}[\s.-]?\d{4}(?!\d)"
    ),
    re.compile(r"(?:성명|이름|신청인|수신인)\s*[:：=]?\s*[가-힣]{2,4}(?:님|씨)?"),
    re.compile(r"[가-힣]{2,4}(?=\s*(?:님|씨)(?:\s|[,.:]|$))"),
    re.compile(r"(?:주소|거주지)\s*[:：=]\s*[^\n,;]+"),
    re.compile(
        r"[가-힣\d]+(?:대로|로|길)\s*\d+(?:번길\s*\d+)?(?:-\d+)?(?:\s*\d+(?:동|호))*"
    ),
    re.compile(r"[가-힣]+(?:읍|면|동|리)\s*\d+(?:-\d+)?(?:번지)?(?:\s*\d+(?:동|호))*"),
)
USER_TAG = re.compile(r"<\s*/?\s*user_input\b[^>]*>", re.IGNORECASE)


def mask_pii(text: str | None) -> str:
    value = text or ""
    for pattern in PATTERNS:
        value = pattern.sub("[개인정보 마스킹]", value)
    return value


def sanitize_user_input(text: str | None) -> str:
    from html import unescape

    value = text or ""
    for _ in range(2):
        value = USER_TAG.sub("", unescape(value))
    return mask_pii(value)


def mask_data(value):
    if isinstance(value, str):
        return mask_pii(value)
    if isinstance(value, dict):
        return {key: mask_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_data(item) for item in value]
    return value
