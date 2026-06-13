# Broker API Research — v4.7.30 push-to-broker build

Multi-agent fleet research output that drove the order-execution design.
See operating_manual.md "Pushing trades to your broker" section for user-facing docs.

## Tradier
- Base URLs: production `https://api.tradier.com/v1`, sandbox `https://sandbox.tradier.com/v1`
- Account lookup: `GET /v1/user/profile` → pick account_number with option_level>=3
- Multi-leg order: `POST /v1/accounts/{account_id}/orders` form-urlencoded
- All spreads use `class=multileg` (not `combo`, not `otoco`)
- Type: `credit` for credit spreads/condors/iron flies; `debit` for debit verticals + long flies; `market` for at-market
- Preview before submit: add `preview=true` as a form field (not query)
- OCC symbol format: `{ROOT}{YYMMDD}{C|P}{strike*1000, 8-digit zero-pad}`  e.g. `SPY260620C00450000`
- Rate limits: 60/min for trading bucket (both env)
- Poll order status at `GET /v1/accounts/{id}/orders/{order_id}` after submit

## WeBull
- Official OpenAPI exists (https://developer.webull.com/apis/docs/) but auth uses HMAC signing
- Signing requires app_key + app_secret — cannot expose secret in browser
- Requires WeBull app approval (1-3 business days)
- Verdict: STUB for v1 with clear "coming soon" labeling; needs a small signing proxy server before browser-only integration possible

