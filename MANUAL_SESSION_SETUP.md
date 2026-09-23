# Connect Drips to IssueBot by hand

Use this if you do not want to run the connector program. It takes about 3 minutes
in your normal Chrome, and you never leave the browser you already use.

You will need:

- Google Chrome on a computer (this does not work on a phone)
- Your Drips account, already logged in
- Your IssueBot login: <https://issue-bot-vert.vercel.app>

## 1. Open Drips and log in

1. Open Chrome and go to <https://www.drips.network>.
2. Log in if you are not already logged in.
3. Leave this tab open.

## 2. Copy your Drips token

1. On the Drips tab, press **F12**. A panel opens on the side or the bottom.
   (If F12 does nothing, click the **⋮** menu → **More tools** → **Developer tools**.)
2. At the top of that panel click **Application**. If you cannot see it, click the
   **»** arrow to show the hidden tabs.
3. In the left list open **Cookies**, then click **https://www.drips.network**.
4. Find the row named exactly **`wave_refresh_token`**.
5. Double-click its **Value**, select the whole thing, and copy it (Ctrl+C).
   It is a long line of letters and numbers.

The value is like a key to your account, so do not send it to anyone.

## 3. Build the JSON

1. Open **Notepad**. Do not use Word or WhatsApp: they change quote marks and
   IssueBot will reject the text.
2. Paste this line into Notepad:

   ```json
   {"cookies": [{"name": "wave_refresh_token", "value": "PASTE_HERE", "domain": ".drips.network"}]}
   ```

3. Select the words `PASTE_HERE` and paste your token over them. Keep the `"`
   quotes around it. The result should look like this, but much longer:

   ```json
   {"cookies": [{"name": "wave_refresh_token", "value": "a1b2c3d4e5f6...9z8y", "domain": ".drips.network"}]}
   ```

4. Select the whole line and copy it.

## 4. Paste it into IssueBot

1. Go to <https://issue-bot-vert.vercel.app> and log in.
2. Click **Setup** (or **Configure automation**).
3. Open **Advanced session import**.
4. Paste your line into the **Or paste session JSON** box.
5. Tick **Enable monitoring**.
6. Click **Save configuration**.

## 5. Check it worked

1. Go to the **Dashboard**.
2. Within about 5 minutes, **Last successful scan** should show a fresh time, and
   the status should read **ACTIVE**, not **PAUSED**.
3. If an error appears, see the list below.

Do **not** click **Log out** on Drips afterwards. That can cancel the connection
you just made. To use another account, open a new Chrome profile instead.

## If something goes wrong

**"Drips session must be a valid JSON file"**
The pasted text is not complete JSON. Common causes:

- Only the token was pasted, without the rest of the line
- Curly quotes (`“ ”`) instead of straight ones (`"`), from copying through Word
  or a chat app. Retype the line in Notepad.
- A missing `}` or `]` at the end, or the words `PASTE_HERE` still there

**"Drips session does not contain a Wave access or refresh token"**
The cookie name is wrong. It has to be exactly `wave_refresh_token`
(or `wave_access_token`). Check you copied from the right row in step 2.

**"Drips session refresh failed" or "session has expired"**
The token is old or came from the wrong row. Log in to Drips again and repeat
from step 2 with a fresh token.

**There is no `wave_refresh_token` row**
You are not logged into Drips in that browser, or you are looking at a different
site in the Cookies list. Log in to Drips, refresh the page and look again.

## Two things to know

- **You may have to redo this now and then.** Drips logins expire. When IssueBot
  says the session expired, repeat these steps with a fresh token.
- **The connector program is still the easier option.** It copies everything
  automatically, including your timezone. Sessions pasted by hand send the
  default timezone instead of yours, which is harmless but less accurate.
