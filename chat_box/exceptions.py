class RoomError(Exception):
    def __init__(self, message, *, code="invalid_room_operation", status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class RoomPermissionDenied(RoomError):
    def __init__(self, message, *, code="permission_denied"):
        super().__init__(message, code=code, status=403)


class RoomNotFound(RoomError):
    def __init__(self, message, *, code="room_not_found"):
        super().__init__(message, code=code, status=404)
