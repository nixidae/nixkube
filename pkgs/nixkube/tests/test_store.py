# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from src.store import extract_store_name, extract_store_paths


class TestExtractStorePaths:
    """Tests for extract_store_paths function."""

    def test_empty_input(self):
        """Empty inputs should return empty set."""
        assert extract_store_paths("") == set()
        assert extract_store_paths({}) == set()
        assert extract_store_paths([]) == set()
        assert extract_store_paths(None) == set()

    def test_single_store_path_string(self):
        """Extract single store path from string."""
        path = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-hello"
        result = extract_store_paths(path)
        assert result == {Path(path)}

    def test_multiple_store_paths_in_string(self):
        """Extract multiple store paths from single string."""
        s = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-pkg1 /nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-pkg2"
        result = extract_store_paths(s)
        assert len(result) == 2
        assert Path("/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-pkg1") in result
        assert Path("/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-pkg2") in result

    def test_nested_dict(self):
        """Extract store paths from nested dict."""
        data = {
            "a": {
                "b": "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-nested",
                "c": ["/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-list"],
            }
        }
        result = extract_store_paths(data)
        assert len(result) == 2

    def test_nested_list(self):
        """Extract store paths from nested list."""
        data = [
            "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-one",
            {"key": "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-two"},
            ["/nix/store/cccccccccccccccccccccccccccccccc-three"],
        ]
        result = extract_store_paths(data)
        assert len(result) == 3

    def test_deduplication(self):
        """Duplicate paths should appear only once in result."""
        path = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-dedup"
        data = {
            "a": path,
            "b": path,
            "c": [path],
        }
        result = extract_store_paths(data)
        assert result == {Path(path)}

    def test_volume_attributes_excluded(self):
        """volumeAttributes key should be skipped (contains multiarch paths)."""
        data = {
            "storePath": "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-include",
            "volumeAttributes": {
                "x86_64": "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-exclude",
            },
        }
        result = extract_store_paths(data)
        # Only the storePath should be included, volumeAttributes skipped
        assert result == {Path("/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-include")}

    def test_partial_paths_ignored(self):
        """Paths that don't match the store path pattern should be ignored."""
        s = "/nix/store/abc1234 /nix/store/toolong notastorepath /nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-valid"
        result = extract_store_paths(s)
        assert result == {Path("/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-valid")}

    def test_store_path_with_special_chars(self):
        """Store path names can contain hyphens and alphanumerics."""
        path = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-package-name-123"
        result = extract_store_paths(path)
        assert result == {Path(path)}

    def test_a_path_inside_json_stops_at_the_quote(self):
        """Container env is JSON, and the name must not swallow its punctuation.

        `APPSTARTER_WANTED` is `builtins.toJSON` of a map from system to store
        path, and annotations.py feeds container env straight in. A name
        matching "anything but whitespace or a slash" took the closing quote
        and brace, and `nix build` fails on the whole argument list when one
        member cannot exist -- `appstarter-init` then never starts.
        """
        env = '{"x86_64-linux":"/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-cacheEnv"}'
        result = extract_store_paths(env)
        assert result == {Path("/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-cacheEnv")}

    def test_two_paths_in_one_json_object_stay_separate(self):
        """The two-architecture shape the node actually receives."""
        env = (
            '{"aarch64-linux":"/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-nodeEnv",'
            '"x86_64-linux":"/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-nodeEnv"}'
        )
        result = extract_store_paths(env)
        assert result == {
            Path("/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-nodeEnv"),
            Path("/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-nodeEnv"),
        }

    def test_a_name_keeps_the_characters_nix_allows(self):
        """`+`, `.`, `_`, `?` and `=` are legal in a store name; none may be cut."""
        path = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-gcc+wrapper-1.2.3_p1?x=y"
        result = extract_store_paths(path)
        assert result == {Path(path)}

    def test_path_objects_ignored(self):
        """Path objects should not be processed (handled as non-matching types)."""
        data = {
            "path_obj": Path("/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-one"),
            "string": "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-two",
        }
        result = extract_store_paths(data)
        # Only the string path should be extracted
        assert result == {Path("/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-two")}

    def test_mixed_nesting(self):
        """Complex nested structure with dicts, lists, and strings."""
        data = {
            "storePath": "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-root",
            "pods": [
                {
                    "containers": [
                        {"image": "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-image"},
                        {"image": "/nix/store/cccccccccccccccccccccccccccccccc-image2"},
                    ]
                }
            ],
        }
        result = extract_store_paths(data)
        assert len(result) == 3


class TestExtractStoreName:
    """Tests for extract_store_name function."""

    def test_full_path(self):
        """Extract name from full store path."""
        path = Path("/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-package-name")
        assert extract_store_name(path) == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-package-name"

    def test_string_input(self):
        """Extract name from string."""
        path = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-package"
        assert extract_store_name(path) == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-package"

    def test_already_name_only(self):
        """If already a name, return as-is."""
        name = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-pkg"
        assert extract_store_name(name) == name

    def test_empty_string(self):
        """Empty string should return empty."""
        assert extract_store_name("") == ""

    def test_preserves_dashes_and_numbers(self):
        """Store name format should be preserved."""
        path = "/nix/store/abcdef1234567890abcdef1234567890-pkg-1.0"
        expected = "abcdef1234567890abcdef1234567890-pkg-1.0"
        assert extract_store_name(path) == expected
