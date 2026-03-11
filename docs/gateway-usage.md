# SmileClaw Gateway Usage

## 1. Config
Use [gateway.yaml](/Users/coderchan/Desktop/github/SmileClaw/docs/gateway.yaml) as baseline.

Telegram adapter uses long polling (`getUpdates`) by default.
Optional field in `channels.telegram`:
- `poll_timeout` (seconds, default: `20`)

## 2. Start Gateway
```bash
python3 src/gateway_cli.py --config docs/gateway.yaml gateway start
```

## 3. Pairing Operations
```bash
python3 src/gateway_cli.py --config docs/gateway.yaml pairing list
python3 src/gateway_cli.py --config docs/gateway.yaml pairing approve <id>
python3 src/gateway_cli.py --config docs/gateway.yaml pairing reject <id>
python3 src/gateway_cli.py --config docs/gateway.yaml pairing revoke <principal>
```

## 4. Approval Operations
```bash
python3 src/gateway_cli.py --config docs/gateway.yaml approval list
python3 src/gateway_cli.py --config docs/gateway.yaml approval approve <approval_id>
python3 src/gateway_cli.py --config docs/gateway.yaml approval reject <approval_id>
```

## 5. Run Introspection
```bash
python3 src/gateway_cli.py --config docs/gateway.yaml runs list
python3 src/gateway_cli.py --config docs/gateway.yaml runs get <run_id>
```

## 6. Storage
SQLite DB path:
`<workspace>/.smileclaw/gateway.db`

Tables:
- `processed_events`
- `pairing_requests`
- `approved_principals`
- `approval_queue`
- `run_state`

## 7. Audit Logs
Audit file:
`<workspace>/.smileclaw/audit/events.jsonl`

Redaction includes:
- tokens/secrets
- webhook signatures
- attachment URLs
- email/phone patterns
