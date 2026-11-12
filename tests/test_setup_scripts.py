"""The setup scripts: setup.sh asks iPhone or Android first and sets everything up for that phone (and hands over to
the Android installer inside Termux); the Android and server installers; the shared shell helpers."""
import os
import pty
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def venv(tmp_path_factory):
    path = tmp_path_factory.mktemp("venv")
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(path)], check=True)
    return path


def mac_env(tmp_path, venv, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ARGUS_", "TERMUX"))}
    env.pop("PREFIX", None)
    env.update(ARGUS_HOME=str(tmp_path / "home"), ARGUS_ANTHROPIC_API_KEY="sk-ant-test", ARGUS_NAME="Sandeep",
               ARGUS_CITY="Portland, OR", ARGUS_TIMEZONE="America/Los_Angeles", ARGUS_BLOCKED_SITES="",
               ARGUS_AI_BRIEFING="no", ARGUS_SKIP_VALIDATION="1", ARGUS_SEND_HELLO="no", ARGUS_ALLOW_LINUX="1",
               ARGUS_SKIP_DEPS="1", ARGUS_SKIP_MODELS="1", ARGUS_VENV=str(venv), ARGUS_BIN_DIR=str(tmp_path / "bin"),
               ARGUS_WORKSPACE=str(tmp_path / "workspace"))
    env.update(extra)
    return env


def run(script, env, *args, stdin=subprocess.DEVNULL, timeout=120):
    return subprocess.run(["bash", str(REPO / script), *args], env=env, stdin=stdin, capture_output=True, text=True,
                          timeout=timeout)


def setting(path, section, key):
    out = subprocess.run(["bash", "-c", f'. "{REPO}/scripts/lib.sh"; toml_get "$1" "$2" "$3"', "_", str(path),
                          section, key], capture_output=True, text=True, check=True)
    return out.stdout.strip()


# ---- the shared helpers ---------------------------------------------------------------------------------------------
def test_toml_helpers(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('name = "Argus"\ncity = "Portland, OR"  # for weather\n\n[phone]\nenabled = true    # gateway\n'
                   'port = 8765\n\n[calls]\nenabled = false\n')

    def sh(code):
        return subprocess.run(["bash", "-c", f'. "{REPO}/scripts/lib.sh"; {code}', "_", str(cfg)], capture_output=True,
                              text=True, check=True).stdout.strip()
    assert sh('toml_get "$1" "" city') == "Portland, OR" and sh('toml_get "$1" phone enabled') == "true"
    assert sh('toml_get "$1" phone type') == "" and sh('toml_get "$1" calls enabled') == "false"
    sh('toml_set "$1" phone type \'"android"\'; toml_set "$1" calls enabled true; toml_set "$1" maps app \'"google"\'')
    text = cfg.read_text()
    assert '[phone]\ntype = "android"\nenabled = true    # gateway' in text and "[calls]\nenabled = true" in text
    assert text.endswith('\n[maps]\napp = "google"\n') and oct(cfg.stat().st_mode & 0o777) == "0o600"
    sh('toml_set "$1" phone type \'"iphone"\'')
    assert cfg.read_text().count("type = ") == 1 and sh('toml_get "$1" phone type') == "iphone"
    for answer, phone in (("1", "iphone"), ("iPhone", "iphone"), (" IOS ", "iphone"), ("2", "android"),
                          ("Samsung", "android"), ("Pixel", "android"), ("3", ""), ("", "")):
        assert sh(f'normalize_phone "{answer}"') == phone, answer


# ---- setup.sh: the phone question ------------------------------------------------------------------------------------
def test_setup_for_android_then_iphone(tmp_path, venv):
    env = mac_env(tmp_path, venv, ARGUS_PHONE="android", ARGUS_GOOGLE_MAPS_KEY="AIza" + "x" * 35)
    first = run("setup.sh", env)
    assert first.returncode == 0, first.stdout + first.stderr
    out = first.stdout
    assert "1/10  Your phone" in out and "Android: Google Maps links" in out and "10/10  Saying hello" in out
    assert "Next, the Android phone" in out and "Calls from your own number need an iPhone" in out
    assert "docs/IPHONE.md" not in out and "Warning" not in first.stderr
    cfg = tmp_path / "home" / "config.toml"
    assert setting(cfg, "phone", "type") == "android" and setting(cfg, "maps", "app") == "google"
    assert setting(cfg, "glasses", "enabled") == "false" and setting(cfg, "calls", "enabled") == "false"
    secrets = (tmp_path / "home" / "secrets.env").read_text()
    assert "GOOGLE_MAPS_API_KEY=AIza" in secrets and "AIza" not in cfg.read_text()
    env.update(ARGUS_PHONE="iphone")
    env.pop("ARGUS_GOOGLE_MAPS_KEY")
    second = run("setup.sh", env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "Kept your settings" in second.stdout and "Switched the phone settings to iphone (maps: apple)" in second.stdout
    assert "Google Maps key: found" in second.stdout and "scripts/setup_calls.sh" in second.stdout
    assert setting(cfg, "phone", "type") == "iphone" and setting(cfg, "maps", "app") == "apple"
    assert setting(cfg, "glasses", "enabled") == "true"
    assert (tmp_path / "home" / "secrets.env").read_text().count("GOOGLE_MAPS_API_KEY=") == 1
    third = run("setup.sh", dict(env, ARGUS_PHONE=""))                 # no answer and no terminal: keep iPhone
    assert third.returncode == 0 and "Switched" not in third.stdout       # no answer and no terminal: keep iPhone
    assert "iPhone: Apple Maps, Siri Shortcuts" in third.stdout and "(saved)" in third.stdout
    loaded = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                             "from argus import config; s = config.load(); print(s.phone_type, s.maps_link_app)",
                             str(REPO)], env=env, capture_output=True, text=True)
    assert loaded.stdout.strip() == "iphone apple"


def test_setup_refuses_a_bad_phone_and_explains_linux(tmp_path, venv):
    bad = run("setup.sh", mac_env(tmp_path, venv), "--phone", "nokia")
    assert bad.returncode == 1 and "--phone must be iphone or android" in bad.stderr
    assert run("setup.sh", mac_env(tmp_path, venv), "--phone").returncode == 2
    linux = run("setup.sh", dict(mac_env(tmp_path, venv), ARGUS_ALLOW_LINUX="0"), "--phone=iphone")
    assert linux.returncode == 1 and "scripts/setup_server.sh" in linux.stderr
    if "microsoft" not in Path("/proc/version").read_text().lower():
        assert "This is a Linux computer. ./setup.sh is for a Mac" in linux.stderr
    helped = run("setup.sh", mac_env(tmp_path, venv), "--help")
    assert helped.returncode == 0 and "--phone iphone|android" in helped.stdout and "asks first" in helped.stdout
    dry = run("setup.sh", mac_env(tmp_path, venv, ARGUS_PHONE="2"), "--dry-run")
    assert dry.returncode == 0 and "[dry-run] write" in dry.stdout and not (tmp_path / "home" / "config.toml").exists()


@pytest.mark.parametrize("system,expected", [
    ("MINGW64_NT-10.0-22631", "This is Windows. Argus runs on a Mac"),
    ("MSYS_NT-10.0", "This is Windows. Argus runs on a Mac"),
    ("CYGWIN_NT-10.0", "an Android phone on its own"),
    ("FreeBSD", "Unsupported system: FreeBSD"),
])
def test_setup_explains_computers_it_cannot_run_on(tmp_path, venv, system, expected):
    """Setup never asks which computer this is: it looks, and says what to do on Windows and others."""
    out = run("setup.sh", dict(mac_env(tmp_path, venv), ARGUS_TEST_SYSTEM=system), "--phone=iphone")
    assert out.returncode == 1 and expected in out.stderr
    assert not (tmp_path / "home" / "config.toml").exists()


def test_setup_says_the_mac_is_set_and_the_question_is_about_the_phone(tmp_path, venv):
    out = run("setup.sh", dict(mac_env(tmp_path, venv), ARGUS_TEST_SYSTEM="Darwin"), "--phone=2", "--dry-run")
    # on Linux the Mac checks after the phone question stop the run; the explanation comes before them
    assert "This computer: a Mac (macOS). Argus will run on it" in out.stdout
    assert "The next question is only about your phone." in out.stdout
    assert "1/10  Your phone (Argus runs on this Mac; the phone is how you reach it when away)" in out.stdout


def test_windows_gets_an_explanation_too():
    text = (REPO / "setup.cmd").read_bytes().decode()
    assert text.startswith("@echo off\r\n") and "runs on a Mac" in text and "docs\\ANDROID.md" in text
    assert text.rstrip().endswith("pause")


def test_setup_asks_which_phone_on_a_terminal(tmp_path, venv):
    env = mac_env(tmp_path, venv)
    master, slave = pty.openpty()
    proc = subprocess.Popen(["bash", str(REPO / "setup.sh")], env=env, stdin=slave, stdout=slave, stderr=slave,
                            close_fds=True)
    os.close(slave)
    seen = b""

    def wait_for_text(text: bytes, timeout: float = 30) -> None:
        nonlocal seen
        deadline = time.time() + timeout
        while text not in seen:
            assert time.time() < deadline, f"never saw {text!r}; got {seen[-500:]!r}"
            ready, _, _ = select.select([master], [], [], 0.5)
            if ready:
                try:
                    seen += os.read(master, 4096)
                except OSError:
                    break
    try:
        wait_for_text(b"Type 1 or 2")
        assert b"1) iPhone" in seen and b"2) Android" in seen
        os.write(master, b"blackberry\n")
        wait_for_text(b"Please type 1 for iPhone or 2 for Android.")
        os.write(master, b"2\n")
        wait_for_text(b"Is Waze on your phone?")
        assert b"Directions open in Google Maps." in seen and b"Is Google Maps on your iPhone" not in seen
        os.write(master, b"y\n")
        wait_for_text(b"Google Maps API key")
        assert b"Live traffic on Android comes from Google Maps" in seen
        os.write(master, b"\n")
        wait_for_text(b"TomTom API key")
        assert b"Accidents, closures and road works on each route" in seen
        os.write(master, b"\n")
        wait_for_text(b"Done.", timeout=60)
        proc.wait(30)
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
    assert proc.returncode == 0
    assert setting(tmp_path / "home" / "config.toml", "phone", "type") == "android"
    assert setting(tmp_path / "home" / "config.toml", "maps", "apps") == "[waze]"
    secrets = (tmp_path / "home" / "secrets.env").read_text()
    assert "GOOGLE_MAPS_API_KEY" not in secrets and "TOMTOM_API_KEY" not in secrets


def test_setup_saves_the_navigation_apps_and_the_tomtom_key(tmp_path, venv):
    env = mac_env(tmp_path, venv, ARGUS_PHONE="iphone", ARGUS_MAPS_APPS="Apple, google,WAZE, bing",
                  ARGUS_TOMTOM_KEY="tt-key-0123456789")
    first = run("setup.sh", env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "Directions open in Apple Maps (also offered: google, waze)" in first.stdout
    cfg = tmp_path / "home" / "config.toml"
    assert 'app = "apple"' in cfg.read_text() and 'apps = ["google", "waze"]' in cfg.read_text()
    assert "TOMTOM_API_KEY=tt-key-0123456789" in (tmp_path / "home" / "secrets.env").read_text()
    assert "tt-key" not in cfg.read_text() and "tt-key" not in first.stdout

    def loaded():
        out = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                              "from argus import config; s = config.load(); print(s.maps_nav_apps, s.maps_news)",
                              str(REPO)], env=env, capture_output=True, text=True)
        return out.stdout.strip()
    assert loaded() == "['apple', 'google', 'waze'] True"
    env.pop("ARGUS_MAPS_APPS")
    env.pop("ARGUS_TOMTOM_KEY")
    again = run("setup.sh", env)                                  # nothing given: the saved choices stay
    assert again.returncode == 0 and "TomTom key: found (ending ...6789)" in again.stdout
    assert loaded() == "['apple', 'google', 'waze'] True" and cfg.read_text().count("\napps = ") == 1
    waze_first = run("setup.sh", dict(env, ARGUS_MAPS_APPS="waze,apple"))
    assert waze_first.returncode == 0 and "Directions open in Waze (also offered: apple)" in waze_first.stdout
    assert loaded() == "['waze', 'apple'] True"
    # settings from before this question existed: the list is added, and the app that opened stays
    cfg.write_text("".join(line for line in cfg.read_text().splitlines(True) if not line.startswith("apps = "))
                   .replace('app = "waze"', 'app = "google"'))
    old = run("setup.sh", env)
    assert old.returncode == 0 and "Directions open in Google Maps" in old.stdout
    assert 'app = "google"' in cfg.read_text() and "apps = []" in cfg.read_text() and loaded() == "['google'] True"


def test_choosing_navigation_apps(tmp_path):
    def chosen(phone, given="", main=""):
        code = (f'. "{REPO}/scripts/lib.sh"; ASSUME_YES=0; choose_nav_apps "{phone}" "{given}" "{main}"; '
                'printf "%s|%s" "$MAPS_APP" "$MAPS_APPS_TOML"')
        return subprocess.run(["bash", "-c", code], capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL).stdout
    assert chosen("iphone") == "apple|" and chosen("android") == "google|" and chosen("iphone", main="waze") == "waze|"
    assert chosen("iphone", "google waze apple") == 'google|"waze", "apple"'
    assert chosen("android", '[waze, "google"] waze') == 'waze|"google"' and chosen("iphone", "bing, mapquest") == "apple|"
    assert chosen("iphone", "apple", main="nonsense") == "apple|"


def test_calls_setup_refuses_on_android(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('[phone]\ntype = "android"\n')
    out = run("scripts/setup_calls.sh", dict(os.environ, ARGUS_HOME=str(home)))
    assert out.returncode == 1 and "go through an iPhone" in out.stderr
    off = run("scripts/setup_calls.sh", dict(os.environ, ARGUS_HOME=str(home)), "--off")
    assert off.returncode == 0 and "[calls]\nenabled = false" in (home / "config.toml").read_text()


# ---- the Android installer (run inside Termux) -----------------------------------------------------------------------
def termux_env(tmp_path, **extra):
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    for name in ("termux-sms-list", "termux-tts-speak", "termux-contact-list", "termux-location",
                 "termux-notification-list", "termux-wake-lock", "termux-setup-storage", "getprop"):
        (stubs / name).write_text("#!/bin/sh\necho '[]'\n")
        (stubs / name).chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("ARGUS_")}
    env.update(HOME=str(tmp_path / "phone"), PATH=f"{stubs}:{os.environ['PATH']}", TERMUX_VERSION="0.119",
               PREFIX=str(tmp_path / "prefix"), ARGUS_SKIP_PKG="1", ARGUS_SKIP_VALIDATION="1",
               ARGUS_PYTHON=sys.executable, ARGUS_ANTHROPIC_API_KEY="sk-ant-test", ARGUS_NAME="Sandeep",
               ARGUS_CITY="Portland, OR", ARGUS_TIMEZONE="America/Los_Angeles", ARGUS_DRIVE_ASSIST="no")
    env.update(extra)
    return env


def test_setup_in_termux_installs_argus_on_the_phone(tmp_path):
    env = termux_env(tmp_path)
    out = run("setup.sh", env, "--phone", "iphone", "--no-service")    # in Termux it's the Android setup either way
    assert out.returncode == 0, out.stdout + out.stderr
    assert "1/7  Checking this phone" in out.stdout and "Termux 0.119" in out.stdout
    assert "WhatsApp backup reader\n  ✓ Skipped (no WhatsApp key)" in out.stdout
    home = tmp_path / "phone" / ".argus"
    cfg = home / "config.toml"
    assert setting(cfg, "phone", "type") == "android" and setting(cfg, "android", "drive_assist") == "false"
    assert setting(cfg, "maps", "app") == "google" and setting(cfg, "glasses", "enabled") == "false"
    assert oct((home / "secrets.env").stat().st_mode & 0o777) == "0o600"
    launcher = tmp_path / "prefix" / "bin" / "argus-android"
    assert f"'{REPO}' android" in launcher.read_text() and oct(launcher.stat().st_mode & 0o777) == "0o700"
    widget = (tmp_path / "phone" / ".shortcuts" / "tasks" / "Argus").read_text()
    assert widget.endswith(f"exec '{launcher}' talk\n")
    boot = (tmp_path / "phone" / ".termux" / "boot" / "argus").read_text()
    assert "termux-wake-lock" in boot and f"exec '{launcher}' serve >>" in boot
    status = subprocess.run(["sh", str(launcher), "status"], env=env, capture_output=True, text=True)
    assert status.returncode == 1 and "Argus isn't running" in status.stdout
    again = run("scripts/setup_android.sh", dict(env, ARGUS_DRIVE_ASSIST="yes"), "--no-service")
    assert again.returncode == 0 and "Kept your settings" in again.stdout and "drive assist: true" in again.stdout
    assert setting(cfg, "android", "drive_assist") == "true"


def test_android_setup_checks_its_input(tmp_path):
    env = termux_env(tmp_path, ARGUS_WHATSAPP_KEY="1234")
    bad = run("scripts/setup_android.sh", env, "--no-service")
    assert bad.returncode == 1 and "64 characters" in bad.stderr
    dry = run("scripts/setup_android.sh", termux_env(tmp_path, ARGUS_WHATSAPP_KEY="AB" * 32), "--dry-run")
    assert dry.returncode == 0 and "[dry-run]" in dry.stdout and "--require-hashes" in dry.stdout
    assert not (tmp_path / "phone" / ".argus" / "config.toml").exists()
    outside = run("scripts/setup_android.sh", dict(termux_env(tmp_path), TERMUX_VERSION="", PREFIX="/usr"))
    assert outside.returncode == 1 and "inside the Termux app" in outside.stderr
    wrong_key = run("scripts/setup_android.sh", termux_env(tmp_path, ARGUS_ANTHROPIC_API_KEY="hello"), "--no-service")
    assert wrong_key.returncode == 1 and "doesn't look like a Claude API key" in wrong_key.stderr
    assert run("scripts/setup_android.sh", termux_env(tmp_path), "--bogus").returncode == 2


# ---- the server installer --------------------------------------------------------------------------------------------
def test_server_setup_needs_tailscale_and_a_phone(tmp_path):
    no_tailscale = tmp_path / "tailscale"
    no_tailscale.write_text("#!/bin/sh\nexit 1\n")
    no_tailscale.chmod(0o755)
    env = dict(os.environ, ARGUS_TAILSCALE=str(no_tailscale), ARGUS_PYTHON=sys.executable,
               ARGUS_STATE_DIR=str(tmp_path / "state"), ARGUS_OPT_DIR=str(tmp_path / "opt" / "argus"))
    out = run("scripts/setup_server.sh", env, "--no-service")
    assert out.returncode == 1 and "Tailscale isn't connected" in out.stderr and "sudo tailscale up" in out.stderr
    no_tailscale.write_text("#!/bin/sh\necho 100.64.1.2\n")
    bad = run("scripts/setup_server.sh", env, "--no-service", "--phone", "nokia")
    assert bad.returncode == 1 and "--phone must be" in bad.stderr
    dry = run("scripts/setup_server.sh", dict(env, ARGUS_ANTHROPIC_API_KEY="sk-ant-x", ARGUS_NAME="S", ARGUS_CITY="P",
                                              ARGUS_TIMEZONE="Mars/Base", ARGUS_SKIP_VALIDATION="1"),
              "--dry-run", "--phone=android")
    assert dry.returncode == 1 and "Unknown time zone Mars/Base" in dry.stderr
    unsafe = run("scripts/setup_server.sh", dict(env, ARGUS_ANTHROPIC_API_KEY="sk-ant-x", ARGUS_NAME="S",
                                                 ARGUS_CITY="P", ARGUS_TIMEZONE="UTC", ARGUS_SKIP_VALIDATION="1",
                                                 ARGUS_OPT_DIR="/"), "--no-service", "--phone=1")
    assert unsafe.returncode == 1 and "isn't a safe place" in unsafe.stderr
    helped = run("scripts/setup_server.sh", env, "--help")
    assert helped.returncode == 0 and "always-on" in helped.stdout


def test_server_setup_requires_root_for_the_real_install(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("running as root")
    ts = tmp_path / "tailscale"
    ts.write_text("#!/bin/sh\necho 100.64.1.2\n")
    ts.chmod(0o755)
    out = run("scripts/setup_server.sh", dict(os.environ, ARGUS_TAILSCALE=str(ts), ARGUS_PYTHON=sys.executable))
    assert out.returncode == 1 and "Run it with sudo" in out.stderr


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck isn't installed")
def test_shell_scripts_pass_shellcheck():
    scripts = [REPO / "setup.sh", REPO / "tests" / "e2e.sh"] + sorted((REPO / "scripts").glob("*.sh"))
    out = subprocess.run(["shellcheck", "-x", *map(str, scripts)], cwd=REPO, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout


# ---- running setup again: keep the saved answers, or go through them ---------------------------------------------
def talk(env, steps, args=(), timeout=60):
    """Run setup.sh on a terminal: for each (text, reply), wait for text, then type reply (None: just wait).
    Returns everything setup printed and its exit code."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(["bash", str(REPO / "setup.sh"), *args], env=env, stdin=slave, stdout=slave,
                            stderr=slave, close_fds=True)
    os.close(slave)
    seen = b""

    def read_until(text: bytes) -> None:
        nonlocal seen
        deadline = time.time() + timeout
        while text not in seen:
            assert time.time() < deadline, f"never saw {text!r}; got {seen[-800:]!r}"
            ready, _, _ = select.select([master], [], [], 0.5)
            if ready:
                try:
                    seen += os.read(master, 4096)
                except OSError:
                    break
    try:
        for text, reply in steps:
            read_until(text.encode())
            if reply is not None:
                os.write(master, reply.encode() + b"\n")
        read_until(b"Done.")
        proc.wait(30)
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
    return seen.decode(errors="replace"), proc.returncode


def test_setup_again_keeps_saved_answers_and_asks_only_new_ones(tmp_path, venv):
    env = mac_env(tmp_path, venv)
    first, code = talk(env, [("Type 1 or 2", "1"), ("Is Google Maps on your iPhone?", "y"), ("Is Waze on your phone?", "n"),
                             ("Google Maps API key", ""), ("TomTom API key", "")])
    assert code == 0 and "You've set up Argus on this Mac before" not in first
    answers = tmp_path / "home" / "setup-answers"
    assert answers.read_text().splitlines() == ["claude_key=saved", "google_key=skipped", "tomtom_key=skipped"]
    assert oct(answers.stat().st_mode & 0o777) == "0o600"

    again, code = talk(env, [("Keep these answers and only ask what's new?", "")])
    assert code == 0
    summary = again[again.index("Your saved answers:"):again.index("Keep these answers")]
    for line in ("Phone:             iPhone", "Maps apps:         Apple Maps, Google Maps",
                 "Claude API key:    saved (ending ...test)", "Your name:         Sandeep", "Home city:         Portland, OR",
                 "Blocked sites:     none", "Google Maps key:   skipped", "TomTom key:        skipped",
                 "Weekday briefing:  off", "Keep Mac awake:    not asked yet (new question)"):
        assert line in summary, line
    for question in ("Type 1 or 2", "Is Google Maps on your iPhone?", "Is Waze on your phone?", "Google Maps API key (",
                     "TomTom API key ("):
        assert question not in again, question                   # all answered before: not asked again
    assert "iPhone: Apple Maps, Siri Shortcuts, Meta glasses, calls from your number (optional) (saved)" in again
    assert "Google Maps key: skipped before" in again and "TomTom key: skipped before" in again

    review = {k: v for k, v in env.items() if k not in ("ARGUS_NAME", "ARGUS_CITY", "ARGUS_BLOCKED_SITES",
                                                        "ARGUS_AI_BRIEFING", "ARGUS_ANTHROPIC_API_KEY")}
    changed, code = talk(review, [
        ("Keep these answers and only ask what's new?", "n"),
        ("Type 1 or 2 [1]", ""), ("Is Google Maps on your iPhone? [Y/n]", ""), ("Is Waze on your phone? [y/N]", "y"),
        ("New Claude API key (Enter keeps the saved one)", ""), ("What should Argus call you? [Sandeep]", ""),
        ("[Portland, OR]", "Fremont, CA"), ("Sites to block, comma-separated [none]", "mybank.com"),
        ("Google Maps API key", ""), ("TomTom API key", "tt-new-key-12345"),
        ("weekday briefing at 7:30 (weather + news; roughly $1-3 a month)? [y/N]", "y")])
    assert code == 0 and "Each question now shows your saved answer" in changed
    cfg = tmp_path / "home" / "config.toml"
    assert setting(cfg, "", "city") == "Fremont, CA" and setting(cfg, "", "user_name") == "Sandeep"
    assert setting(cfg, "maps", "apps") == "[google, waze]" and setting(cfg, "security", "blocked_sites") == "[mybank.com]"
    assert setting(cfg, "schedule", "morning_briefing") == "30 7 * * 1-5"
    secrets = (tmp_path / "home" / "secrets.env").read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-test" in secrets and "TOMTOM_API_KEY=tt-new-key-12345" in secrets
    assert "tomtom_key=saved" in answers.read_text() and "google_key=skipped" in answers.read_text()
    assert cfg.read_text().count("city = ") == 1 and "\n[]\n" not in cfg.read_text()
    loaded = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                             "from argus import config; s = config.load(); print(s.city, s.blocked_sites)", str(REPO)],
                            env=env, capture_output=True, text=True)
    assert loaded.stdout.strip() == "Fremont, CA ['mybank.com']", loaded.stderr


def test_toml_set_writes_top_level_settings(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('name = "Argus"\ncity = "Portland, OR"\n\n[phone]\ntype = "iphone"\n')

    def sh(code):
        return subprocess.run(["bash", "-c", f'. "{REPO}/scripts/lib.sh"; {code}', "_", str(cfg)], capture_output=True,
                              text=True, check=True).stdout.strip()
    sh('toml_set "$1" "" city \'"Fremont, CA"\'; toml_set "$1" "" user_name \'"Sandeep"\'')
    assert cfg.read_text() == 'name = "Argus"\ncity = "Fremont, CA"\n\nuser_name = "Sandeep"\n[phone]\ntype = "iphone"\n'
    assert sh('toml_get "$1" "" user_name') == "Sandeep" and sh('toml_get "$1" phone type') == "iphone"
    flat = tmp_path / "flat.toml"
    flat.write_text('name = "Argus"\n')
    subprocess.run(["bash", "-c", f'. "{REPO}/scripts/lib.sh"; toml_set "$1" "" city \'"X"\'', "_", str(flat)], check=True)
    assert flat.read_text() == 'name = "Argus"\ncity = "X"\n'
