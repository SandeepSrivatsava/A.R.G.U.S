# An always-on Argus (server mode)

The Mac isn't on all the time, and an iPhone can't run Argus in the background. So Argus can also run on a
small always-on Linux machine (a cloud server for a few dollars a month): your phone's shortcut talks to it
when the Mac is off or far away, and your reminders and scheduled results arrive as notifications on the
phone.

(On Android you may not need this: Argus runs on the phone itself; see [ANDROID.md](ANDROID.md).)

## What it can and can't do

| Can | Can't |
|---|---|
| Answer your phone's shortcut (Siri on iPhone, HTTP Shortcuts on Android) | Hear you directly (no microphone) |
| Web search, news, weather, facts | Use a screen, apps or a shell (there's no sandbox on the server to box commands in) |
| Directions with live traffic from where your phone is, with a link that opens your maps app | Anything that needs your OK (deleting, sending, buying, calling): it says so and leaves it for the Mac |
| Reminders and scheduled tasks, delivered as phone notifications | Read your Mac's files or your phone's apps |
| Remember things and learn lessons | Share memories with the Mac (each keeps its own) |

## What you need

* A small Linux server with **Debian 12 or Ubuntu 24.04** (or newer), 1 GB of memory is plenty. Any
  provider works; pick one near you.
* **Tailscale** on the server and on your phone (the same free account you use for the Mac). Argus answers
  only on the Tailscale network, never on the open internet.
* A **Claude API key**. Make a separate key for the server at platform.claude.com, so you can cancel it on
  its own.
* The **ntfy** app on your phone (free, App Store or Google Play) for notifications.
* Optional, for live traffic: a Google Maps API key and/or an Apple Maps token (below).

## Install

On the server, as a user who can use `sudo`:

```bash
# 1. Tailscale (from tailscale.com/download/linux), then sign in:
sudo tailscale up
# 2. Argus. The repository is private: sign in to GitHub first (the gh tool, or a deploy key).
sudo apt install git gh && gh auth login
gh repo clone SandeepSrivatsava/A.R.G.U.S argus && cd argus
sudo scripts/setup_server.sh
```

It asks which phone you use, your Claude key (typing is hidden), your name, city and time zone, and the
optional maps keys. It then:

* copies Argus to `/opt/argus` (owned by root; Argus itself can't change its own code);
* creates an `argus` user with no password and no login; its settings, keys and database live in
  `/var/lib/argus` (readable only by that user);
* installs a locked-down systemd service: it can write only to `/var/lib/argus`, can't gain privileges,
  can't see other users' files or processes, and can't change the system;
* installs `argus-admin`, which runs any `argusctl` command as the `argus` user;
* starts Argus and prints the phone address, the token and your notification topic.

Argus needs nothing but the Python that comes with the server (no extra packages).

## Connect your phone

* **iPhone:** make a copy of your Argus Shortcut from [IPHONE.md](IPHONE.md) and change the address to the
  server's (`http://<server's Tailscale address>:8765/ask`) and the token to the server's
  (`sudo argus-admin phone-token`). Name it "Argus anywhere", or switch the address when the Mac is off.
* **Android:** an HTTP Shortcuts shortcut, as in [ANDROID.md](ANDROID.md), with the server's address and
  token.
* **Notifications:** in the ntfy app, Subscribe to topic → the server and topic setup printed (`ntfy.sh`
  and a long random name). Keep the topic private: anyone who knows it can read your reminders. ntfy.sh
  keeps a message for up to 12 hours. For more privacy, run your own ntfy server inside Tailscale and set
  `server` under `[push]` in `/var/lib/argus/.argus/config.toml`.

Test it on the server: `sudo argus-admin ask "what's the weather tomorrow?"` and
`sudo argus-admin say "hello"` (sends a test notification).

## Reminders from the Mac while you're away

The Mac can use the same notifications: when a reminder comes while the Mac is locked or its microphone is
off, it also goes to your phone. On the Mac:

```bash
argusctl push on --topic <the server's topic>    # or without --topic, for a new one
argusctl restart && argusctl push test
```

## Live traffic

* **Google:** a Google Maps API key with the Routes API switched on (console.cloud.google.com). Give it to
  setup, or later: `sudo scripts/setup_server.sh --change-keys`.
* **Apple Maps:** needs the Apple Developer Program. In your developer account, create a Maps ID and a Maps
  private key (Certificates, Identifiers & Profiles), download the key (`AuthKey_XXXXXXXXXX.p8`), then on
  the Mac:

  ```bash
  argusctl maps-token --key ~/Downloads/AuthKey_XXXXXXXXXX.p8 --key-id XXXXXXXXXX --team-id YYYYYYYYYY --print
  ```

  That makes a token that lasts a year (the key itself never leaves the Mac). Give the token to the server's
  setup (it asks for an Apple Maps token). Without `--print`, it's saved on the Mac instead. Tokens made
  directly in the developer portal also work but last only 7 days.

`[maps] traffic = "auto"` uses the same maps company as your navigation links (Apple for an iPhone, Google
for Android) when that one is set up, otherwise the other.

## Everyday commands

```bash
sudo argus-admin status          # is it running, the phone address, push and maps
sudo argus-admin log             # recent activity
sudo argus-admin usage           # Claude spend
sudo argus-admin lessons         # what it has learned
sudo argus-admin doctor --offline
sudo systemctl restart argus     # after editing /var/lib/argus/.argus/config.toml
sudo journalctl -u argus -n 50   # its log
```

**Updating:** `git pull` in the `argus` folder, then `sudo scripts/setup_server.sh` again (it keeps your
settings and keys).

## Uninstall

```bash
sudo systemctl disable --now argus
sudo rm -rf /opt/argus /var/lib/argus /etc/systemd/system/argus.service /usr/local/sbin/argus-admin
sudo userdel argus
```
