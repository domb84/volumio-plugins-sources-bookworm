from typing import Any, Dict, List, Tuple


def parse_button_config(cfg: Dict[str, Any]) -> List[Tuple[str, int, Tuple[str, int, int]]]:
    """Parse a button configuration dictionary into a normalized list.

    Expected input format: { 'button_name': (channel, value_or_range), ... }
    Returns: list of tuples (name, channel, ('value', val)) or ('range', low, high)
    """
    parsed = []
    for name, pair in (cfg or {}).items():
        try:
            ch_str, data = pair
        except Exception:
            continue
        try:
            ch = int(ch_str)
        except Exception:
            ch = int(str(ch_str))

        if isinstance(data, str) and '-' in data:
            parts = [int(x) for x in data.split('-')]
            low, high = sorted(parts)
            parsed.append((name, ch, ('range', low, high)))
        else:
            parsed.append((name, ch, ('value', int(data))))
    return parsed
