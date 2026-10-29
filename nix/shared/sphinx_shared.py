"""The Sphinx settings that every docs site shares. nixidae/nixidae#1105.

A source's `docs/conf.py` imports these names and adds its own.
"""

from __future__ import annotations

import os

author = "Carl Andersson"

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
]

myst_enable_extensions = [
    "colon_fence",
    "deflist",
]

# The pages link to each other's sections, as `howto.md#run-a-test`.
myst_heading_anchors = 3

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "furo"

# The pages hold literal `--flag` mentions and ranges such as "0-7".
smartquotes = False

# A Nix build has no network, so it sets this variable and skips the inventory fetch.
intersphinx_mapping = {} if os.environ.get("NIXIDAE_DOCS_OFFLINE") else {"python": ("https://docs.python.org/3", None)}

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_default_options = {
    "member-order": "bysource",
}
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_use_rtype = False
