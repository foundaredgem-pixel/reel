# Simple Insta Reel Poster

A small personal web app:

1. Paste an Instagram Reel URL.
2. Enter a caption.
3. Press Enter or POST REEL.
4. The server downloads the Reel, normalizes it with FFmpeg, uses `static/cover.jpg` as the fixed cover, and publishes it through Instagram's Reels API.

## Important limitation

Instagram Reel downloading is not a stable official "download any public Reel" API. The downloader uses yt-dlp, so Instagram can require login, rate-limit extraction, or change its web behavior. If extraction fails, the app reports the reason and does not publish.

Use the tool only for media you are authorized to download and repost.

## Deploy with Render

1. Create a new GitHub repository.
2. Upload all files in this project.
3. In Render, create a Web Service from the repository.
4. Choose the Docker runtime (the included Dockerfile installs FFmpeg).
5. Add environment variables:
   - `IG_ACCESS_TOKEN`
   - `IG_USER_ID`
   - `IG_API_VERSION=v26.0` (or the version valid for your account/app)
   - `APP_KEY` (recommended; generate a random secret)
   - `PUBLIC_BASE_URL=https://YOUR-SERVICE.onrender.com`
6. Deploy.
7. Open `https://YOUR-SERVICE.onrender.com/health`. It should return `ok: true`.
8. Open the same service URL on your phone.

## App key

The app key is only a password between your phone/browser and your backend. It is not an Instagram credential. Generate one using a password manager or another secure random generator, then put the same value in Render's `APP_KEY` and the phone app will ask for it.

If you leave `APP_KEY` blank, the app-key field is hidden. That is easier but less protected.

## Instagram publishing requirements

The Instagram API creates a Reel container from a publicly reachable `video_url`, waits for processing, and then calls `media_publish`. The processed video is served temporarily by this app at `/media/<job>/reel.mp4`.

The included processor produces a standard MP4/H.264/AAC, 1080x1920, 30 FPS file.

Your Instagram API credentials must be valid for content publishing. Keep the access token only in Render environment variables.

## Fixed cover

The exact supplied cover is stored at:

`static/cover.jpg`

The publisher sends its public `/cover.jpg` URL for every Reel.

## Optional cookies

If a public Reel cannot be downloaded without authentication, you may provide your own exported Netscape-format Instagram cookies as base64 in `INSTAGRAM_COOKIES_B64`. Never commit cookies to GitHub. Cookies are sensitive account credentials and can expire.

## Free Render caveat

Render's free web services are suitable for testing/hobby use and can spin down after inactivity. A job may therefore be slower after the service wakes. Free services are not intended as production infrastructure.

## Local testing

You do not need Python locally if you deploy with Render. For local development with Docker:

`docker build -t insta-reel-poster .`

Then run with your environment variables. The easiest user path is deployment directly from GitHub to Render.
