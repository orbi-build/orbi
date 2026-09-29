# elasticsearch exporter: retries share one `timeout` deadline, so data is dropped while Elasticsearch is down

## Problem

The Elasticsearch exporter (`exporter/elasticsearchexporter`) documents `timeout` as the HTTP
request timeout (default 90s) and supports `retry` (`enabled`, `max_retries`,
`initial_interval`, `max_interval`, `retry_on_status`). In practice the `timeout` budget is
shared by *all* attempts of one bulk flush: once the first attempts and the backoff waits between
them have used up `timeout`, the flush is aborted with `context deadline exceeded` and the
remaining retries never happen.

With a configuration like

```yaml
exporters:
  elasticsearch:
    endpoint: http://localhost:9200
    timeout: 30s
    retry:
      enabled: true
      initial_interval: 1s
      max_interval: 30s
      max_retries: 0      # unlimited
      retry_on_status: [429, 500, 502, 503, 504]
    sending_queue:
      enabled: true
      storage: file_storage
```

stopping Elasticsearch (or having it answer `429` for a while) makes the collector log
`bulk indexer flush error ... context deadline exceeded` / `Exporting failed. Dropping data.`
long before the retry policy is exhausted, instead of continuing to retry until Elasticsearch is
back.

Minimal reproduction with a test server: set `timeout: 50ms`, enable retries with
`initial_interval: 100ms` / `max_interval: 100ms` and a large `max_retries`, and let the bulk
endpoint return `429` for the first two requests and a final response on the third. Today the
exporter makes a single request and fails with `context deadline exceeded` after ~50ms; the
server never sees the 2nd and 3rd attempts.

## Expected behavior

- `timeout` limits each individual HTTP request to Elasticsearch, as documented; it is not a
  budget for the whole flush, and every retry attempt gets its own full `timeout`.
- The number of attempts and the backoff between them are governed by the `retry` settings only.
- Update the README note on `timeout` if needed.

## Acceptance

- Fix the bug.
- Add tests.
- The module's tests pass (with `GOTOOLCHAIN=go1.26.6`):
  `cd exporter/elasticsearchexporter && go test ./... && go vet ./...`
