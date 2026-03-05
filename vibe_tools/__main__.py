"""Allow running as: python -m vibe_tools"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from vibe_tools.main import main

main()
