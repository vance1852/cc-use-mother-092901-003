"""领域服务使用的业务异常。"""


class DomainError(Exception):
    """所有可预期业务异常的基类。"""

    code = "domain_error"
    status = 400


class ValidationError(DomainError):
    """输入字段不符合业务约束。"""

    code = "validation_error"


class NotFoundError(DomainError):
    """请求引用的业务对象不存在。"""

    code = "not_found"
    status = 404


class PermissionDenied(DomainError):
    """操作者没有执行当前动作的权限。"""

    code = "permission_denied"
    status = 403


class ConflictError(DomainError):
    """请求编号或业务唯一键与既有内容冲突。"""

    code = "conflict"
    status = 409


class AccountClosedError(DomainError):
    """收入已经关账，迟到事件或调整不能直接改写。"""

    code = "account_closed"
    status = 409


class BillingStateError(DomainError):
    """行程当前状态不允许该操作，例如尚未生成计费事实。"""

    code = "billing_state"
    status = 409
