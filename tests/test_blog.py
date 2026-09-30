"""Tests for pappus.blog — projecting a dialog into a Quarto blog post.

No network: exercises the pure cell→nbformat mapping and file assembly. The key
property vs. the library path is that **outputs are kept** (the library emits
`outputs: []`; the blog carries stdout + plots into the post).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pappus import blog
from pappus.client import Msg


def _dialog():
    """A note, a plotted code cell (stdout + a png), a scratch-less code cell, a prompt."""
    return [
        Msg(id="_n1", msg_type="note", content="# Sine\nA quick plot."),
        Msg(id="_c1", msg_type="code",
            content="#| export mylib:core\nprint('hi')\nplot()",
            output="hi",
            rich=[{"type": "image/png", "data": "BASE64PNGDATA"}]),
        Msg(id="_p1", msg_type="prompt", content="Why sine?",
            output="Because it's periodic."),
        Msg(id="_c2", msg_type="code", content="x = 1"),   # no output yet
    ]


def test_post_structure():
    nb = blog.dialog_to_post(_dialog(), title="My Post", date="2026-07-02")
    assert nb["nbformat"] == 4
    kinds = [c["cell_type"] for c in nb["cells"]]
    # front-matter raw cell, then note(md), code, prompt(md), code
    assert kinds == ["raw", "markdown", "code", "markdown", "code"]


def test_frontmatter():
    nb = blog.dialog_to_post(_dialog(), title="My: Post", date="2026-07-02")
    fm = "".join(nb["cells"][0]["source"])
    assert fm.startswith("---")
    assert 'title: "My: Post"' in fm      # colon safely quoted
    assert 'date: "2026-07-02"' in fm


def test_author_in_frontmatter():
    nb = blog.dialog_to_post(_dialog(), title="p", author="Ada Lovelace")
    fm = "".join(nb["cells"][0]["source"])
    assert 'author: "Ada Lovelace"' in fm


def test_author_omitted_when_blank():
    nb = blog.dialog_to_post(_dialog(), title="p")       # no author
    fm = "".join(nb["cells"][0]["source"])
    assert "author:" not in fm


def test_outputs_are_kept():
    nb = blog.dialog_to_post(_dialog(), title="p")
    code = [c for c in nb["cells"] if c["cell_type"] == "code"][0]
    types = [o["output_type"] for o in code["outputs"]]
    assert "stream" in types and "display_data" in types
    disp = [o for o in code["outputs"] if o["output_type"] == "display_data"][0]
    assert disp["data"]["image/png"] == "BASE64PNGDATA"


def test_directives_stripped_from_code():
    nb = blog.dialog_to_post(_dialog(), title="p")
    code = [c for c in nb["cells"] if c["cell_type"] == "code"][0]
    src = "".join(code["source"])
    assert "#| export" not in src         # library directive is reader-noise
    assert "print('hi')" in src


def test_prompt_rendered_as_prose():
    nb = blog.dialog_to_post(_dialog(), title="p")
    md = "".join(nb["cells"][3]["source"])
    assert "> Why sine?" in md            # question quoted
    assert "Because it's periodic." in md


def test_empty_code_cell_has_null_execution_count():
    nb = blog.dialog_to_post(_dialog(), title="p")
    last = nb["cells"][-1]                 # `x = 1`, never run
    assert last["cell_type"] == "code"
    assert last["execution_count"] is None
    assert last["outputs"] == []


def test_blog_files_layout():
    nb = blog.dialog_to_post(_dialog(), title="p")
    files = blog.blog_files({"my_post": nb}, title="Pappus")
    assert set(files) == {"_quarto.yml", "index.qmd", "posts/my_post.ipynb"}
    # the emitted post is valid JSON
    json.loads(files["posts/my_post.ipynb"])


class _FakeBackend:
    def __init__(self, dialogs):
        self._d = dialogs

    def list_dialogs(self):
        return list(self._d)

    def messages(self, name):
        return self._d[name]


def test_build_blog_writes_files(tmp_path):
    be = _FakeBackend({"intro": _dialog()})
    result = blog.build_blog(be, ["intro"], str(tmp_path), title="Pappus", date="2026-07-02")
    assert result["posts"] == ["intro"]
    assert (tmp_path / "posts" / "intro.ipynb").exists()
    assert (tmp_path / "_quarto.yml").exists()
    assert (tmp_path / "index.qmd").exists()


def test_build_blog_defaults_author_from_git(tmp_path, monkeypatch):
    # No explicit author -> build_blog fills the byline from git user.name.
    monkeypatch.setattr("pappus.nbdev_export._git_identity",
                        lambda: ("Grace Hopper", "grace@example.com"))
    be = _FakeBackend({"intro": _dialog()})
    blog.build_blog(be, ["intro"], str(tmp_path), title="Pappus")
    post = json.loads((tmp_path / "posts" / "intro.ipynb").read_text())
    fm = "".join(post["cells"][0]["source"])
    assert 'author: "Grace Hopper"' in fm


def test_publish_dialog_rename_leaves_one_post(tmp_path):
    # Publishing, renaming the dialog, then re-publishing must reconcile: the old
    # post is pruned (its dialog no longer exists), leaving exactly one.
    be = _FakeBackend({"topic/a": _dialog()})
    r1 = blog.publish_dialog(be, "topic/a", str(tmp_path), date="2026-07-02")
    assert (tmp_path / "posts" / f"{r1['slug']}.ipynb").exists()
    be._d["topic/b"] = be._d.pop("topic/a")                  # rename: old name disappears
    r2 = blog.publish_dialog(be, "topic/b", str(tmp_path), date="2026-07-02")
    posts = list((tmp_path / "posts").glob("*.ipynb"))
    assert len(posts) == 1 and posts[0].name == f"{r2['slug']}.ipynb"
    assert f"posts/{r1['slug']}.ipynb" in r2["pruned"]       # orphan removed


def test_publish_dialog_distinct_slug_on_collision(tmp_path):
    # Two different dialog names that slug to the same base must not overwrite each
    # other; a re-publish reuses the dialog's own recorded slug (stable URL).
    be = _FakeBackend({"topic/a": _dialog(), "topic-a": _dialog()})
    r1 = blog.publish_dialog(be, "topic/a", str(tmp_path))
    r2 = blog.publish_dialog(be, "topic-a", str(tmp_path))
    assert r1["slug"] != r2["slug"]                          # de-duped against disk
    assert (tmp_path / "posts" / f"{r1['slug']}.ipynb").exists()
    assert (tmp_path / "posts" / f"{r2['slug']}.ipynb").exists()
    r1b = blog.publish_dialog(be, "topic/a", str(tmp_path))
    assert r1b["slug"] == r1["slug"]                         # re-publish reuses its slug
    assert len(list((tmp_path / "posts").glob("*.ipynb"))) == 2
