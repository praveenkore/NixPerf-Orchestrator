import pytest
from pathlib import Path
from orchestrator.parser import ResultsParser


def test_parser_standard_csv(tmp_path):
    csv_file = tmp_path / "results.csv"
    csv_file.write_text(
        "timeStamp,elapsed,label,responseCode,success\n"
        "1234567,100,Home,200,true\n"
        "1234568,200,About,500,false\n"  # HTTP 500 = real error
    )

    parser = ResultsParser(str(csv_file))
    metrics = parser.parse()

    assert metrics is not None
    assert metrics.total_requests == 2
    assert metrics.error_count == 1      # HTTP 500 counted
    assert metrics.error_percent == 50.0
    assert metrics.avg_response_time == 150.0


def test_parser_assertion_failure_not_counted_as_http_error(tmp_path):
    """Assertion failures (success=false, responseCode=200) must NOT count as HTTP errors.
    This was the root bug that caused 48% reported errors when JMeter console showed 0%.
    """
    csv_file = tmp_path / "results_assert.csv"
    csv_file.write_text(
        "timeStamp,elapsed,label,responseCode,success\n"
        "1234567,100,Home,200,false\n"   # assertion failed but HTTP 200
        "1234568,200,About,200,true\n"   # all good
    )

    parser = ResultsParser(str(csv_file))
    metrics = parser.parse()

    assert metrics is not None
    assert metrics.total_requests == 2
    assert metrics.error_count == 0       # NO HTTP errors
    assert metrics.error_percent == 0.0   # matches JMeter console 'Err: 0 (0%)'


def test_parser_http_error_4xx_counted(tmp_path):
    """4xx response codes must be counted as errors."""
    csv_file = tmp_path / "results_4xx.csv"
    csv_file.write_text(
        "timeStamp,elapsed,label,responseCode,success\n"
        "1234567,100,Login,401,false\n"
        "1234568,200,Home,200,true\n"
        "1234569,150,Profile,403,false\n"
    )

    parser = ResultsParser(str(csv_file))
    metrics = parser.parse()

    assert metrics is not None
    assert metrics.total_requests == 3
    assert metrics.error_count == 2       # 401 + 403
    assert round(metrics.error_percent, 2) == round(2 / 3 * 100, 2)


def test_parser_non_numeric_response_code_counted_as_error(tmp_path):
    """Non-numeric response codes (e.g. 'Non HTTP response code') = error."""
    csv_file = tmp_path / "results_nonnumeric.csv"
    csv_file.write_text(
        "timeStamp,elapsed,label,responseCode,success\n"
        "1234567,100,Home,200,true\n"
        "1234568,500,API,Non HTTP response code,false\n"
    )

    parser = ResultsParser(str(csv_file))
    metrics = parser.parse()

    assert metrics is not None
    assert metrics.total_requests == 2
    assert metrics.error_count == 1


def test_parser_case_insensitive_headers(tmp_path):
    csv_file = tmp_path / "results_case.csv"
    # Case variations and extra spaces
    csv_file.write_text(
        " TimeStamp , Elapsed , Label , ResponseCode , Success \n"
        "1234567,100,Home,200,true\n"
    )

    parser = ResultsParser(str(csv_file))
    metrics = parser.parse()

    assert metrics is not None
    assert metrics.total_requests == 1
    assert metrics.error_count == 0
    assert metrics.avg_response_time == 100.0


def test_parser_missing_header_fails_gracefully(tmp_path):
    csv_file = tmp_path / "results_no_header.csv"
    # No header, just data. DictReader will use first line as header.
    csv_file.write_text("1234567,100,Home,200,true\n1234568,200,About,200,false\n")

    parser = ResultsParser(str(csv_file))
    metrics = parser.parse()

    # Should be None because it can't find 'elapsed' or 'success' keys
    assert metrics is None


def test_parser_empty_file(tmp_path):
    csv_file = tmp_path / "empty.csv"
    csv_file.write_text("")

    parser = ResultsParser(str(csv_file))
    metrics = parser.parse()

    assert metrics is None
