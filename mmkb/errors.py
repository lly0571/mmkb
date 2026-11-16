"""Errors shared by resource services and HTTP adapters."""


class ResourceError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class StorageError(ResourceError):
    def __init__(self, message: str = "Storage unavailable; check server configuration"):
        super().__init__("storage_unavailable", message, 503)


class InvalidKeyError(StorageError):
    def __init__(self, message: str = "Storage key must be a relative path inside the storage root"):
        ResourceError.__init__(self, "invalid_key", message, 422)


class StorageConfigError(StorageError):
    def __init__(self, message: str):
        ResourceError.__init__(self, "storage_config", message, 503)


class ObjectNotFound(StorageError):
    def __init__(self):
        ResourceError.__init__(self, "original_not_found", "Original file not found", 404)


# Keep the original API error name available to existing resource callers.
ObjectNotFoundError = ObjectNotFound
