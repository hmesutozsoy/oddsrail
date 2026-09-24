"""Entry point Claude Desktop runs for the oddsrail bundle.

The bundle holds no code of its own: pyproject.toml pins the published
oddsrail package and uv installs it, so the bundle and `pip install oddsrail`
always run exactly the same server.
"""
from oddsrail.server import main

if __name__ == "__main__":
    main()
