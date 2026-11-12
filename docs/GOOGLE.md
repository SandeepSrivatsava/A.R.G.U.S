# Connecting your Google account (Gmail, Drive, Calendar)

Argus reads your Gmail, Google Drive and Google Calendar when you ask ("what's on tomorrow?", "find my
lease", "I'm flying Friday": it finds the booking). It asks Google only for **read-only** permission, so
Google itself refuses anything else: Argus can't send, delete or change mail, files or events.

You make your own small "app" in Google Cloud, so the sign-in belongs to you and nobody else is involved.
It takes about 10 minutes and costs nothing.

## 1. Make a project

1. Open https://console.cloud.google.com and sign in with the Google account Argus should read.
2. At the top, click the project picker → **New project**. Name it `Argus` → **Create**, and select it.

## 2. Switch on the three APIs

In the search bar at the top, search for each of these and click **Enable**:

* **Gmail API**
* **Google Drive API**
* **Google Calendar API**

## 3. Describe the app (the consent screen)

1. Menu → **Google Auth Platform** → **Get started** (or **Branding** if you've done this before).
2. App name `Argus`, support email: your own address → **Next**.
3. Audience: **External** (a personal Gmail can't use Internal) → **Next**.
4. Contact information: your address → **Next** → agree → **Create**.
5. **Audience** → **Test users** → **Add users** → your own Gmail address → **Save**.

## 4. Make the Desktop client

1. **Clients** → **Create client** → Application type **Desktop app**, name `Argus Mac` → **Create**.
2. Keep the window open: it shows the **Client ID** (ends with `.apps.googleusercontent.com`) and the
   **Client secret**. Don't paste them into a chat or an email.

Now on the Mac, in Terminal:

```bash
argusctl google connect
```

Paste the Client ID, then the Client secret (it stays hidden as you type). Your browser opens:

* Choose your account.
* Google says **"Google hasn't verified this app"**. That's expected: it's your own app. Click
  **Advanced** → **Go to Argus (unsafe)**.
* Tick all three boxes (Gmail, Drive, Calendar, each "view") → **Continue**.

The browser says "Argus is connected". Then:

```bash
argusctl restart
argusctl google status     # shows which of the three Argus may read
```

## 5. Make it last (recommended)

While the app is in **Testing**, Google ends the sign-in after **7 days**, and Argus will say
"Google's sign-in for Argus has ended". Two choices:

* **Publish it:** **Audience** → **Publish app** → **Confirm**. Google allows personal-use apps (fewer
  than 100 users) to keep working without its review; you click through the "hasn't verified" warning
  once more. Run `argusctl google connect` again afterwards to get a sign-in that lasts.
* **Or stay in Testing** and run `argusctl google connect` once a week.

## Turning it off

```bash
argusctl google disconnect   # revokes Argus's access at Google and removes it from this Mac
```

You can also remove it at https://myaccount.google.com/connections.

## What Argus does with it

* Mail: searches the whole mailbox (Gmail search, so it's fast and includes archived mail), then reads
  only the message that fits. Security-code and password mails, and mail from your never-touch sites, are
  left out before Claude sees anything.
* Drive: searches names and contents, reads Google Docs, Sheets (as a table), Slides and text files.
  Files whose names look like passwords, recovery codes or keys are skipped.
* Calendar: your main calendar, for a day or a range (also used by the morning briefing).
* Whatever Argus reads to answer you is sent to Claude, like anything else you ask about. Instructions
  written inside a mail or document are treated as information, never followed.
* With Google connected, Gmail answers mail questions instead of the Mail app.
