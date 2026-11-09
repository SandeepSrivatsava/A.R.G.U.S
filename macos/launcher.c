/*
 * Argus.app's main executable. It starts the Argus voice app (Python) as a child process so that
 * macOS attributes microphone access to "Argus" (this app bundle) and shows our usage text,
 * forwards stop signals to it, and exits with its exit code (so launchd restarts it only on a crash).
 *
 * Build: clang -O2 -Wall -o Argus.app/Contents/MacOS/Argus launcher.c
 */
#include <errno.h>
#include <limits.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static volatile pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

int main(int argc, char **argv) {
    const char *home = getenv("HOME");
    const char *override = getenv("ARGUS_PYTHON");  /* tests only */
    char python[PATH_MAX];
    if (override && *override) {
        snprintf(python, sizeof python, "%s", override);
    } else if (home && *home) {
        snprintf(python, sizeof python, "%s/.argus/venv/bin/python3", home);
    } else {
        fprintf(stderr, "argus: HOME is not set\n");
        return 1;
    }
    char *args[] = {python, "-I", "-m", "argus", "run", NULL};
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = forward;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);
    pid_t pid;
    int rc = posix_spawn(&pid, python, NULL, NULL, args, environ);
    if (rc != 0) {
        fprintf(stderr, "argus: cannot start %s: %s (run ./setup.sh again)\n", python, strerror(rc));
        return 1;
    }
    child = pid;
    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) return 1;
    }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status) && (WTERMSIG(status) == SIGTERM || WTERMSIG(status) == SIGINT)) return 0;
    return 1;
}
