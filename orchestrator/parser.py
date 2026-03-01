import csv
import statistics
import os

class ResultsParser:
    def __init__(self, file_path):
        self.file_path = file_path
        self.results = []

    def parse(self):
        """
        Parses JMeter result files (CSV or JTL) and calculates key metrics.
        JMeter default CSV/JTL format:
        timeStamp,elapsed,label,responseCode,responseMessage,threadName,dataType,success,failureMessage,bytes,sentBytes,grpThreads,allThreads,URL,Latency,IdleTime,Connect
        """
        if not os.path.exists(self.file_path):
            raise FileNotFoundError(f"Result file not found: {self.file_path}")

        elapsed_times = []
        error_count = 0
        total_count = 0

        with open(self.file_path, mode='r', encoding='utf-8') as f:
            # JMeter CSV can have header
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    elapsed = int(row['elapsed'])
                    success = row['success'].lower() == 'true'
                    
                    elapsed_times.append(elapsed)
                    if not success:
                        error_count += 1
                    total_count += 1
                except (ValueError, KeyError):
                    continue

        if total_count == 0:
            return None

        metrics = {
            "total_requests": total_count,
            "error_count": error_count,
            "error_percent": (error_count / total_count) * 100,
            "avg_response_time": sum(elapsed_times) / total_count if elapsed_times else 0,
            "min_response_time": min(elapsed_times) if elapsed_times else 0,
            "max_response_time": max(elapsed_times) if elapsed_times else 0,
            "p95": statistics.quantiles(elapsed_times, n=20)[18] if len(elapsed_times) >= 2 else 0,
            "p99": statistics.quantiles(elapsed_times, n=100)[98] if len(elapsed_times) >= 2 else 0,
        }
        
        return metrics

if __name__ == "__main__":
    # Test with a dummy file if needed
    pass
