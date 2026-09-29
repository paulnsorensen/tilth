"""Core fixture: nested definitions, duplicate names, multiline signatures."""


def compute(
    a: int,
    b: int,
) -> int:
    """Computes the outer value."""
    return a + b


class Inner:
    @staticmethod
    def compute() -> int:
        """Shadows the outer compute name."""
        return 0

    class Deep:
        @staticmethod
        def compute() -> int:
            return 1
