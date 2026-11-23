/* Start the bundled interpreter in this GUI process, with Unicode arguments and isolated paths. */
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <shellapi.h>
#include <stdlib.h>
#include <wchar.h>

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR command, int show) {
    (void)instance; (void)previous; (void)command; (void)show;
    int argc;
    wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    wchar_t root[32768];
    DWORD size = GetModuleFileNameW(NULL, root, 32768);
    if (!argv || !size || size >= 32768) return 1;
    wchar_t *slash = wcsrchr(root, L'\\');
    if (!slash || (slash - root) > 32740) return 1;
    wcscpy(slash + 1, L"python312.dll");
    HMODULE python = LoadLibraryExW(root, NULL, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!python) {
        MessageBoxW(NULL, L"无法加载随 Seekino 安装的 Python 运行库。请完整解压后再次打开。", L"Seekino", MB_ICONERROR);
        return 1;
    }
    typedef int (__cdecl *Main)(int, wchar_t **);
    Main entry = (Main)GetProcAddress(python, "Py_Main");
    wchar_t **args = calloc((size_t)argc + 5, sizeof(wchar_t *));
    if (!entry || !args) return 1;
    args[0] = argv[0];
    args[1] = L"-I"; args[2] = L"-B"; args[3] = L"-m"; args[4] = L"desktop.main";
    for (int i = 1; i < argc; ++i) args[i+4] = argv[i];
    int result = entry(argc + 4, args);
    free(args);
    LocalFree(argv);
    if (result != 0 && argc == 1) {
        MessageBoxW(NULL, L"Seekino 未能启动。请将整个 ZIP 解压到同一个文件夹，再打开 Seekino.exe。",
                    L"Seekino", MB_ICONERROR);
    }
    return result;
}
