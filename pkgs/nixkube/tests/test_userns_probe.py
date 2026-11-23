# SPDX-License-Identifier: Apache-2.0

from src.userns_probe import ERROR_ANNOTATION, USERNS_LABEL, node_patch


def test_a_working_node_is_labelled_true_and_loses_an_old_error() -> None:
    assert node_patch(None) == {"metadata": {"labels": {USERNS_LABEL: "true"}, "annotations": {ERROR_ANNOTATION: None}}}


def test_a_failing_node_is_labelled_false_with_the_error() -> None:
    error = "[Errno 1] Operation not permitted"
    assert node_patch(error) == {
        "metadata": {"labels": {USERNS_LABEL: "false"}, "annotations": {ERROR_ANNOTATION: error}}
    }
