from os.path import join as join_path
import sys as system

from .core import compute as compute_alias


def use_join() -> str:
    return join_path("a", "b")
