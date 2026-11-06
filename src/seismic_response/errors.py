"""Exceptions raised by seismic_response."""

__all__ = [
    "SeismicResponseError",
    "ResponseParseError",
    "ResponseValueError",
    "ChannelNotFoundError",
    "UnsupportedFeatureError",
]


class SeismicResponseError(Exception):
    """Base class for all errors raised by this package."""


class ResponseParseError(SeismicResponseError, ValueError):
    """Input (RESP or StationXML) is malformed or uses unsupported content."""


class ResponseValueError(SeismicResponseError, ValueError):
    """A response is internally inconsistent or cannot be evaluated as requested.

    These correspond to the fatal conditions evalresp reports, for example a
    missing decimation blockette on a FIR stage or a zero stage gain.
    """


class ChannelNotFoundError(SeismicResponseError, LookupError):
    """No channel matched the requested identifier and time."""


class UnsupportedFeatureError(SeismicResponseError, NotImplementedError):
    """A feature that evalresp supports but this package does not (yet)."""
