Run the transport regression tests from the repository root:

```sh
uv run --python 3.13 --with-requirements tests/requirements.txt pytest -q
```

The tests stub Home Assistant's device lookup and task creation while loading
this integration's transport code. They do not start Home Assistant or contact
hardware. They exercise stale GATT discovery, recovery through the active
backend, failure and cancellation cleanup, command replay boundaries, response
readiness, pairing policy, and connection reuse.
