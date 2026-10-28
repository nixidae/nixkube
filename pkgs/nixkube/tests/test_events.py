# SPDX-License-Identifier: Apache-2.0

from hypothesis import assume, given, strategies as st

from src.events import _format_event_note


class TestFormatEventNote:
    """Tests for _format_event_note truncation logic."""

    MAX_SIZE = 1000  # Should match constants in events.py

    def test_message_only_no_logs(self):
        """Message without logs should return message unchanged."""
        msg = "Build failed"
        result = _format_event_note(msg)
        assert result == msg
        assert len(result.encode()) <= self.MAX_SIZE

    def test_message_with_empty_logs(self):
        """Empty logs should return message unchanged."""
        msg = "Error occurred"
        result = _format_event_note(msg, logs="")
        assert result == msg

    def test_message_with_none_logs(self):
        """None logs should return message unchanged."""
        msg = "Warning"
        result = _format_event_note(msg, logs=None)
        assert result == msg

    def test_message_and_short_logs(self):
        """Message and short logs should be combined with newline."""
        msg = "Build failed"
        logs = "Error: out of memory"
        result = _format_event_note(msg, logs=logs)
        assert result == f"{msg}\n{logs}"
        assert len(result.encode()) <= self.MAX_SIZE

    def test_long_logs_truncated(self):
        """Very long logs should be truncated to fit 1000 byte limit."""
        msg = "Build failed"
        logs = "x" * 2000  # Very long logs
        result = _format_event_note(msg, logs=logs)
        assert result.startswith(msg)
        assert len(result.encode()) <= self.MAX_SIZE
        # Should contain some of the logs (recent end)
        assert "x" in result

    def test_logs_most_recent_kept(self):
        """Recent portion of logs (last 1000 chars) should be kept."""
        msg = "Build"
        logs_end = "final error message"
        logs = "x" * 5000 + logs_end
        result = _format_event_note(msg, logs=logs)
        # Should contain end of logs but not be all padding
        assert logs_end in result or "final" in result

    def test_utf8_multibyte_chars(self):
        """UTF-8 multi-byte characters should be handled correctly."""
        msg = "Build error"
        logs = "Output: こんにちは 世界 🚀 café"
        result = _format_event_note(msg, logs=logs)
        assert len(result.encode()) <= self.MAX_SIZE
        # All UTF-8 should be preserved if it fits
        assert logs in result

    def test_truncation_at_utf8_boundary(self):
        """Truncation should not split UTF-8 multi-byte sequences."""
        msg = "Error"
        # Create logs that will be truncated exactly at a multi-byte boundary
        logs = "Start" + "🚀" * 500  # Many 4-byte emoji characters
        result = _format_event_note(msg, logs=logs)
        assert len(result.encode()) <= self.MAX_SIZE
        # Result should be valid UTF-8 (no split sequences)
        try:
            result.encode().decode()  # Should not raise
        except UnicodeDecodeError:
            raise AssertionError("Result contains invalid UTF-8") from None

    def test_large_message_small_logs(self):
        """Large message with small logs."""
        msg = "a" * 900
        logs = "Error detail"
        result = _format_event_note(msg, logs=logs)
        assert len(result.encode()) <= self.MAX_SIZE
        assert msg in result

    def test_message_with_newlines(self):
        """Message containing newlines should be preserved."""
        msg = "Build failed\nDetails: timeout\nContext: test-pod"
        logs = "stdout logs"
        result = _format_event_note(msg, logs=logs)
        assert msg in result
        assert logs in result

    def test_logs_with_newlines_truncated(self):
        """Multi-line logs should be truncated but newlines preserved."""
        msg = "Error"
        logs = "\n".join([f"Line {i}" for i in range(200)])
        result = _format_event_note(msg, logs=logs)
        assert len(result.encode()) <= self.MAX_SIZE
        assert msg in result

    def test_exact_fit_boundary(self):
        """Logs that fit exactly at boundary should not be truncated."""
        msg = "x"
        # Calculate exact size: message (1 byte) + newline (1 byte) + remaining
        available = self.MAX_SIZE - len(b"x") - 1
        logs = "y" * available
        result = _format_event_note(msg, logs=logs)
        assert len(result.encode()) <= self.MAX_SIZE
        # All logs should fit
        assert logs in result

    def test_result_always_within_limit(self):
        """Any valid input should produce output within limit."""
        test_cases = [
            ("Short", ""),
            ("Message", "Logs"),
            ("m" * 500, "l" * 2000),
            ("Error", "🚀" * 300),
            ("x", "y" * 10000),
        ]
        for msg, logs in test_cases:
            result = _format_event_note(msg, logs=logs)
            assert len(result.encode()) <= self.MAX_SIZE, (
                f"Result exceeded limit: {len(result.encode())} > {self.MAX_SIZE}"
            )


class TestFormatEventNoteProperties:
    """Property-based tests for _format_event_note using Hypothesis.

    These complement the example-based tests above by generating hundreds of
    random Unicode strings — including emoji, Arabic, Chinese, null bytes, and
    strings at exact byte boundaries — to verify invariants the implementation
    must always uphold.
    """

    @given(
        message=st.text(),
        logs=st.one_of(st.none(), st.text()),
    )
    def test_byte_limit_always_holds(self, message, logs):
        """Output is always at most 1000 bytes. For *any* input.

        This carried `assume(len(message.encode()) <= 1000)`, which excluded
        the one input the function got wrong: an over-long message raised
        `AssertionError` instead of being cut. The property is only worth
        anything unconditionally -- this runs while reporting some other
        failure, and what it raises replaces that failure.
        """
        result = _format_event_note(message, logs)
        assert len(result.encode()) <= 1000

    @given(logs=st.one_of(st.none(), st.text()))
    def test_a_message_at_the_limit_does_not_raise(self, logs):
        """The boundary the second assertion tripped on.

        At exactly 1000 bytes the space left for logs goes negative, and the
        newline separator alone took the result to 1001.
        """
        result = _format_event_note("a" * 1000, logs)
        assert len(result.encode()) <= 1000

    @given(
        message=st.text(),
        logs=st.one_of(st.none(), st.text()),
    )
    def test_message_always_preserved(self, message, logs):
        """Message is always present verbatim in the output.

        This one keeps its `assume`, and means it: a message over the limit
        is cut, so it cannot also be verbatim. The byte-limit property above
        is the one that has to hold for every input.
        """
        assume(len(message.encode()) <= 1000)
        result = _format_event_note(message, logs)
        assert message in result
