"""MCP test server with 4 tools for e2e testing.

Runs as a standalone process. Uses RastirMCPMiddleware + @mcp_endpoint
to create server-side spans linked to the client's trace context.

Tools:
  - get_weather(city) → weather description
  - convert_temperature(value, from_unit, to_unit) → converted value
  - get_population(city) → population number
  - get_timezone(city) → timezone string

Usage:
    PYTHONPATH=src python tests/e2e/mcp_test_server.py [--port PORT]
"""

from __future__ import annotations

import argparse
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from mcp.server.fastmcp import FastMCP
import rastir
from rastir import configure, mcp_endpoint
from rastir.remote import RastirMCPMiddleware

# Configure to export server-side spans to collector
# Guard against double-configure when running in-process with client
try:
    configure(service="mcp-test-server", push_url="http://localhost:8080")
except RuntimeError:
    pass  # Already configured by the client process

mcp = FastMCP(
    "TestToolServer",
    host="127.0.0.1",
    port=19876,
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
@mcp_endpoint
async def get_weather(city: str) -> str:
    """Get the current weather for a city.

    Args:
        city: The name of the city to get weather for.

    Returns:
        A string describing the current weather conditions.
    """
    weather_data = {
        "tokyo": "22°C, partly cloudy, humidity 65%",
        "london": "15°C, rainy, humidity 80%",
        "new york": "28°C, sunny, humidity 45%",
        "paris": "18°C, overcast, humidity 70%",
        "sydney": "25°C, clear skies, humidity 55%",
    }
    return weather_data.get(city.lower(), f"No weather data available for {city}")


@mcp.tool()
@mcp_endpoint
async def convert_temperature(value: float, from_unit: str, to_unit: str) -> str:
    """Convert a temperature between Celsius and Fahrenheit.

    Args:
        value: The temperature value to convert.
        from_unit: The source unit ('celsius' or 'fahrenheit').
        to_unit: The target unit ('celsius' or 'fahrenheit').

    Returns:
        A string with the converted temperature.
    """
    from_unit = from_unit.lower()
    to_unit = to_unit.lower()

    if from_unit == to_unit:
        return f"{value}° {to_unit.title()}"

    if from_unit == "celsius" and to_unit == "fahrenheit":
        result = (value * 9 / 5) + 32
    elif from_unit == "fahrenheit" and to_unit == "celsius":
        result = (value - 32) * 5 / 9
    else:
        return f"Unknown units: {from_unit} → {to_unit}"

    return f"{value}° {from_unit.title()} = {result:.1f}° {to_unit.title()}"


@mcp.tool()
@mcp_endpoint
async def get_population(city: str) -> str:
    """Get the approximate population of a city.

    Args:
        city: The name of the city.

    Returns:
        A string with the population estimate.
    """
    populations = {
        "tokyo": "13.96 million",
        "london": "8.98 million",
        "new york": "8.34 million",
        "paris": "2.16 million",
        "sydney": "5.31 million",
    }
    return populations.get(city.lower(), f"Population data not available for {city}")


@mcp.tool()
@mcp_endpoint
async def get_timezone(city: str) -> str:
    """Get the timezone for a city.

    Args:
        city: The name of the city.

    Returns:
        A string with the timezone information.
    """
    timezones = {
        "tokyo": "JST (UTC+9)",
        "london": "GMT/BST (UTC+0/+1)",
        "new york": "EST/EDT (UTC-5/-4)",
        "paris": "CET/CEST (UTC+1/+2)",
        "sydney": "AEST/AEDT (UTC+10/+11)",
    }
    return timezones.get(city.lower(), f"Timezone data not available for {city}")


def create_app(port: int = 19876):
    """Create the ASGI app with middleware."""
    mcp.settings.port = port
    app = mcp.streamable_http_app()
    app = RastirMCPMiddleware(app)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=19876)
    args = parser.parse_args()

    import uvicorn
    app = create_app(args.port)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
