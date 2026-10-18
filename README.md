# Yadanabon Daily News Bot

A Render worker that checks Myawady's Yadanabon newspaper pages, downloads today's PDF when found, converts each page to JPG, and posts the images to a Telegram channel.

## Important
- Use this only where downloading and reposting the newspaper is permitted. Consider the publisher's terms/copyright.
- Never commit your Telegram bot token.
- Add the bot as a channel administrator with permission to post messages.
- This checks every 30 minutes by default and only posts a date once per local runtime date.
- The Myawady website may change its URL structure or HTML. If it does, update `discover_today_pdf()` in `main.py`.
- A Render worker runs continuously; service availability and persistent-disk support depend on your Render plan. Without persistent storage, duplicate prevention state may be lost after a restart.

## GitHub and Render setup

1. Create a GitHub repository and upload `main.py`, `requirements.txt`, `render.yaml`, `.gitignore`, and this README.
2. In Render, choose **New + → Blueprint** and connect the repository, or create a Background Worker manually.
3. Set these environment variables in Render:
   - `BOT_TOKEN`: the new token from BotFather (do not share it in chat or commit it to GitHub)
   - `CHANNEL_ID`: channel username such as `@your_channel`, or numeric channel ID such as `-1001234567890`
   - `CHECK_INTERVAL_MINUTES`: `30`
   - `RENDER_DPI`: `160` (increase for sharper images, but larger files)
   - `STATE_FILE`: `/data/posted_dates.txt` if persistent disk is mounted; otherwise use `/tmp/posted_dates.txt` and understand restart duplicate risk.
4. Add the bot as a channel admin and allow posting.
5. Deploy and check Render logs.

## Notes
- Telegram media groups accept at most 10 photos per group, so pages are split into groups.
- If the PDF isn't published yet, the worker retries on the next check.
- This implementation uses the server's date/time. Set the Render service timezone to UTC or configure the host to Myanmar time if you need exact Myanmar calendar-day behavior; for robust date handling, update the code to use `zoneinfo.ZoneInfo("Asia/Yangon")`.
