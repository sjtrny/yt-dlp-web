# iOS Shortcut

Send Safari's current page URL to yt-dlp-web. The server downloads the video.
The file does not download to the phone. Safari cookies are not sent.

## Template

The [unsigned template](Download%20with%20yt-dlp-web.unsigned.shortcut) accepts
URLs and Safari Web Pages. It sends the first URL to `POST /api/v1/downloads`.
It shows the accepted job ID or the server response. Acceptance does not mean
the download is complete.

The template contains a placeholder endpoint. It is not a signed installer.
Signing, import, and use on an iPhone have not been tested.
The file structure and server API have been checked.

### Build and sign on Mac

From the repository root, with Python 3:

```sh
python3 shortcuts/build_shortcut.py
shortcuts sign --mode anyone \
  --input 'shortcuts/Download with yt-dlp-web.unsigned.shortcut' \
  --output 'shortcuts/Download with yt-dlp-web.shortcut'
```

Sign the placeholder template. Set the real endpoint after import.
Apple receives a copy during signing. See [Apple's signing guide](https://support.apple.com/guide/shortcuts-mac/apd455c82f02/mac).

Send the signed file to the iPhone with AirDrop. Open it in Shortcuts.
Set **API URL** to the full endpoint, such as
`https://your-server.example/api/v1/downloads`.

The iPhone must be able to reach the server. `localhost` refers to the phone.
The generated signed file is ignored by Git.

### Build on iPhone

1. Create **Download with yt-dlp-web** in Shortcuts. Enable **Show in Share
   Sheet**. Accept **URLs** and **Safari Web Pages**. These are supported
   [input types](https://support.apple.com/guide/shortcuts/apd7644168e1/ios).
2. Add **Get URLs from Input** with **Shortcut Input**.
   Add **Get Item from List** and select **First Item**.
3. Add **Get Contents of URL**. Set the URL to the full API endpoint.
4. Set the method to **POST** and the request body to **JSON**. Add a Text field:
   key `url`, value **First Item**. Use the variable, not typed JSON.
   See [Apple's API guide](https://support.apple.com/guide/shortcuts/apd58d46713f/ios).
5. Add **Get Dictionary Value** with key `job.id` from the HTTP response.
   Add **If** the value is present, then **Show Notification** with the job ID.
   In **Otherwise**, use **Show Result** with the HTTP response.

## Use

1. Open a video page in Safari.
2. Select **Share**, then **Download with yt-dlp-web**.
3. Check the job on the server's **Downloads** page.

## Retries

Sharing an active URL returns the existing job. Sharing it after completion
starts another download and keeps both copies.
See [API retry rules](../docs/api.md#duplicate-and-retry-rules).
