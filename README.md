# Insta Reel Poster — Memory-Fix Edition

A simple phone-friendly web app:

Phone → Render web app → yt-dlp → low-memory FFmpeg processing → Instagram Graph API.

## What changed in this edition

This version is specifically adjusted for Render's 512 MB free-instance limit:

- Downloads at no more than 1080p when possible.
- Prefers a single MP4 source to avoid unnecessary merging.
- Uses FFmpeg with one thread and limited filter threads.
- Outputs 720×1280 vertical video instead of 1080×1920 to reduce peak memory.
- Uses the `ultrafast` encoder preset.
- Deletes the original downloaded source before Instagram processing.
- Automatically deletes processed media after a short period.
- The web page now handles HTML/non-JSON server errors cleanly instead of showing `Unexpected token '<'`.

## Deploy

1. Create a new GitHub repository, for example `Insta-Reel-Poster`.
2. Upload every file/folder from this project. Upload the contents, not the ZIP itself.
3. In Render, create a new Web Service from that GitHub repository.
4. Runtime: Docker.
5. Plan: Free.
6. Add these environment variables:

- `IG_ACCESS_TOKEN` = your Instagram publishing access token
- `IG_USER_ID` = your Instagram user ID
- `IG_API_VERSION` = `v26.0`
- `APP_KEY` = optional private key for protecting the web page
- `PUBLIC_BASE_URL` = `https://reel-72t1.onrender.com`
- `INSTAGRAM_COOKIES_B64` = optional; leave blank unless downloading public Reels requires your Instagram session

7. Deploy.
8. Open:
   `https://reel-72t1.onrender.com/health`

Expected result includes `"ok": true`.

## Important: PUBLIC_BASE_URL

For Instagram to fetch the processed video, the video must have a public HTTPS URL.

Set:

`PUBLIC_BASE_URL=https://reel-72t1.onrender.com`

Do not add a trailing slash.

If your Render service gets a different URL later, update this variable.

## App key

`APP_KEY` is NOT an Instagram credential.

It is simply a private password between you and this web app. If you leave it blank, the app does not ask for a key.

If you set one, the app-key field appears automatically.

## Fixed cover

The fixed cover is:

`static/cover.jpg`

It is already included in this package.

## Instagram publishing

The backend needs valid Instagram publishing credentials in Render:

- `IG_ACCESS_TOKEN`
- `IG_USER_ID`
- `IG_API_VERSION`

The phone page never receives the Instagram access token.

## Instagram Reel downloading

The app uses yt-dlp to retrieve the source Reel. Instagram can require login, rate-limit automated requests, or change its web delivery behavior. Therefore downloading is not guaranteed for every Reel.

If your own Instagram session is required, you can optionally provide a base64-encoded Netscape `cookies.txt` file through `INSTAGRAM_COOKIES_B64`. Do not put that cookie data in the HTML page.

## Free Render limitation

Render's free web service can sleep after inactivity and has limited RAM. This project is optimized for the 512 MB limit, but very unusual/large videos can still fail.

The app intentionally processes at 720×1280 to reduce memory use. That is a valid vertical Reel resolution.

## Local testing

No local Python installation is required for deployment. Render builds the Docker image and installs Python, FFmpeg, and yt-dlp for you.

## Use

Open the Render URL on your phone, paste the Reel link, enter the caption, and press Enter in the caption box or tap POST REEL.

Only repost media you are authorized to download and publish.
