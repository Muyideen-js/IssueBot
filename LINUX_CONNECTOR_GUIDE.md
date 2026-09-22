# IssueBot Linux Connection Guide

This guide connects your Drips Wave account to IssueBot from a Linux computer.

## What you need

- A 64-bit Linux computer with a graphical desktop.
- Your personal IssueBot username and temporary password.
- The `issuebot-connector-linux-x86_64.tar.gz` file.
- Internet access.
- Your own GitHub/Drips account.

Do not use another person's GitHub, Drips, or IssueBot account.

## 1. Confirm that your computer is supported

Open Terminal and run:

```bash
uname -m
```

Continue if the result is:

```text
x86_64
```

The supplied connector does not support ARM devices that display `aarch64` or
`arm64`.

## 2. Sign in to IssueBot

Open this page:

https://issue-bot.app.runonflux.io/login

Sign in using the personal username and temporary password provided to you.
Change the temporary password when prompted.

## 3. Generate a connection code

1. Open **Setup** in your IssueBot dashboard.
2. Select the option to connect your Drips account.
3. Generate a one-time connection code.
4. Keep this page open.

The code expires after 15 minutes and can only be used once. Generate a new code
if the old one expires.

## 4. Extract the Linux connector

Move `issuebot-connector-linux-x86_64.tar.gz` into your Downloads folder. Open
Terminal and run:

```bash
cd ~/Downloads
tar -xzf issuebot-connector-linux-x86_64.tar.gz
chmod +x issuebot-connector
```

## 5. Run the connector

Run:

```bash
./issuebot-connector
```

Enter the following website URL when requested:

```text
https://issue-bot.app.runonflux.io
```

Then enter the one-time connection code from your IssueBot Setup page.

## 6. Complete the Drips login

1. The connector opens a browser window.
2. Sign in to Drips with your own GitHub account.
3. Complete any GitHub or Drips verification prompts.
4. Wait until the Drips Wave issue page is fully visible.
5. Return to Terminal and press **Enter**.
6. Wait for this confirmation:

```text
Drips connected successfully. You can close this window.
```

## 7. Configure your automation

Return to your IssueBot dashboard and:

1. Add your own AI API key, or configure your fallback application message.
2. Configure your repository priorities and application limits.
3. Enable automatic applications.
4. Confirm that the dashboard reports your Drips session as connected.

After connection, you can close the connector and turn off your computer. The
hosted IssueBot service continues monitoring on your behalf.

## Troubleshooting

### Permission denied

Run:

```bash
chmod +x issuebot-connector
./issuebot-connector
```

### File not found

Check the files in your current directory:

```bash
ls
```

Use `cd` to enter the folder containing the downloaded archive and try again.

### Connection code rejected or expired

Return to IssueBot Setup, generate a new code, and restart the connector.

### Browser or connector error

Copy the exact Terminal error and send it to the IssueBot administrator. Never
send your GitHub password, API key, Drips cookies, or connection secrets.
