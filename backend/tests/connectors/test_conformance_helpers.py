"""Control tests for the shared conformance assertion helpers.

``_conformance.py`` ships the single gate every registered connector must pass
(``assert_result_shape``, ``assert_write_result_shape``, ``assert_health_shape``).
If one helper silently stops flagging a violation, the whole parametrised
conformance suite weakens at once — every connector reports green against a
contract nobody is enforcing. These control tests pin each helper to its
documented contract with positive (must-pass) and negative (must-fail) inputs,
including the JSON round-trip subtleties the assertions are documented to catch
(non-string dict keys, NaN float corruption).
"""

import pytest

from modulo.connectors.base import ConnectorResult, HealthResult
from tests.connectors._conformance import (
    assert_health_shape,
    assert_result_shape,
    assert_write_result_shape,
)


class TestAssertResultShape:
    def test_accepts_valid_result(self) -> None:
        assert_result_shape(
            ConnectorResult(
                records=[{"name": "f.txt", "type": "file"}],
                next_cursor=None,
                total=1,
                metadata={"elapsed_ms": 3},
            )
        )

    def test_rejects_non_connector_result(self) -> None:
        with pytest.raises(AssertionError, match="Expected ConnectorResult"):
            assert_result_shape({"records": []})

    def test_rejects_non_list_records(self) -> None:
        with pytest.raises(AssertionError, match="records must be a list"):
            assert_result_shape(ConnectorResult(records=("x",)))

    def test_rejects_non_dict_record(self) -> None:
        with pytest.raises(AssertionError, match="records must contain only dicts"):
            assert_result_shape(ConnectorResult(records=["not-a-dict"]))

    def test_rejects_nonnumeric_total(self) -> None:
        with pytest.raises(AssertionError, match="total must be None or int"):
            assert_result_shape(ConnectorResult(records=[], total="1"))

    def test_rejects_negative_total(self) -> None:
        with pytest.raises(AssertionError, match="total must be non-negative"):
            assert_result_shape(ConnectorResult(records=[], total=-1))

    def test_rejects_wrong_metadata_type(self) -> None:
        result = ConnectorResult()
        result.metadata = []
        with pytest.raises(AssertionError, match="metadata must be a dict"):
            assert_result_shape(result)

    def test_rejects_metadata_with_non_string_dict_key(self) -> None:
        result = ConnectorResult(records=[], metadata={1: "x"})
        with pytest.raises(AssertionError, match="does not survive a JSON round-trip"):
            assert_result_shape(result)

    def test_rejects_metadata_with_nan_float(self) -> None:
        result = ConnectorResult(records=[], metadata={"price": float("nan")})
        with pytest.raises(AssertionError, match="does not survive a JSON round-trip"):
            assert_result_shape(result)


class TestAssertWriteResultShape:
    def test_accepts_serializable_dict(self) -> None:
        assert_write_result_shape({"path": "/tmp/out.txt", "bytes_written": 4})

    def test_rejects_non_dict(self) -> None:
        with pytest.raises(AssertionError, match="Expected dict from write"):
            assert_write_result_shape(["not-a-dict"])

    def test_rejects_non_string_dict_key(self) -> None:
        with pytest.raises(AssertionError, match="does not survive a JSON round-trip"):
            assert_write_result_shape({"bytes_written": {1: "x"}})


class TestAssertHealthShape:
    def test_accepts_ok(self) -> None:
        assert_health_shape(HealthResult(ok=True, detail="ready"))

    def test_accepts_failed_with_detail(self) -> None:
        assert_health_shape(HealthResult(ok=False, detail="credential revoked"))

    def test_rejects_non_health_result(self) -> None:
        with pytest.raises(AssertionError, match="Expected HealthResult"):
            assert_health_shape({"ok": True, "detail": "x"})

    def test_rejects_silent_failure(self) -> None:
        with pytest.raises(AssertionError, match="must provide a non-empty detail"):
            assert_health_shape(HealthResult(ok=False, detail=""))
