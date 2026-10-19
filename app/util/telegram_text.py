"""Keep complete plain-text previews within Telegram's UTF-16 message limit."""


def message_parts(text: str, limit: int = 4000) -> list[str]:
    parts, current, size = [], [], 0
    for char in text:
        units = 2 if ord(char) > 0xFFFF else 1
        if size + units > limit:
            parts.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += units
    if current:
        parts.append("".join(current))
    return parts
