import os, json, re, base64, requests, time
from apify_client import ApifyClient
import gspread
from google.oauth2.service_account import Credentials

# ================= CONFIG =================
APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
SHEET_CREDS_B64 = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

KEYWORDS = ["python developer", "python intern", "it officer"]
TARGET_LOCATION = "lagos"
MIN_SALARY_NGN = 175000
ENTRY_LEVEL_TAGS = ["entry", "junior", "intern", "graduate", "associate"]

# ================= HELPER FUNCTIONS =================
def parse_salary(text: str) -> float:
    if not text: return 0
    cleaned = re.sub(r'[₦,kKmM\s]', '', str(text), flags=re.IGNORECASE)
    nums = re.findall(r'\d+\.?\d*', cleaned)
    return float(nums[0]) if nums else 0

def is_entry_level(title: str, desc: str) -> bool:
    combined = (title + " " + desc).lower()
    return any(tag in combined for tag in ENTRY_LEVEL_TAGS)

def filter_job(job: dict) -> bool:
    loc = (job.get("location", "") or "").lower()
    if TARGET_LOCATION not in loc: return False
    if parse_salary(job.get("salary", "")) < MIN_SALARY_NGN: return False
    if not is_entry_level(job.get("title", ""), job.get("description", "")): return False
    title_desc = (job.get("title", "") + " " + job.get("description", "")).lower()
    return any(kw in title_desc for kw in KEYWORDS)

def send_webhook(jobs: list):
    if not jobs: return
    payload = {"content": "🇳🇬 **New Job Matches Found**\n", "embeds": []}
    for j in jobs[:5]:
        payload["embeds"].append({
            "title": f"{j['title']} @ {j['company']}",
            "description": f"📍 {j['location']}\n💰 {j.get('salary', 'Negotiable')}\n🔗 [Apply Now]({j['url']})",
            "color": 5814783
        })
    requests.post(WEBHOOK_URL, json=payload)

def sync_to_sheets(jobs: list):
    if not jobs: return
    creds_data = base64.b64decode(SHEET_CREDS_B64)
    creds = Credentials.from_service_account_info(json.loads(creds_data), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID).sheet1
    if sheet.row_count < 1:
        sheet.append_row(["Date Added", "Title", "Company", "Location", "Salary", "URL", "Status"])
    rows = [[os.getenv("GITHUB_SHA", "manual")[:7], j["title"], j["company"], j["location"], j.get("salary", ""), j["url"], "Not Applied"] for j in jobs]
    sheet.append_rows(rows, value_input_option="USER_ENTERED")

# ================= MAIN =================
def run():
    client = ApifyClient(APIFY_TOKEN)
    matched = []
    seen_urls = set()
    
    # Build search URLs for Nigerian job boards
    urls = []
    for kw in KEYWORDS:
        kw_enc = kw.replace(" ", "%20")
        urls.append(f"https://www.jobberman.com/jobs?keyword={kw_enc}&location=Lagos")
        urls.append(f"https://www.myjobmag.com/jobs/keyword/{kw.replace(' ', '-')}/lagos")
    
    # Use official Apify website crawler (most reliable)
    run_input = {
        "startUrls": [{"url": u} for u in urls],
        "maxCrawlPages": 30,
        "maxRequestRetries": 2,
        "requestTimeoutSecs": 60,
        "pageFunction": """async function pageFunction(context) {
            const $ = context.jQuery;
            const pageTitle = $('title').text();
            // Skip non-job pages
            if (!pageTitle.toLowerCase().includes('job')) return;
            
            // Extract job data (generic fallback - customize per site if needed)
            return {
                title: $('h1').first().text().trim() || $('[itemprop="title"]').text().trim(),
                company: $('[itemprop="hiringOrganization"]').text().trim() || $('.company-name').text().trim(),
                location: $('[itemprop="jobLocation"]').text().trim() || $('.location').text().trim(),
                salary: $('.salary').text().trim() || $('[itemprop="baseSalary"]').text().trim(),
                url: context.request.url,
                description: $('body').text().slice(0, 2000)
            };
        }"""
    }
    
    print("🔄 Running Apify website crawler...")
    try:
        run = client.actor("apify/website-content-crawler").call(run_input=run_input, wait_secs=180)
        dataset = client.dataset(run["defaultDatasetId"]).list_items().items
        
        for item in dataset:
            url = item.get("url", "")
            if not url or url in seen_urls: continue
            seen_urls.add(url)
            # Normalize keys
            job = {
                "title": item.get("title", "") or item.get("pageTitle", ""),
                "company": item.get("company", "") or item.get("organization", ""),
                "location": item.get("location", "") or item.get("address", ""),
                "salary": item.get("salary", ""),
                "url": url,
                "description": item.get("description", "") or item.get("text", "")
            }
            if filter_job(job):
                matched.append(job)
                
    except Exception as e:
        print(f"⚠️ Apify crawl failed: {e}")
        return  # Exit early to avoid sheet/webhook errors
    
    print(f"✅ Found {len(matched)} matching jobs.")
    if matched:
        sync_to_sheets(matched)
        send_webhook(matched)
    else:
        print("📭 No new matches this run.")

if __name__ == "__main__":
    run()
