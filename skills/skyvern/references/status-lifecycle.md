# Run Status Lifecycle

Typical flow:

1. `created`
2. `queued`
3. `running`
4. terminal status: `completed`, `failed`, `canceled`, `terminated`, or `timed_out`

Additional states:

- `paused` — non-terminal; the run is suspended and can be resumed.

Operational guidance:

- Define max runtime per workflow class.
- Alert on runs stuck in non-terminal states beyond threshold.
- Track failure signatures for prioritization.
