/* Exercise the embedded backend without a web view, HTTP server or child Python. */
#include <Python.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void check(PyStatus status) {
    if (PyStatus_Exception(status)) Py_ExitStatusException(status);
}

static void add_path(PyConfig *config, const char *resources, const char *suffix) {
    char path[PATH_MAX];
    if (snprintf(path, sizeof(path), "%s/%s", resources, suffix) >= (int)sizeof(path)) exit(1);
    wchar_t *wide = Py_DecodeLocale(path, NULL);
    if (!wide) exit(1);
    check(PyWideStringList_Append(&config->module_search_paths, wide));
    PyMem_RawFree(wide);
}

#ifndef SEEKINO_ENTRY
#define SEEKINO_ENTRY L"server.desktop.host"
#endif

int main(int argc, char **argv) {
    char executable[PATH_MAX], resolved[PATH_MAX], resources[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) || !realpath(executable, resolved)) return 1;
    char *filename = strrchr(resolved, '/');
    if (!filename) return 1;
    *filename = 0;
    if (snprintf(resources, sizeof(resources), "%s/../Resources", resolved) >= (int)sizeof(resources)) return 1;
    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    config.parse_argv = 0;
    config.write_bytecode = 0;
    config.site_import = 0;
    config.module_search_paths_set = 1;
    add_path(&config, resources, "");
    add_path(&config, resources, "python/lib/python3.12");
    add_path(&config, resources, "python/lib/python3.12/lib-dynload");
    add_path(&config, resources, "site-packages");
    char home[PATH_MAX];
    if (snprintf(home, sizeof(home), "%s/python", resources) >= (int)sizeof(home)) return 1;
    check(PyConfig_SetBytesString(&config, &config.home, home));
    check(PyConfig_SetBytesArgv(&config, argc, argv));
    check(PyConfig_SetString(&config, &config.run_module, SEEKINO_ENTRY));
    check(Py_InitializeFromConfig(&config));
    PyConfig_Clear(&config);
    return Py_RunMain();
}
