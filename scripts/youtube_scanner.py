import re
import sys
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
import requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from datetime import datetime

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

MAX_VIDEOS = 100          # How many videos to scan per channel
MAX_WORKERS = 10          # Concurrent threads for link checking
REQUEST_TIMEOUT = 8       # Seconds before a link check times out
USER_AGENT = (            # Some servers reject requests without a UA header
    "Mozilla/5.0 (compatible; LinkChecker/1.0)"
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_handle_or_id(channel_input: str) -> tuple[str, str]:
    """
    Parse a raw channel input and return (kind, value) where kind is one of:
      'handle'  - e.g. '@mkbhd'
      'channel' - e.g. 'UCBcRF18a7Qf58cCRy5xuWwQ'
      'raw'     - anything else (passed through as-is for the caller to try)
    """
    channel_input = channel_input.strip().rstrip("/")

    if "youtube.com/@" in channel_input:
        handle = "@" + channel_input.split("@")[1].split("?")[0]
        return "handle", handle
    elif "youtube.com/channel/" in channel_input:
        cid = channel_input.split("/channel/")[1].split("?")[0]
        return "channel", cid
    elif channel_input.startswith("@"):
        return "handle", channel_input
    else:
        return "raw", channel_input


def resolve_channel_id(youtube, channel_input: str) -> str:
    """
    Return the internal channel ID (UCxxx) for any channel input format.
    Raises ValueError if the channel cannot be found.
    """
    kind, value = extract_handle_or_id(channel_input)

    if kind == "handle":
        resp = youtube.channels().list(
            forHandle=value, part="id", maxResults=1
        ).execute()
        items = resp.get("items", [])
        if items:
            return items[0]["id"]
        raise ValueError(f"No channel found for handle: {value}")

    if kind == "channel":
        return value

    resp = youtube.channels().list(
        id=value, part="id", maxResults=1
    ).execute()
    if resp.get("items"):
        return resp["items"][0]["id"]

    resp = youtube.channels().list(
        forUsername=value, part="id", maxResults=1
    ).execute()
    if resp.get("items"):
        return resp["items"][0]["id"]

    raise ValueError(f"Channel not found: {value}")


def get_uploads_playlist_id(youtube, channel_id: str) -> str:
    """Return the 'uploads' playlist ID for a channel."""
    resp = youtube.channels().list(
        id=channel_id, part="contentDetails", maxResults=1
    ).execute()
    try:
        return resp["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except (KeyError, IndexError) as exc:
        raise ValueError("Could not retrieve uploads playlist") from exc


def fetch_video_ids(youtube, playlist_id: str, max_videos: int) -> list[str]:
    """Fetch up to max_videos video IDs from a playlist."""
    video_ids: list[str] = []
    page_token = None

    while len(video_ids) < max_videos:
        batch_size = min(50, max_videos - len(video_ids))
        kwargs = dict(
            playlistId=playlist_id,
            part="contentDetails",
            maxResults=batch_size,
        )
        if page_token:
            kwargs["pageToken"] = page_token

        resp = youtube.playlistItems().list(**kwargs).execute()
        for item in resp.get("items", []):
            video_ids.append(item["contentDetails"]["videoId"])

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return video_ids


def fetch_video_details(youtube, video_ids: list[str]) -> list[dict]:
    """Batch-fetch snippet and statistics for a list of video IDs."""
    details: list[dict] = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        resp = youtube.videos().list(
            id=",".join(batch), part="snippet,statistics", maxResults=50
        ).execute()
        details.extend(resp.get("items", []))
    return details


def extract_urls(text: str) -> list[str]:
    """Extract and lightly clean URLs from a block of text."""
    raw = re.findall(r"https?://[^\s]+", text)
    cleaned = [u.rstrip(".,;:!?)\"'") for u in raw]
    seen: set[str] = set()
    unique: list[str] = []
    for url in cleaned:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def check_url(url: str) -> tuple[str, bool, str]:
    """
    Check whether a URL is reachable.
    Returns (url, is_broken, reason).
    Tries HEAD first; falls back to GET if the server doesn't cooperate.
    """
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.head(
            url, timeout=REQUEST_TIMEOUT, allow_redirects=True, headers=headers
        )
        if r.status_code == 405:
            r = requests.get(
                url,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
                headers=headers,
                stream=True,
            )
        if r.status_code == 429:
            return url, False, "Rate limited (likely working — verify manually)"
        if r.status_code >= 400:
            return url, True, f"HTTP {r.status_code}"
        return url, False, "OK"
    except requests.exceptions.Timeout:
        return url, True, "Timeout"
    except requests.exceptions.TooManyRedirects:
        return url, True, "Too many redirects"
    except requests.exceptions.RequestException as exc:
        return url, True, str(exc)


# ---------------------------------------------------------------------------
# Main scanner
# ---------------------------------------------------------------------------

def scan_channel(channel_input: str) -> dict:
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

    try:
        channel_id = resolve_channel_id(youtube, channel_input)
        log.info("Resolved channel ID: %s", channel_id)
    except (ValueError, HttpError) as exc:
        return {"error": str(exc)}

    try:
        playlist_id = get_uploads_playlist_id(youtube, channel_id)
    except (ValueError, HttpError) as exc:
        return {"error": str(exc)}

    log.info("Fetching up to %d video IDs...", MAX_VIDEOS)
    try:
        video_ids = fetch_video_ids(youtube, playlist_id, MAX_VIDEOS)
    except HttpError as exc:
        return {"error": f"API error fetching videos: {exc}"}

    log.info("Found %d videos. Fetching descriptions...", len(video_ids))
    try:
        videos = fetch_video_details(youtube, video_ids)
    except HttpError as exc:
        return {"error": f"API error fetching video details: {exc}"}

    video_url_map: list[tuple[dict, list[str]]] = []
    all_urls: set[str] = set()

    for video in videos:
        snippet = video["snippet"]
        urls = extract_urls(snippet.get("description", ""))
        if urls:
            video_url_map.append((video, urls))
            all_urls.update(urls)

    log.info(
        "Found %d unique URLs across %d videos. Checking links...",
        len(all_urls),
        len(video_url_map),
    )

    url_status: dict[str, tuple[bool, str]] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_url, url): url for url in all_urls}
        completed = 0
        for future in as_completed(futures):
            url, is_broken, reason = future.result()
            url_status[url] = (is_broken, reason)
            completed += 1
            print(
                f"\r  Checked {completed}/{len(all_urls)} URLs...",
                end="",
                flush=True,
            )
    print()

    # ---------------------------------------------------------------------------
    # Revenue estimate
    # ---------------------------------------------------------------------------
    # Formula: views on affected videos × click-through rate × conversion rate
    # × average order value × average commission rate
    #
    # Assumptions (based on beauty/lifestyle creator industry benchmarks):
    #   CTR on description links : 0.5%  (conservative)
    #   Conversion rate          : 2.0%
    #   Average order value      : $60
    #   Average commission rate  : 15%  (typical for ShopMy/LTK)
    #   Broken link loss factor  : proportion of links that are broken

    CTR = 0.005
    CONVERSION_RATE = 0.02
    AVG_ORDER_VALUE = 60
    COMMISSION_RATE = 0.15

    total_broken = sum(1 for broken, _ in url_status.values() if broken)
    total_links = len(all_urls)
    broken_ratio = total_broken / total_links if total_links > 0 else 0

    results = {
        "channel": channel_input,
        "channel_id": channel_id,
        "total_videos": len(videos),
        "total_links": total_links,
        "broken_links": total_broken,
        "broken_ratio": broken_ratio,
        "estimated_monthly_loss_low": 0,
        "estimated_monthly_loss_high": 0,
        "estimated_annual_loss": 0,
        "videos_with_broken_links": [],
    }

    total_affected_views = 0

    for video, urls in video_url_map:
        snippet = video["snippet"]
        vid = video["id"]
        view_count = int(video.get("statistics", {}).get("viewCount", 0))

        broken_details = [
            {"url": u, "reason": url_status[u][1]}
            for u in urls
            if url_status[u][0]
        ]

        if broken_details:
            total_affected_views += view_count
            results["videos_with_broken_links"].append(
                {
                    "title": snippet["title"],
                    "video_id": vid,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "view_count": view_count,
                    "broken_link_count": len(broken_details),
                    "broken_links": broken_details,
                }
            )

    # Calculate revenue estimate using total views on affected videos
    if total_affected_views > 0:
        monthly_clicks = total_affected_views * CTR * broken_ratio
        monthly_conversions = monthly_clicks * CONVERSION_RATE
        monthly_commission = monthly_conversions * AVG_ORDER_VALUE * COMMISSION_RATE

        # Low estimate: half the benchmark assumptions
        results["estimated_monthly_loss_low"] = round(monthly_commission * 0.5)
        # High estimate: full benchmark assumptions
        results["estimated_monthly_loss_high"] = round(monthly_commission)
        results["estimated_annual_loss"] = round(monthly_commission * 12)

    return results

# ---------------------------------------------------------------------------
# HTML report generator
# ---------------------------------------------------------------------------

def generate_html_report(results: dict) -> str:
    """Generate a professional HTML audit report."""
    date_str = datetime.now().strftime('%B %d, %Y')

    monthly_low = results["estimated_monthly_loss_low"]
    monthly_high = results["estimated_monthly_loss_high"]
    annual_loss = results["estimated_annual_loss"]

    # Sort videos by broken link count, most critical first
    sorted_videos = sorted(
        results["videos_with_broken_links"],
        key=lambda x: x["view_count"] * x["broken_link_count"],
        reverse=True
    )

    # Build the per-video HTML blocks
    video_blocks = ""
    for i, video in enumerate(sorted_videos, 1):
        broken_rows = ""
        for item in video["broken_links"]:
            reason = item["reason"]
            if "404" in reason:
                explanation = "Page no longer exists — remove or replace this link"
            elif "400" in reason:
                explanation = "Affiliate link expired — update with a current link"
            elif "403" in reason:
                explanation = "Access blocked — verify manually before removing"
            elif "Timeout" in reason:
                explanation = "Site not responding — check manually"
            else:
                explanation = "Link unreachable — check manually"

            broken_rows += f"""
                <tr>
                    <td style="padding:8px;border-bottom:1px solid #eee;word-break:break-all;font-size:13px;">
                        <a href="{item['url']}" target="_blank">{item['url']}</a>
                    </td>
                    <td style="padding:8px;border-bottom:1px solid #eee;font-size:13px;color:#e53935;">
                        {reason}
                    </td>
                    <td style="padding:8px;border-bottom:1px solid #eee;font-size:13px;color:#555;">
                        {explanation}
                    </td>
                </tr>"""

        video_blocks += f"""
        <div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;
                    padding:20px;margin-bottom:16px;">
            <div style="display:flex;justify-content:space-between;align-items:center;
                        margin-bottom:12px;">
                <div>
                    <span style="color:#999;font-size:13px;">#{i}</span>
                    <a href="{video['url']}" target="_blank"
                       style="font-size:16px;font-weight:600;color:#1a1a1a;
                              text-decoration:none;margin-left:8px;">
                        {video['title']}
                    </a>
                </div>
                <div style="text-align:right;flex-shrink:0;margin-left:16px;">
                    <span style="background:#ffebee;color:#e53935;padding:4px 10px;
                                 border-radius:12px;font-size:13px;font-weight:600;">
                        {video['broken_link_count']} broken
                    </span>
                    <div style="color:#999;font-size:12px;margin-top:4px;">
                        {video['view_count']:,} views
                    </div>
                </div>
            </div>
            <table style="width:100%;border-collapse:collapse;">
                <thead>
                    <tr style="background:#f5f5f5;">
                        <th style="padding:8px;text-align:left;font-size:12px;
                                   color:#666;width:45%;">URL</th>
                        <th style="padding:8px;text-align:left;font-size:12px;
                                   color:#666;width:15%;">Error</th>
                        <th style="padding:8px;text-align:left;font-size:12px;
                                   color:#666;width:40%;">What to do</th>
                    </tr>
                </thead>
                <tbody>{broken_rows}</tbody>
            </table>
        </div>"""

    # Summary stats bar
    broken_pct = round((results['broken_links'] / results['total_links'] * 100)
                       if results['total_links'] > 0 else 0)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YouTube Link Audit — {results['channel']}</title>
</head>
<body style="margin:0;padding:0;background:#f4f6f8;font-family:Arial,sans-serif;">

    <!-- Header -->
    <div style="background:#1a1a2e;color:#fff;padding:32px 40px;">
        <h1 style="margin:0 0 4px 0;font-size:24px;">YouTube Link Audit Report</h1>
        <p style="margin:0;color:#aaa;font-size:14px;">
            {results['channel']} &nbsp;·&nbsp; {date_str}
        </p>
    </div>

    <div style="max-width:860px;margin:32px auto;padding:0 20px;">

        <!-- Revenue impact banner -->
        <div style="background:#e53935;color:#fff;border-radius:10px;
                    padding:28px 32px;margin-bottom:28px;">
            <p style="margin:0 0 6px 0;font-size:14px;opacity:0.85;">
                ESTIMATED MONTHLY REVENUE LOSS
            </p>
            <p style="margin:0;font-size:42px;font-weight:700;">
                ${monthly_low:,} – ${monthly_high:,}
                <span style="font-size:20px;font-weight:400;">/month</span>
            </p>
            <p style="margin:8px 0 0 0;font-size:15px;opacity:0.85;">
                That's up to <strong>${annual_loss:,} per year</strong> in lost
                affiliate commissions from broken links alone.
            </p>
            <p style="margin:12px 0 0 0;font-size:12px;opacity:0.65;">
                Based on 0.5% CTR on description links, 2% conversion rate,
                $60 average order value and 15% affiliate commission.
                Your actual figures may vary.
            </p>
        </div>

        <!-- Summary stats -->
        <div style="display:grid;grid-template-columns:repeat(4,1fr);
                    gap:16px;margin-bottom:28px;">
            <div style="background:#fff;border-radius:8px;padding:20px;
                        border-top:4px solid #1a1a2e;text-align:center;">
                <div style="font-size:32px;font-weight:700;color:#1a1a2e;">
                    {results['total_videos']}
                </div>
                <div style="font-size:13px;color:#666;margin-top:4px;">
                    Videos scanned
                </div>
            </div>
            <div style="background:#fff;border-radius:8px;padding:20px;
                        border-top:4px solid #1a1a2e;text-align:center;">
                <div style="font-size:32px;font-weight:700;color:#1a1a2e;">
                    {results['total_links']}
                </div>
                <div style="font-size:13px;color:#666;margin-top:4px;">
                    Unique links found
                </div>
            </div>
            <div style="background:#fff;border-radius:8px;padding:20px;
                        border-top:4px solid #e53935;text-align:center;">
                <div style="font-size:32px;font-weight:700;color:#e53935;">
                    {results['broken_links']}
                </div>
                <div style="font-size:13px;color:#666;margin-top:4px;">
                    Broken links
                </div>
            </div>
            <div style="background:#fff;border-radius:8px;padding:20px;
                        border-top:4px solid #e53935;text-align:center;">
                <div style="font-size:32px;font-weight:700;color:#e53935;">
                    {broken_pct}%
                </div>
                <div style="font-size:13px;color:#666;margin-top:4px;">
                    Links broken
                </div>
            </div>
        </div>

        <!-- What this means -->
        <div style="background:#fff;border-radius:8px;padding:24px 28px;
                    margin-bottom:28px;border-left:4px solid #fb8c00;">
            <h2 style="margin:0 0 12px 0;font-size:18px;color:#1a1a2e;">
                What this means
            </h2>
            <p style="margin:0 0 10px 0;color:#444;line-height:1.6;">
                Every broken affiliate link in a video description is a missed
                commission — not just once, but every single day that video
                continues to get views. Older videos often have the most broken
                links and can still drive significant traffic.
            </p>
            <p style="margin:0;color:#444;line-height:1.6;">
                The estimate above is based on your total views on affected videos,
                industry-average click-through and conversion rates for beauty
                creators, and a typical affiliate commission of 15% on a $60
                average order.
            </p>
        </div>

        <!-- How to fix -->
        <div style="background:#fff;border-radius:8px;padding:24px 28px;
                    margin-bottom:28px;border-left:4px solid #43a047;">
            <h2 style="margin:0 0 16px 0;font-size:18px;color:#1a1a2e;">
                How to fix this
            </h2>
            <div style="margin-bottom:16px;">
                <p style="margin:0 0 6px 0;font-weight:600;color:#1a1a2e;">
                    Step 1 — Prioritise
                </p>
                <p style="margin:0;color:#444;line-height:1.6;">
                    Start with the videos below that have the most broken links.
                    These are ordered by severity.
                </p>
            </div>
            <div style="margin-bottom:16px;">
                <p style="margin:0 0 6px 0;font-weight:600;color:#1a1a2e;">
                    Step 2 — Update descriptions in YouTube Studio
                </p>
                <p style="margin:0;color:#444;line-height:1.6;">
                    Go to YouTube Studio → Content → click a video → Details →
                    scroll to Description. For each broken link, either replace
                    it with a current affiliate link for the same product, or
                    remove it entirely if the product is discontinued.
                </p>
            </div>
            <div>
                <p style="margin:0 0 6px 0;font-weight:600;color:#1a1a2e;">
                    Step 3 — Understand the error types
                </p>
                <ul style="margin:0;padding-left:20px;color:#444;line-height:1.8;">
                    <li>
                        <strong>HTTP 404</strong> — Page gone. Remove or replace
                        the link immediately.
                    </li>
                    <li>
                        <strong>HTTP 400</strong> — Affiliate link expired or
                        product delisted. Replace with a current link.
                    </li>
                    <li>
                        <strong>HTTP 403</strong> — Access blocked. Click the
                        link manually to check if it still works before removing.
                    </li>
                    <li>
                        <strong>Timeout</strong> — Site not responding. Check
                        manually and try again later.
                    </li>
                </ul>
            </div>
        </div>

        <!-- Broken links by video -->
        <h2 style="font-size:20px;color:#1a1a2e;margin:0 0 16px 0;">
            Broken links by video
            <span style="font-size:14px;font-weight:400;color:#999;">
                (sorted by severity)
            </span>
        </h2>
        {video_blocks}

        <!-- Footer -->
        <div style="text-align:center;padding:32px 0;color:#999;font-size:13px;">
            <p style="margin:0;">
                Generated by Link Sniffer &nbsp;·&nbsp; {date_str}
            </p>
            <p style="margin:6px 0 0 0;">
                Revenue estimates are based on industry benchmarks and are
                intended as directional guidance only.
            </p>
        </div>

    </div>
</body>
</html>"""

    return html

# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def print_report(results: dict) -> None:
    if "error" in results:
        print(f"\n Error: {results['error']}")
        return

    sep = "=" * 60
    print(f"\n{sep}")
    print("  YOUTUBE CHANNEL LINK AUDIT")
    print(sep)
    print(f"  Channel  : {results['channel']}")
    print(f"  ID       : {results['channel_id']}")
    print(f"  Videos   : {results['total_videos']}")
    print(f"  Links    : {results['total_links']} unique")
    print(f"  Broken   : {results['broken_links']}")
    print(sep)

    if results["broken_links"] == 0:
        print("\n No broken links found!\n")
    else:
        print(
            f"\n  {len(results['videos_with_broken_links'])} video(s) have broken links:\n"
        )
        for video in results["videos_with_broken_links"]:
            print(f"  {video['title']}")
            print(f"     {video['url']}")
            for item in video["broken_links"]:
                print(f"       x {item['url']}")
                print(f"         Reason: {item['reason']}")
            print()

    print(sep + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    channel_input = input(
        "Enter YouTube channel URL or @handle\n"
        "(e.g. https://youtube.com/@channelname or @channelname): "
    ).strip()

    if not channel_input:
        print("No input provided.")
        sys.exit(1)

    print("\nScanning channel - this may take a minute...\n")
    report = scan_channel(channel_input)
    print_report(report)

    # Generate HTML report
    if "error" not in report:
        html = generate_html_report(report)
        channel_slug = channel_input.strip("/").split("/")[-1].replace("@", "")
        reports_dir = "/Users/prettykaur/link-sniffer/reports"
        date_stamp = datetime.now().strftime("%Y-%m-%d")
        filename = f"{reports_dir}/audit_{channel_slug}_{date_stamp}.html"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"HTML report saved: {filename}")
        print(f"Open it in your browser: open '{filename}'\n")