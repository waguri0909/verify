"""패널 왼쪽 바 색상 입력 — HEX 또는 색 이름을 0xRRGGBB 로 바꾼다.

인증 패널(bot.py)과 티켓 패널(ticket.py)이 같이 쓰므로 한곳에 모았다.
"""
from __future__ import annotations

DEFAULT_VERIFY_COLOR = 0x57F287  # 인증 패널 기본(초록)
DEFAULT_TICKET_COLOR = 0x6366F1  # 티켓 패널 기본(인디고)

# 색상 입력은 HEX 6자리 또는 이렇게 이름으로 적을 수 있다.
COLOR_NAMES = {
    "green": 0x57F287, "그린": 0x57F287, "초록": 0x57F287,
    "blue": 0x3498DB, "블루": 0x3498DB, "파랑": 0x3498DB, "파란": 0x3498DB,
    "red": 0xED4245, "레드": 0xED4245, "빨강": 0xED4245, "빨간": 0xED4245,
    "purple": 0x9B59B6, "퍼플": 0x9B59B6, "보라": 0x9B59B6,
    "indigo": 0x6366F1, "인디고": 0x6366F1,
    "gold": 0xF1C40F, "골드": 0xF1C40F, "금색": 0xF1C40F,
    "pink": 0xEB459E, "핑크": 0xEB459E, "분홍": 0xEB459E,
    "orange": 0xE67E22, "오렌지": 0xE67E22, "주황": 0xE67E22,
    "cyan": 0x1ABC9C, "시안": 0x1ABC9C, "청록": 0x1ABC9C,
    "white": 0xFFFFFF, "화이트": 0xFFFFFF, "흰색": 0xFFFFFF,
    "black": 0x000000, "블랙": 0x000000, "검정": 0x000000,
    "gray": 0x95A5A6, "grey": 0x95A5A6, "회색": 0x95A5A6,
}

HEX_HINT = "`#57F287` 처럼 HEX 6자리, 또는 그린/블루/보라 같은 색 이름"


def parse_color(raw: str) -> int | None:
    """'#57F287' / '57F287' / '#abc' / '그린' → 0xRRGGBB. 못 읽으면 None."""
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("#"):
        s = s[1:]
    elif s[:2].lower() == "0x":
        s = s[2:]
    # 블랙(0x000000)은 값이 0이라 `or` 에 걸리면 안 된다 → is not None 로 검사
    named = COLOR_NAMES.get(s.lower())
    if named is not None:
        return named
    if len(s) == 3 and all(c in "0123456789abcdefABCDEF" for c in s):
        s = "".join(c * 2 for c in s)
    if len(s) == 6 and all(c in "0123456789abcdefABCDEF" for c in s):
        return int(s, 16)
    return None
