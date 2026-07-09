"""S6 refactor: notebook/markdown export helpers moved out of the UI module.

These exercise sidekick.export_nb *directly* (not via sidekick.app), proving the
split gives the serializers independent, state-free testability.
"""
import json

from sidekick.client import Msg
from sidekick.export_nb import to_ipynb, to_markdown


def test_to_ipynb_direct_valid_nbformat():
    msgs = [Msg("n", "note", "# Title"),
            Msg("c", "code", "print(1)", output="1",
                rich=[{"type": "image/png", "data": "AAAA"}]),
            Msg("p", "prompt", "what?", output="because", model="codex")]
    nb = to_ipynb(msgs)
    assert nb["nbformat"] == 4 and nb["nbformat_minor"] == 5
    assert len(nb["cells"]) == 3
    assert nb["cells"][0]["cell_type"] == "markdown"
    code = nb["cells"][1]
    assert code["cell_type"] == "code" and "print(1)" in "".join(code["source"])
    kinds = []
    for o in code["outputs"]:
        kinds.append(o["name"]) if o["output_type"] == "stream" else kinds.extend(o["data"])
    assert "stdout" in kinds and "image/png" in kinds
    assert nb["cells"][2]["cell_type"] == "markdown"
    json.dumps(nb)  # must be JSON-serializable


def test_to_markdown_direct():
    md = to_markdown([
        Msg("n", "note", "## Hi"),
        Msg("c", "code", "x=1", output="ok",
            rich=[{"type": "image/png", "data": "BBBB"}]),
        Msg("p", "prompt", "q", output="a", model="glm"),
    ])
    assert "## Hi" in md
    assert "```python\nx=1\n```" in md and "```\nok\n```" in md
    assert "data:image/png;base64,BBBB" in md
    assert "**Prompt:** q" in md and "**glm:**" in md


def test_app_reexports_stay_identical():
    # app.<name> must resolve to the very same objects (tests reference app.to_ipynb).
    import sidekick.app as app
    assert app.to_ipynb is to_ipynb
    assert app.to_markdown is to_markdown
