import pytest
from pathlib import Path
from orchestrator.parser import ResultsParser

def test_parser_standard_csv(tmp_path):
    csv_file = tmp_path / "results.csv"
    csv_file.write_text("timeStamp,elapsed,label,responseCode,success\n1234567,100,Home,200,true\n1234568,200,About,200,false\n")
    
    parser = ResultsParser(str(csv_file))
    metrics = parser.parse()
    
    assert metrics is not None
    assert metrics.total_requests == 2
    assert metrics.error_count == 1
    assert metrics.avg_response_time == 150.0

def test_parser_case_insensitive_headers(tmp_path):
    csv_file = tmp_path / "results_case.csv"
    # Case variations and extra spaces
    csv_file.write_text(" TimeStamp , Elapsed , Label , ResponseCode , Success \n1234567,100,Home,200,true\n")
    
    parser = ResultsParser(str(csv_file))
    metrics = parser.parse()
    
    assert metrics is not None
    assert metrics.total_requests == 1
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
