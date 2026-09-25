"""稳定的错误码与统一错误响应体。

所有错误响应均为:
    {"error": {"code": <稳定代码>, "message": <描述>, "details": [...]}}
"""


class ApiError(Exception):
    """业务错误，由全局异常处理器转换为统一错误响应。"""

    def __init__(self, status_code, code, message, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or []


def error_body(code, message, details=None):
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or [],
        }
    }
