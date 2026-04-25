import base64
import json
import uuid
from dataclasses import dataclass, asdict
from typing import Dict, Optional


def b64_encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64_decode(raw: str) -> bytes:
    return base64.b64decode(raw.encode("ascii"))


def new_request_id() -> str:
    return uuid.uuid4().hex


@dataclass
class HttpRequestMessage:
    request_id: str
    method: str
    target_host: str
    target_port: int
    path: str
    headers: Dict[str, str]
    body_b64: str
    timeout_seconds: int = 30

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(payload: str) -> "HttpRequestMessage":
        data = json.loads(payload)
        return HttpRequestMessage(**data)

    @property
    def body(self) -> bytes:
        return b64_decode(self.body_b64) if self.body_b64 else b""


@dataclass
class HttpResponseMessage:
    request_id: str
    status_code: int
    reason: str
    headers: Dict[str, str]
    body_b64: str
    error: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(payload: str) -> "HttpResponseMessage":
        data = json.loads(payload)
        return HttpResponseMessage(**data)

    @property
    def body(self) -> bytes:
        return b64_decode(self.body_b64) if self.body_b64 else b""


@dataclass
class TunnelOpenMessage:
    tunnel_id: str
    target_host: str
    target_port: int

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(payload: str) -> "TunnelOpenMessage":
        data = json.loads(payload)
        return TunnelOpenMessage(**data)


@dataclass
class TunnelDataMessage:
    tunnel_id: str
    data_b64: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(payload: str) -> "TunnelDataMessage":
        data = json.loads(payload)
        return TunnelDataMessage(**data)

    @property
    def data(self) -> bytes:
        return b64_decode(self.data_b64) if self.data_b64 else b""


@dataclass
class TunnelCloseMessage:
    tunnel_id: str
    reason: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(payload: str) -> "TunnelCloseMessage":
        data = json.loads(payload)
        return TunnelCloseMessage(**data)
