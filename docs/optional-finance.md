# Optional finance installation

The Python base package now supports coding/chat without requiring NumPy, pandas,
SciPy, pandas-ta or yfinance. Financial source code remains available; install its
dependencies when you need local market data and quantitative analysis:

```sh
python3 -m pip install "aria-code[finance]<4"
# China data plus the numerical/global-data stack:
python3 -m pip install "aria-code[finance,cn]<4"
```

For a source checkout, use `python3 -m pip install -e ".[finance]"`.
The source installer retains its existing default full installation;
`bash install.sh --core` selects coding/chat without finance. The repository's
`./aria-code` launcher installs the declared base package rather than forcing
yfinance separately. Existing environments retain previously installed packages;
this change does not uninstall dependencies from a user's environment.

`finance`, `full`, `all` and `dev` explicitly include the five dependencies.
Feature extras retain their own needed dependencies: files/data/crypto/charts/cn
explicitly retain pandas (and its NumPy dependency), backtest retains yfinance,
and sports retains NumPy. Installing these profiles can therefore add parts of
the numerical stack without installing all of finance.

The developer profile keeps them because the existing regression suite includes
financial algorithms. Native/npm build workflows and the local frozen-build script
explicitly request finance, preserving their existing feature set. This change
does not shrink the published native archive or switch the default Python worker.
`requirements.txt` remains the full deployment environment.

`/doctor` treats missing pandas, NumPy and yfinance as optional and suggests the
finance extra. Calling local market tools without their required numerical
packages fails with an installation hint; no estimated market data is substituted.
SciPy and pandas-ta retain their existing optional fallbacks where implemented.
The supported Python range remains 3.10–3.13, including full/finance dependencies;
removing a base dependency does not establish Python 3.14 compatibility.

The unused root `core/application.py` prototype was removed: no in-repository
callers or packaging entrypoints referred to it, its bootstrap operations were
placeholders, and its intent handler returned completion without doing work.
The active `packages/aria_core` package is retained. Compatibility import shims,
the production Python relay, and the Python application worker are also retained.

The next migration boundary is native task persistence: task briefs, scoped
access and current-content acceptance evidence need a shared protocol and actual
Go entrypoint integration before selecting Go by default. This dependency change
does not establish that parity or authorize a production cutover.
