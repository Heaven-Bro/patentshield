"""Live patent-data provider adapters and canonical ingestion services."""

from patents.connectors.epo import EPOOPSConnector
from patents.connectors.uspto import USPTOODPConnector

__all__ = ["EPOOPSConnector", "USPTOODPConnector"]
