import yaml

try:
    with open('config/scenarios.yaml', 'r') as f:
        data = yaml.safe_load(f)
    print("YAML is valid!")
    print(data)
except Exception as e:
    print(f"YAML Error: {e}")
