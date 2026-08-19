from typing import Any, Dict, List, Sequence, Tuple

# The MCP3008 is a 10-bit ADC, so a raw reading is 0..1023.
ADC_MAX = 1023

# Button values are matched against the raw reading. An earlier version squashed
# readings into 32 buckets first, which threw away the resolution that separates
# adjacent buttons on the ladder; a value at or below this looks like it was
# written in that scale rather than as a raw count.
LEGACY_VALUE_CEILING = 32


def median(values: Sequence[int]) -> int:
    """Middle value of `values`, biased low for an even count.

    Deliberately a median rather than a mean: a single ADC read corrupted by an
    LCD strobe or an I2S edge is an outlier, and a median discards it outright
    instead of averaging its error into the result.
    """
    ordered = sorted(values)
    if not ordered:
        raise ValueError("median() of empty sequence")
    return ordered[(len(ordered) - 1) // 2]


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

    Buttons on the ladder read in the hundreds, so a config where nothing
    exceeds 32 was almost certainly written before matching moved to raw counts
    and needs re-capturing. Worth saying out loud at startup, because otherwise
    the symptom is every button silently doing nothing.
    """
    if not parsed:
        return False
    return all(max(spec[1:]) <= LEGACY_VALUE_CEILING for _, _, spec in parsed)


def spec_contains(spec: Tuple, value: int, margin: int = 0) -> bool:
    """Whether `value` falls in `spec`, widened by `margin` counts each side."""
    if spec[0] == 'range':
        return (spec[1] - margin) <= value <= (spec[2] + margin)
    return abs(value - spec[1]) <= margin
