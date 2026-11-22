"""Project scaffold templates for `/init <template>`."""

from __future__ import annotations

SCAFFOLD_TEMPLATES = {
    "quant": {
        "desc": "量化策略与回测脚手架（可对接 financial-service MCP）",
        "dirs": ["data", "strategy", "backtest", "tests"],
        "files": {
            "requirements.txt": "pandas\nnumpy\npytest\n",
            "strategy/dual_ma.py": (
                "import pandas as pd\n\n\n"
                "def generate_signals(close: pd.Series, fast: int = 5, slow: int = 20) -> pd.Series:\n"
                "    fast_ma = close.rolling(fast).mean()\n"
                "    slow_ma = close.rolling(slow).mean()\n"
                "    sig = pd.Series(0, index=close.index)\n"
                "    sig[fast_ma > slow_ma] = 1\n"
                "    sig[fast_ma < slow_ma] = -1\n"
                "    return sig\n"
            ),
            "main.py": (
                '"""Entrypoint — pair with arthera-finance MCP tools for live quotes and backtests."""\n'
                "if __name__ == \"__main__\":\n"
                "    print(\"Ready. Run via Aria Code or financial-service MCP.\")\n"
            ),
        },
    },
    "analysis": {
        "desc": "数据分析项目（数据加载 / 统计 / 可视化）",
        "dirs": ["data/raw", "data/processed", "src", "output"],
        "files": {
            "requirements.txt": "pandas\nnumpy\nmatplotlib\n",
            "src/analyzer.py": (
                "import pandas as pd\n\n\n"
                "def describe_df(df: pd.DataFrame) -> pd.DataFrame:\n"
                "    return df.describe().round(4)\n"
            ),
            "main.py": (
                '"""Data analysis project entrypoint."""\n'
                "if __name__ == \"__main__\":\n"
                "    print(\"Data analysis workspace ready.\")\n"
            ),
        },
    },
    "fastapi": {
        "desc": "FastAPI 服务脚手架（REST / 健康检查 / 测试）",
        "dirs": ["app/routers", "tests"],
        "files": {
            "requirements.txt": "fastapi\nuvicorn[standard]\nhttpx\npytest\n",
            "app/main.py": (
                "from fastapi import FastAPI\n\n"
                "app = FastAPI(title=\"Aria Service\", version=\"1.0.0\")\n\n\n"
                "@app.get(\"/health\")\n"
                "def health_check() -> dict:\n"
                "    return {\"status\": \"ok\"}\n"
            ),
        },
    },
    "dashboard": {
        "desc": "Plotly Dash 交互式看板脚手架",
        "dirs": ["assets", "components"],
        "files": {
            "requirements.txt": "dash\nplotly\npandas\n",
            "app.py": (
                "import dash\n"
                "from dash import html\n\n"
                "app = dash.Dash(__name__, title=\"Aria Dashboard\")\n"
                "app.layout = html.Div([html.H1(\"Aria Dashboard\")])\n\n"
                "if __name__ == \"__main__\":\n"
                "    app.run(debug=True, host=\"0.0.0.0\", port=8050)\n"
            ),
        },
    },
}
