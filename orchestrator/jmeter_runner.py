import subprocess
import os

class JMeterRunner:
    def __init__(self, jmeter_path="jmeter"):
        self.jmeter_path = jmeter_path

    def run(self, jmx_path, result_path, users, slaves=None):
        """
        Runs JMeter in CLI mode.
        -n: non-GUI mode
        -t: JMX file path
        -l: JTL result file path
        -Jusers: set property 'users'
        -R: list of slave nodes (optional)
        """
        command = [
            self.jmeter_path,
            "-n",
            "-t", jmx_path,
            "-l", result_path,
            f"-Jusers={users}"
        ]

        if slaves:
            command.extend(["-R", ",".join(slaves)])

        print(f"Executing: {' '.join(command)}")
        
        try:
            # Run JMeter and wait for it to finish
            process = subprocess.run(command, capture_output=True, text=True, check=True)
            return True, process.stdout
        except subprocess.CalledProcessError as e:
            print(f"JMeter execution failed with exit code {e.returncode}")
            print(f"Error output: {e.stderr}")
            return False, e.stderr
        except Exception as e:
            print(f"An unexpected error occurred: {str(e)}")
            return False, str(e)
