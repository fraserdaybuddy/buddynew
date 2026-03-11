"""
run_server.py — Start the JOB-006 dashboard API server
Usage: PYTHONUTF8=1 python run_server.py
Then open: dashboard/betting-dashboard.html in your browser
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from src.api.server import main
main()
