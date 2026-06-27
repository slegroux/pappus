"""Tests for sidekick.export — tangling a dialog into an installable package.

No server, no network: exercises the pure tangle/emit logic and verifies the
generated package actually imports.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidekick import export
from sidekick.client import Msg


def _dialog():
    """A small dialog: a note, an exported fn, a scratch cell, an exported submodule fn."""
    return [
        Msg(id="_n1", msg_type="note", content="# Adder\nImplements the toy paper."),
        Msg(id="_c1", msg_type="code",
            content="#| default_exp core\n#| export\ndef add(a, b):\n    return a + b"),
        Msg(id="_c2", msg_type="code", content="print(add(1, 2))  # scratch, not exported"),
        Msg(id="_c3", msg_type="code",
            content="#| export geometry\nimport math\n\ndef area(r):\n    return math.pi * r * r"),
        Msg(id="_n2", msg_type="note", content="Notes carry into the README."),
    ]


def test_slug():
    assert export.slug("My Cool Paper!") == "my_cool_paper"
    assert export.slug("123 abc").startswith("pkg_")


def test_file_set():
    files = export.dialog_to_package(_dialog(), "Adder Paper")
    assert set(files) == {
        "pyproject.toml", "README.md",
        "src/adder_paper/__init__.py",
        "src/adder_paper/core.py",
        "src/adder_paper/geometry.py",
    }


def test_only_exported_cells_tangled():
    files = export.dialog_to_package(_dialog(), "adder")
    core = files["src/adder/core.py"]
    assert "def add" in core
    assert "scratch" not in core           # the un-marked cell is dropped
    assert "#| export" not in core         # directive lines are stripped
    assert "#| default_exp" not in core
    assert "def area" in files["src/adder/geometry.py"]


def test_init_reexports_public_names():
    files = export.dialog_to_package(_dialog(), "adder")
    init = files["src/adder/__init__.py"]
    assert "from .core import add" in init
    assert "from .geometry import area" in init
    assert '"add"' in init and '"area"' in init


def test_pyproject_deps_are_third_party_only():
    files = export.dialog_to_package(_dialog(), "adder")
    proj = files["pyproject.toml"]
    assert 'name = "adder"' in proj
    assert "math" not in proj              # stdlib is not a dependency
    assert "adder" not in proj.split("dependencies")[1]  # not its own package


def test_pypi_name_mapping():
    msgs = [Msg(id="_c", msg_type="code", content="#| export\nimport sklearn\nx = 1")]
    proj = export.dialog_to_package(msgs, "p")["pyproject.toml"]
    assert "scikit-learn" in proj
    assert "sklearn" not in proj


def test_notes_flow_into_readme():
    readme = export.dialog_to_package(_dialog(), "adder")["README.md"]
    assert "Implements the toy paper." in readme
    assert "Notes carry into the README." in readme


def test_provenance_header_and_source():
    files = export.dialog_to_package(_dialog(), "adder",
                                     source_url="https://arxiv.org/abs/1234.5678",
                                     dialog_name="adder-dialog")
    assert "1234.5678" in files["README.md"]
    assert "adder-dialog" in files["src/adder/core.py"]


def test_hostile_dialog_name_produces_valid_module():
    # A dialog name containing triple-quotes / backslashes must not break the
    # generated module (no docstring injection / SyntaxError).
    import ast
    evil = 'evil"""\\n+__import__(\'os\').system("x")#'
    files = export.dialog_to_package(
        [Msg(id="_c", msg_type="code", content="#| export\nx = 1")],
        "pkg", dialog_name=evil, source_url='">>>"')
    for rel, content in files.items():
        if rel.endswith(".py"):
            ast.parse(content)                       # every module stays valid Python
    assert evil.replace("\n", " ") in files["src/pkg/core.py"]   # provenance preserved


def test_duplicate_export_names_deduped_across_modules():
    # Two modules each defining `f`: __init__ must not duplicate it or shadow-import.
    import ast
    msgs = [Msg(id="_a", msg_type="code", content="#| export m1\ndef f(): return 1"),
            Msg(id="_b", msg_type="code", content="#| export m2\ndef f(): return 2")]
    init = export.dialog_to_package(msgs, "p")["src/p/__init__.py"]
    ast.parse(init)                                  # valid
    assert init.count('"f"') == 1                    # f appears once in __all__
    assert init.count("import f") == 1               # re-exported once (first wins)


def test_generated_package_imports(tmp_path):
    """The tangled package must actually import and expose its symbols."""
    files = export.dialog_to_package(_dialog(), "adder")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    src = str(tmp_path / "src")
    sys.path.insert(0, src)
    try:
        import importlib
        mod = importlib.import_module("adder")
        assert mod.add(2, 3) == 5
        assert round(mod.area(1.0), 5) == round(3.141592653589793, 5)
    finally:
        sys.path.remove(src)
        for name in [n for n in sys.modules if n == "adder" or n.startswith("adder.")]:
            del sys.modules[name]


def test_package_zip_has_root_prefix():
    import io
    import zipfile

    files = export.dialog_to_package(_dialog(), "adder")
    blob = export.package_zip(files, "adder")
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = zf.namelist()
    assert "adder/pyproject.toml" in names
    assert "adder/src/adder/core.py" in names


def test_empty_dialog_is_safe():
    files = export.dialog_to_package([], "empty")
    assert "pyproject.toml" in files
    assert files["src/empty/__init__.py"]  # still valid, just no re-exports


def test_toggle_export_roundtrip():
    code = "def f():\n    return 1"
    on = export.toggle_export(code)
    assert export.has_export(on)
    assert on.startswith("#| export")
    off = export.toggle_export(on)
    assert not export.has_export(off)
    assert off.strip() == code


def test_toggle_export_keeps_default_exp():
    code = "#| default_exp core\n#| export\ndef f(): ...\n"
    off = export.toggle_export(code)
    assert "#| default_exp core" in off
    assert not export.has_export(off)


def test_export_package_route_returns_zip():
    """The /export/package route tangles the live dialog into a downloadable zip."""
    import io
    import zipfile

    from sidekick import app as appmod

    backend = appmod.STATE["backend"]
    dialog = appmod.STATE["dialog"]
    backend.add(dialog, "#| export\ndef hello():\n    return 'hi'", "code")
    resp = appmod.export_package()
    assert resp.media_type == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.body)) as zf:
        names = zf.namelist()
    assert any(n.endswith("/__init__.py") for n in names)
    assert any(n.endswith("pyproject.toml") for n in names)
