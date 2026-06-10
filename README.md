# Narrative Memory Agent

## Railway persistence

The agent stores its SQLite database, state, and strategy configuration under
`DATA_DIR`, and writes the activity log under `LOG_DIR`.

For durable Railway deployments, mount a volume and set:

```text
DATA_DIR=/data
LOG_DIR=/data/logs
```

Without a persistent volume, Railway may remove trade history and narrative
memory when the service is redeployed.
