"""Per-user model credentials in the two supported desktop credential stores."""
import ctypes as c
import sys
from ctypes import wintypes

SERVICE = "app.seekino.desktop.models"
KEYS = ("ARK_API_KEY", "DEEPSEEK_API_KEY")


class MacKeychain:
    def __init__(self):
        self.security = c.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.foundation = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.foundation.CFRelease.argtypes = [c.c_void_p]
        text = [c.c_uint32, c.c_char_p]
        self.security.SecKeychainFindGenericPassword.argtypes = [
            c.c_void_p, *text, *text, c.POINTER(c.c_uint32), c.POINTER(c.c_void_p), c.POINTER(c.c_void_p),
        ]
        self.security.SecKeychainItemFreeContent.argtypes = [c.c_void_p, c.c_void_p]
        self.security.SecKeychainAddGenericPassword.argtypes = [
            c.c_void_p, *text, *text, *text, c.POINTER(c.c_void_p),
        ]
        self.security.SecKeychainItemModifyAttributesAndData.argtypes = [c.c_void_p, c.c_void_p, *text]

    @staticmethod
    def _query(name):
        account, service = name.encode(), SERVICE.encode()
        return (None, len(service), service, len(account), account)

    def read(self, name):
        size, data = c.c_uint32(), c.c_void_p()
        code = self.security.SecKeychainFindGenericPassword(
            *self._query(name), c.byref(size), c.byref(data), None,
        )
        if code == -25300:  # errSecItemNotFound
            return ""
        if code:
            raise RuntimeError(f"读取钥匙串失败（{code}）")
        try:
            return c.string_at(data, size.value).decode()
        finally:
            self.security.SecKeychainItemFreeContent(None, data)

    def write(self, name, value):
        raw = value.encode()
        item = c.c_void_p()
        query = self._query(name)
        code = self.security.SecKeychainFindGenericPassword(*query, None, None, c.byref(item))
        if code == -25300:
            code = self.security.SecKeychainAddGenericPassword(*query, len(raw), raw, None)
        elif code == 0:
            try:
                code = self.security.SecKeychainItemModifyAttributesAndData(item, None, len(raw), raw)
            finally:
                self.foundation.CFRelease(item)
        if code:
            raise RuntimeError(f"写入钥匙串失败（{code}）")


class _Credential(c.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", c.c_void_p),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", c.c_void_p),
        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentials:
    def __init__(self):
        self.api = c.WinDLL("advapi32", use_last_error=True)
        self.api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       c.POINTER(c.POINTER(_Credential))]
        self.api.CredWriteW.argtypes = [c.POINTER(_Credential), wintypes.DWORD]
        self.api.CredFree.argtypes = [c.c_void_p]

    def read(self, name):
        record = c.POINTER(_Credential)()
        if not self.api.CredReadW(f"{SERVICE}/{name}", 1, 0, c.byref(record)):
            code = c.get_last_error()
            if code == 1168:  # ERROR_NOT_FOUND
                return ""
            raise c.WinError(code)
        try:
            return c.string_at(record.contents.CredentialBlob, record.contents.CredentialBlobSize).decode("utf-16-le")
        finally:
            self.api.CredFree(record)

    def write(self, name, value):
        raw = value.encode("utf-16-le")
        buffer = c.create_string_buffer(raw)
        record = _Credential(Type=1, TargetName=f"{SERVICE}/{name}", CredentialBlobSize=len(raw),
                             CredentialBlob=c.cast(buffer, c.c_void_p), Persist=2, UserName=name)
        if not self.api.CredWriteW(c.byref(record), 0):
            raise c.WinError(c.get_last_error())


def platform_store():
    if sys.platform == "darwin":
        return MacKeychain()
    if sys.platform == "win32":
        return WindowsCredentials()
    raise RuntimeError("当前系统没有可用的桌面凭据存储")
