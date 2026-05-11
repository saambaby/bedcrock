# Alpaca cassettes

The unit tests in `tests/broker/test_alpaca_adapter.py` use `httpx.MockTransport`
with hand-built response payloads, so no recorded fixtures are required to run
the CI suite. The folder is reserved for future `vcrpy` cassette-based tests
that exercise the adapter against a real Alpaca paper account end-to-end.

## Getting Alpaca paper keys

1. Sign up at <https://alpaca.markets> (free).
2. From the dashboard, switch to **Paper Trading** in the top-right toggle.
3. Generate an API key pair. Copy the key id and secret — the secret is shown
   only once.

## Recording a cassette

Set the env vars and run the broker test suite with vcr in record-once mode:

```sh
export BROKER=alpaca
export MODE=paper
export ALPACA_API_KEY=...           # paper key id
export ALPACA_API_SECRET=...        # paper secret
pytest tests/broker/ --record-mode=once
```

On replay (the default in CI), the cassettes are read from this directory and
no network traffic is generated.

## Header redaction

To make sure keys never land in committed YAML, the cassette fixture must
register the following filters with `vcrpy`:

```python
vcr_config = {
    "filter_headers": [
        ("APCA-API-KEY-ID", "REDACTED"),
        ("APCA-API-SECRET-KEY", "REDACTED"),
        ("Authorization", "REDACTED"),
    ],
    "filter_query_parameters": [("api_key", "REDACTED")],
    "decode_compressed_response": True,
}
```

After recording, grep the cassette files for the literal key prefix
(`PK...`) and confirm they're absent before committing.
