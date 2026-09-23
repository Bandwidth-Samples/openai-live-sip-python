import re
from typing import List, Optional

from pydantic import BaseModel, Field


class SipHeader(BaseModel):
    """A single SIP header name/value pair."""

    name: str
    value: str


class LiveTransportData(BaseModel):
    """Payload inside a live.transport.incoming webhook event."""

    model_config = {"populate_by_name": True, "extra": "ignore"}

    type: str
    session_id: str
    from_uri: Optional[str] = Field(None, alias="from")
    to_uri: Optional[str] = Field(None, alias="to")
    sip_headers: List[SipHeader] = []

    def get_sip_host(self) -> Optional[str]:
        """Extract IP:port from the Contact SIP header."""
        for header in self.sip_headers:
            if header.name.lower() == "contact":
                match = re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{1,5}\b", header.value)
                if match:
                    return match.group(0)
        return None


class LiveTransportIncoming(BaseModel):
    """Top-level Live transport incoming webhook event."""

    model_config = {"extra": "ignore"}

    type: str
    data: LiveTransportData
