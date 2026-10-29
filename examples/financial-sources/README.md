# Financial-source recovery

The financial statement tool first tries Yahoo, then SEC EDGAR when Yahoo returns no statements. Yahoo keeps its 20-second request budget; the SEC fallback has a separate 10-second budget. These are staged budgets, not a promise that the complete lookup always finishes within 20 seconds. Network-disabled host configuration blocks both.

SEC exposes public JSON APIs without an API key. Declare your application/company and a real contact email using `EDGAR_USER_AGENT`; do not use a fabricated address or an example placeholder. Company is Arthera; the contact email is currently not configured. Nothing in this integration registers an email account or sends mail. An initial local SEC request returned HTTP 403; a successful live fallback has not yet been verified.

```sh
python3 examples/financial-sources/verify_financial_sources.py
# After you have a contact email, set EDGAR_USER_AGENT securely in your environment.
python3 examples/financial-sources/verify_financial_sources.py --live --symbol AAPL
```

The first command checks configuration presence only. The second makes actual network requests and returns nonzero if no provider succeeds. It reports attempts and provenance, never the contact header. Cached responses expire; expired data is not silently returned after an outage. Requests are paced at no more than two per second in one process; ensure total deployment traffic stays within the SEC's fair-access limit.

The strict parser supports annual US-GAAP 10-K/10-K/A USD values. Core revenue, net income, assets and stockholders' equity must come from one accession and matching dates. Quarterly, YTD, other-currency or conflicting facts are not combined. Optional values stay missing. Results include period, accession, filing/retrieval dates, source URL and a stale-period flag. Annual financial statements are not real-time market quotes; a successful lookup is not a solvency opinion.

Offline tests use explicitly synthetic facts and verify recovery logic, not live SEC availability:

```sh
python -m pytest -q tests/test_sec_statement_fallback.py tests/test_financial_statement_recovery.py
```

Official references: [SEC API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces), [declared user agents](https://www.sec.gov/about/webmaster-frequently-asked-questions), [fair access](https://www.sec.gov/about/developer-resources).
