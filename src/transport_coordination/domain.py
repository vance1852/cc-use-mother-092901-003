"""定义基础服务允许登记的资料类别。"""

ALLOWED_CATEGORIES = frozenset({
    "operator_profile",
    "network_registry",
    "transport_resource",
    "operator_assignment",
})


def is_allowed_category(value: str) -> bool:
    return value in ALLOWED_CATEGORIES
