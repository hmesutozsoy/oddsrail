"""The example scripts must at least import and compile; they are the first
thing a stranger runs."""

import importlib.util
import pathlib
import py_compile


def test_footguns_compiles_and_imports():
    path = pathlib.Path(__file__).resolve().parents[1] / "examples" / "footguns.py"
    py_compile.compile(str(path), doraise=True)
    spec = importlib.util.spec_from_file_location("footguns", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # imports oddsrail, no network
    assert callable(mod.main) and callable(mod.one_book_arrives_worst_first)
