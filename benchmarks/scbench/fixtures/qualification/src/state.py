"""Tiny non-scored workspace used to qualify cross-checkpoint memory."""

VALUE = 1


def get_value() -> int:
    """Return the compatibility value that later checkpoints must preserve."""
    return VALUE
