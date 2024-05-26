from skyvern.forge.sdk.core.permissions.permission_checkers import NoopPermissionChecker, PermissionChecker


class PermissionCheckerFactory:
    __instance: PermissionChecker = NoopPermissionChecker()

    @staticmethod
    def get_instance() -> PermissionChecker:
        return PermissionCheckerFactory.__instance

    @staticmethod
    def set_instance(permission_checker: PermissionChecker) -> None:
        PermissionCheckerFactory.__instance = permission_checker
