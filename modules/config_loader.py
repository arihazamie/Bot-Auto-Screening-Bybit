import json
import os
from dotenv import load_dotenv

load_dotenv()

def load_config():
    if not os.path.exists('config.json'): return {}
    with open('config.json', 'r') as f:
        config = json.load(f)
    return config

CONFIG = load_config()
