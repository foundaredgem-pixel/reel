# Insta Reel Poster — Instagram DM Edition

Keeps the working web poster and adds Instagram DM automation.

## DM workflow
Send an Instagram Reel to the meme account from an approved Instagram account.
The webhook checks the sender ID, ignores unapproved/non-Reel messages, gets the shared Reel URL, reads its caption with yt-dlp, downloads it, applies the fixed cover, publishes it, and DMs the sender with success or the actual error.

DM jobs are queued and processed one at a time to protect Render's 512 MB free RAM.

## Render variables
Keep:
- IG_ACCESS_TOKEN
- IG_USER_ID
- IG_API_VERSION=v26.0
- PUBLIC_BASE_URL=https://reel-72t1.onrender.com

Add:
- WEBHOOK_VERIFY_TOKEN = a private random verification string
- APPROVED_SENDER_IDS = comma-separated Instagram-scoped sender IDs allowed to trigger posts

Do not use usernames in APPROVED_SENDER_IDS.

## Webhook URL
https://reel-72t1.onrender.com/webhooks/instagram

Use the same WEBHOOK_VERIFY_TOKEN in the Meta webhook verification settings.

## Meta permissions
Current Meta Instagram Login documentation lists:
instagram_business_basic
instagram_business_content_publish
instagram_business_manage_messages
instagram_business_manage_comments

Messaging requires instagram_business_manage_messages.

Meta documents that a shared-media webhook contains the URL of the shared media/post. This project uses that URL as the Reel input.

## Important
The reliable supported case is your main Instagram account -> meme Instagram account. Instagram's API is designed around conversations between the Professional account and other users; a message from the meme account to itself may not create a supported messaging event.

## Caption
The shared-media webhook supplies the post/media URL. The server uses yt-dlp to read the Reel description/caption. If Instagram requires authentication or blocks extraction, the sender receives the actual error.

## Fixed cover
static/cover.jpg is the same diamond cover.

## Manual web posting
https://reel-72t1.onrender.com/

## Security
Keep access tokens, cookies, and WEBHOOK_VERIFY_TOKEN private.
