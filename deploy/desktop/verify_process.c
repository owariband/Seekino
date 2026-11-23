/* Observe the native host from before its first instruction until exit. */
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/event.h>
#include <sys/resource.h>
#include <time.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

int main(int argc, char **argv) {
    if (argc < 2) return 1;
    int kq = kqueue();
    if (kq < 0) { perror("kqueue"); return 1; }
    posix_spawnattr_t attr;
    if (posix_spawnattr_init(&attr) ||
        posix_spawnattr_setflags(&attr, POSIX_SPAWN_START_SUSPENDED)) return 1;
    pid_t child;
    int error = posix_spawn(&child, argv[1], NULL, &attr, &argv[1], environ);
    posix_spawnattr_destroy(&attr);
    if (error) { errno = error; perror("posix_spawn"); return 1; }

    struct kevent request, result;
    EV_SET(&request, child, EVFILT_PROC, EV_ADD | EV_CLEAR, NOTE_FORK | NOTE_EXEC | NOTE_EXIT, 0, NULL);
    struct timespec start, end;
    clock_gettime(CLOCK_MONOTONIC, &start);
    if (kevent(kq, &request, 1, NULL, 0, NULL) < 0 || kill(child, SIGCONT) < 0) {
        perror("register host observer");
        kill(child, SIGKILL);
        waitpid(child, NULL, 0);
        return 1;
    }
    int spawned = 0;
    for (;;) {
        if (kevent(kq, NULL, 0, &result, 1, NULL) < 0) {
            if (errno == EINTR) continue;
            perror("kevent");
            kill(child, SIGKILL);
            waitpid(child, NULL, 0);
            return 1;
        }
        if (result.fflags & (NOTE_FORK | NOTE_EXEC)) spawned = 1;
        if (result.fflags & NOTE_EXIT) break;
    }
    int status;
    struct rusage usage;
    if (wait4(child, &status, 0, &usage) < 0) return 1;
    clock_gettime(CLOCK_MONOTONIC, &end);
    double seconds = end.tv_sec - start.tv_sec + (end.tv_nsec - start.tv_nsec) / 1e9;
    fprintf(stderr, "host elapsed=%.3fs peak_rss=%ld bytes\n", seconds, usage.ru_maxrss);
    fprintf(stderr, "pid=%d macOS observed fork or exec: %s\n", child, spawned ? "yes" : "no");
    if (spawned) return 86;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
