# IssueBot User Guide

IssueBot is your private workspace for monitoring Drips Wave opportunities, reviewing issue candidates, and tracking your applications.

## What you will receive

The administrator will give you:

- Your IssueBot username
- A temporary password
- The `issuebot-connector.exe` application
- The IssueBot website: https://issuebot-hthd.onrender.com

Keep your username, password, and connection codes private.

## 1. Sign in

1. Open the user login page:
   https://issuebot-hthd.onrender.com/login
2. Enter the username and temporary password provided by the administrator.
3. Create a new private password when prompted. It must contain at least 10 characters.

Do not use the administrator login page.

## 2. Connect your Drips account

1. After signing in, click **Setup**.
2. Under **Drips connection**, click **Generate connection code**.
3. Copy the code shown at the top of the page.
4. Open `issuebot-connector.exe` on a Windows computer.
5. Enter this website address:

   ```text
   https://issuebot-hthd.onrender.com
   ```

6. Paste your one-time connection code.
7. The connector opens Drips in a browser window. Sign in to your own Drips account.
8. Wait for the connector to confirm that the connection succeeded.

The connection code expires after 15 minutes and works only once. Generate a new code if it expires or fails.

The connector transfers only the Drips session needed by IssueBot. It does not ask for or store your GitHub password.

## 3. Enable monitoring

1. Return to **Setup** in IssueBot.
2. Confirm that Drips shows as **Connected**.
3. Select **Enable monitoring**.
4. Choose your review queue size and scan interval.
5. Click **Save configuration**.

IssueBot discovers opportunities, but it does not automatically apply for them.

## 4. Review an opportunity

New opportunities appear under **Issue candidates** on the Dashboard.

1. Open the issue link and read the complete issue scope.
2. Check that you have the skills and time to complete it.
3. Review and personalize the proposed application message.
4. Click **Approve and apply** only when you are ready.
5. Click **Dismiss** if the issue is not suitable.

Do not submit generic applications or apply without reviewing the issue.

## 5. Monitor your work

The Dashboard shows:

- **Awaiting review:** opportunities waiting for your decision
- **Pending applications:** applications waiting for Drips approval
- **Accepted assignments:** work assigned to you
- **Activity:** scans, applications, accepted assignments, connection updates, and errors

Refresh the page to view the latest activity.

## Important Drips rules

- Use only your own Drips account and KYC identity.
- Do not share a Drips account with another person.
- Review the complete issue scope before applying.
- Apply only when you intend to complete the work.
- Follow the repository's contribution guidelines.
- Avoid low-quality or generic AI-generated submissions.

Drips Wave terms: https://docs.drips.network/wave/terms-and-rules/

## Troubleshooting

### The website is taking time to open

The free website may sleep when it has not been used recently. Wait about one minute and refresh the page.

### Invalid CSRF token or expired sign-in page

Close old IssueBot tabs, open a fresh private/incognito window, and sign in again.

### The connection code failed

Generate a new code. Make sure you use it within 15 minutes and enter the website address exactly as shown above.

### Drips shows as disconnected

Generate a new connection code and run the connector again. Drips sessions can expire and require reconnection.

### Windows displays an unknown publisher warning

The connector is not currently code-signed. Confirm that the file came directly from your IssueBot administrator before opening it.

### You forgot your password

Contact the IssueBot administrator. They can issue a new temporary password.

## Getting help

Contact your IssueBot administrator and include:

- What you were trying to do
- The exact error message
- A screenshot that does not expose passwords, connection codes, or Drips session information

Never send your password, Neon database URL, Drips session JSON, or connection code to another person.
