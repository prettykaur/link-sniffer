# link-sniffer

A Python tool that scans a YouTube channel's video descriptions for broken affiliate links and estimates how much revenue those broken links are costing.

Given a channel handle or URL, it fetches the most recent videos via the YouTube Data API, extracts every URL from their descriptions, checks each one for reachability, and produces a detailed HTML audit report showing which links are broken, why, and what to do about it.

---

## What it does

- Fetches up to 100 videos from any public YouTube channel
- Extracts and deduplicates all URLs from video descriptions
- Checks each URL concurrently (HEAD request with GET fallback)
- Categorises failures: 404, 400, 403, timeout, redirect loops
- Estimates monthly and annual affiliate revenue loss based on views, click-through rate, and commission benchmarks
- Generates a standalone HTML report sorted by severity — no server needed, just open it in a browser

---

## Sample output

```
============================================================
  YOUTUBE CHANNEL LINK AUDIT
============================================================
  Channel  : @channelname
  Videos   : 87
  Links    : 214 unique
  Broken   : 31
============================================================

  12 video(s) have broken links:

  My Everyday Makeup Routine 2021
     https://www.youtube.com/watch?v=xxxxx
       x https://rstyle.me/n/abc123
         Reason: HTTP 404
       x https://shopmy.us/collections/xyz
         Reason: Timeout
```

The HTML report includes a revenue impact banner, a summary stats grid, plain-English explanations of each error type, and step-by-step instructions for fixing broken links in YouTube Studio.

---

## Revenue estimate methodology

The revenue estimate is based on views across affected videos and the following assumptions, derived from beauty/lifestyle creator industry benchmarks:

| Variable                                | Value |
| --------------------------------------- | ----- |
| Click-through rate on description links | 0.5%  |
| Conversion rate                         | 2.0%  |
| Average order value                     | $60   |
| Affiliate commission rate               | 15%   |

The report shows a low estimate (half the benchmark assumptions) and a high estimate (full benchmarks). These figures are directional — your actual numbers will vary depending on your niche, audience, and affiliate programmes.

---

## Requirements

- Python 3.11+
- A [YouTube Data API v3 key](https://console.cloud.google.com/)

---

## Setup

**1. Clone the repo**

```bash
git clone https://github.com/prettykaur/link-sniffer.git
cd link-sniffer
```

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

**3. Add your API key**

Create a `.env` file in the project root:

```
YOUTUBE_API_KEY=your_api_key_here
```

The `.gitignore` already excludes `.env` — your key won't be committed.

**4. Create a reports directory**

```bash
mkdir -p reports
```

---

## Usage

```bash
python main.py
```

When prompted, enter a channel URL or handle:

```
Enter YouTube channel URL or @handle
(e.g. https://youtube.com/@channelname or @channelname): @mkbhd
```

The script accepts any of the following formats:

- `@handle`
- `https://youtube.com/@handle`
- `https://youtube.com/channel/UCxxxxxx`
- A bare channel ID (`UCxxxxxx`)

Scanning takes 1–3 minutes depending on the number of videos and links. When complete, the terminal prints a summary and an HTML report is saved to `reports/`.

```bash
# Open the report in your default browser (macOS)
open reports/audit_handle_2026-03-08.html
```

---

## Configuration

All tunable parameters are at the top of `main.py`:

| Variable          | Default | Description                           |
| ----------------- | ------- | ------------------------------------- |
| `MAX_VIDEOS`      | `100`   | Maximum videos to scan per channel    |
| `MAX_WORKERS`     | `10`    | Concurrent threads for link checking  |
| `REQUEST_TIMEOUT` | `8`     | Seconds before a link check times out |

---

## Notes

- The script checks link reachability, not affiliate validity. A link that returns HTTP 200 but has been reassigned to a different product will not be flagged.
- Some servers return 403 for automated requests regardless of link health. The report flags these for manual verification rather than treating them as definitively broken.
- Rate-limited responses (HTTP 429) are noted in the report and marked for manual review.
- The YouTube Data API has a default quota of 10,000 units per day. Scanning 100 videos uses approximately 200–300 units.

---

## License

MIT
