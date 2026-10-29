from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nix" / "shared"))

from sphinx_shared import (
    author as author,
    exclude_patterns as exclude_patterns,
    extensions as shared_extensions,
    html_theme as html_theme,
    intersphinx_mapping as intersphinx_mapping,
    myst_enable_extensions as myst_enable_extensions,
    smartquotes as smartquotes,
    templates_path as templates_path,
)

project = "nixkube"
copyright = "2025, Carl Andersson"

extensions = [*shared_extensions, "sphinx.ext.graphviz"]

graphviz_output_format = "svg"
