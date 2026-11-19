# Change Log

## Unreleased (2026-11-18)

- Raise package exceptions for invalid arguments and ambiguous channel selection, and add AmbiguousChannelError
- Reject negative Content-Length and time out stalled request bodies in the GUI server
- Return an error from the GUI API for malformed requests and out-of-range channel ids
- Report an unusable GUI server port as an error instead of a traceback

## v0.1.0

- Initial version
  - pure python package for calculating responses programmatically
  - evalresp compatibility with considered deviations
  - Web-compatible (and desktop friendly) app for visualizing responses,
    loading responses from local files, URLs, and service fetch form
