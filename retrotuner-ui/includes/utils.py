from typing import Any, Dict, List, Tuple

# Button values are matched against the raw 10-bit ADC reading. An earlier
# version squashed readings into 32 buckets first, which threw away the
# resolution that separates adjacent buttons on the ladder. Buttons read in the
# hundreds, so a config where nothing exceeds this was written in that old scale
# and can never match.
LEGACY_VALUE_CEILING = 32


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


def looks_like_legacy_values(parsed: List[Tuple[str, int, Tuple]]) -> bool:
    """Whether every parsed entry sits in the old 32-bucket range.

    Worth saying out loud at startup, because otherwise the symptom is every
    button silently doing nothing.
    """
    if not parsed:
        return False
    return all(max(spec[1:]) <= LEGACY_VALUE_CEILING for _, _, spec in parsed)


def spec_contains(spec: Tuple, value: int, margin: int = 0) -> bool:
    """Whether `value` falls in `spec`, widened by `margin` counts each side."""
    if spec[0] == 'range':
        return (spec[1] - margin) <= value <= (spec[2] + margin)
    return abs(value - spec[1]) <= margin
