from __future__ import annotations

import os

project = "nixkube"
copyright = "2025, Carl Andersson"
author = "Carl Andersson"

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx.ext.graphviz",
]

graphviz_output_format = "svg"

myst_enable_extensions = [
    "colon_fence",
    "deflist",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "furo"

smartquotes = False

intersphinx_mapping = {} if os.environ.get("NIXCSI_DOCS_OFFLINE") else {"python": ("https://docs.python.org/3", None)}
