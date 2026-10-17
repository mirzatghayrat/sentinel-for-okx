# Sentinel for OKX development

Product name: 哨兵 Sentinel for OKX. Package and LaunchAgent identifiers stay `okx-desk`, so a new service install replaces an older one instead of running beside it. Not an official OKX product: do not use OKX logos or imply affiliation.

- Keep `data/`, `.env`, access codes and all exchange credentials out of Git, logs and tool outputs. Do not read or copy saved exchange secrets for development.
- Validate order behavior with mocked transports or explicitly authorized OKX demo funds. Do not start the live launcher, enable real-money execution, submit live orders or manage the owner's live account.
- Run `uv run python -m pytest -q` and `node --check static/app.js` after relevant changes.
- The loopback binding, authentication, origin checks, live process opt-in, user confirmation, single-instance lock, durable pending intents and budget/expiry gates are deliberate. Preserve them.
- A submission receipt is not a fill. Never retry uncertain orders with a new client ID or reset the ledger to conceal unresolved fills or losses.
- The LLM strategy only picks buy / sell / hold for a closed bar; every engine gate runs after it. Never put credentials, account identifiers or exchange balances into prompts, and never let model output choose size, pair or mode, or skip a confirmation.
- The tradable universe is BTC-USDT, ETH-USDT and the latest scanner universe, built by code from hard filters. The order client rejects any other instId. A pair that leaves the universe may still be sold, never bought.
- Order writes are spot cash market orders, post-only limit orders (the maker grid) and cancel-by-client-ID. Never add plain limit, conditional, margin or derivative orders. Every grid order is recorded before it is sent; an unconfirmed one pauses the trial and is only looked up again by its client ID. Pause, expiry, restart and errors take grid orders down; the loss line cancels first, then exits at market.
- The scanner reads public market data only and must stay paced so it does not starve trading requests. Its score orders candidates; it is not a forecast. Do not present rankings as expected returns.
- Test LLM paths with mocked transports. Do not call OpenRouter or TypeSafe with the owner's keys.
- Historical results are short-sample research. Do not claim stable profits or label the default strategies as validated.
- The MCP server (`lab/mcp_server.py`) is read-only. It opens the journal in SQLite read-only mode and never touches credential files, the access code or exchange balances. Any tool that would change state belongs to a separate, reviewed proposal-and-approval design; never add one here.
