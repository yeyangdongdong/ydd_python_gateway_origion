"""HTTP 和 WebSocket 共用的业务错误。"""


class GatewayError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    def as_dict(self) -> dict:
        """错误对象本身只带 code 和 message，其余信息放在事件/响应体的外层。"""
        return {"code": self.code, "message": self.message}

    def as_body(self) -> dict:
        """HTTP 错误响应的统一外层：{"error": {"code", "message"}}。"""
        return {"error": self.as_dict()}
