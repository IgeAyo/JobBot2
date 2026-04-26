import os, json, re, base64, requests
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
ENTRY_LEVEL_TAGS = ["entry", "junior", "intern", "graduate", "associate", "nysc", "trainee", "0-1 year", "0-2 year"]

# ================= HELPER FUNCTIONS =================
def parse_salary(text: str) -> float:
    if not text: return 0
    cleaned = re.sub(r'[₦,\s]', '', str(text).lower())
    nums = re.findall(r'(\d+)(?:k|kngn|ngn)?', cleaned)
    if not nums: return 0
    values = [float(n) * (1000 if 'k' in cleaned else 1) for n in nums]
    return min(values)

def is_entry_level(title: str, desc: str) -> bool:
    combined = (title + " " + desc).lower()
    return any(tag in combined for tag in ENTRY_LEVEL_TAGS)

def filter_job(job: dict, debug: bool = False, debug_id: str = "") -> bool:
    title = job.get("title", "") or ""
    company = job.get("company", "") or ""
    location = (job.get("location", "") or "").lower()
    salary_text = job.get("salary", "") or ""
    description = job.get("description", "") or ""
    
    if debug:
        print(f"\n🔎 FILTER DEBUG [{debug_id}]:")
        print(f"   title: '{title[:80]}{'...' if len(title)>80 else ''}'")
        print(f"   company: '{company[:50]}'")
        print(f"   location: '{location}'")
        print(f"   salary: '{salary_text}'")
        print(f"   keywords in title/desc: {any(kw in (title+description).lower() for kw in KEYWORDS)}")
    
    # Location check
    if TARGET_LOCATION not in location and "lagos state" not in location:
        if debug: print(f"   ❌ REJECTED: location '{location}' doesn't contain '{TARGET_LOCATION}'")
        return False
    
    # Salary check (only reject if salary was detected but too low)
    salary_num = parse_salary(salary_text)
    if salary_num > 0 and salary_num < MIN_SALARY_NGN:
        if debug: print(f"   ❌ REJECTED: salary ₦{salary_num:,.0f} < ₦{MIN_SALARY_NGN:,}")
        return False
    elif salary_num == 0 and debug:
        print(f"   ⚠️  WARNING: Could not parse salary from '{salary_text}'")
    
    # Entry-level check
    if not is_entry_level(title, description):
        if debug: print(f"   ❌ REJECTED: no entry-level tags in title/desc")
        return False
    
    # Keyword check
    title_desc = (title + " " + description).lower()
    if not any(kw in title_desc for kw in KEYWORDS):
        if debug: print(f"   ❌ REJECTED: none of {KEYWORDS} found in title/desc")
        return False
    
    if debug: print(f"   ✅ PASSED ALL FILTERS")
    return True

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
    
    urls = []
    for kw in KEYWORDS:
        kw_enc = kw.replace(" ", "%20")
        urls.append(f"https://www.jobberman.com/jobs?keyword={kw_enc}&location=Lagos")
        urls.append(f"https://www.myjobmag.com/jobs/keyword/{kw.replace(' ', '-')}/lagos")
    
    run_input = {
        "startUrls": [{"url": u} for u in urls],
        "maxCrawlPages": 20,
        "maxRequestRetries": 1,
        "requestTimeoutSecs": 45,
        "usePlaywright": True,
        "pageFunction": """async function pageFunction(context) {
            const { page, request } = context;
            await page.waitForLoadState('networkidle').catch(() => {});
            const title = await page.title();
            if (!title.toLowerCase().includes('job') && !title.toLowerCase().includes('career')) return;
            const extractText = async (selector) => {
                try {
                    const el = await page.$(selector);
                    return el ? await el.evaluate(el => el.textContent.trim()) : '';
                } catch { return ''; }
            };
            return {
                title: await extractText('h1') || await extractText('[itemprop="title"]') || await extractText('.job-title') || title,
                company: await extractText('[itemprop="hiringOrganization"]') || await extractText('.company-name') || await extractText('.employer'),
                location: await extractText('[itemprop="jobLocation"]') || await extractText('.location') || await extractText('.job-location'),
                salary: await extractText('.salary') || await extractText('[itemprop="baseSalary"]') || await extractText('.remuneration'),
                url: request.url,
                description: await page.evaluate(() => document.body.innerText.slice(0, 1500))
            };
        }"""
    }
    
    print("🔄 Running Apify website crawler...")
    try:
        run = client.actor("apify/website-content-crawler").call(run_input=run_input, wait_secs=120)
        dataset = client.dataset(run["defaultDatasetId"]).list_items().items
        print(f"📦 Received {len(dataset)} raw items from crawler")
        
        for idx, item in enumerate(dataset, 1):
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
            
            # Debug filter evaluation WITH field values
            if filter_job(job, debug=True, debug_id=f"ITEM_{idx}"):
                matched.append(job)
                
    except Exception as e:
        print(f"⚠️ Apify crawl failed: {e}")
        import traceback; traceback.print_exc()
        return
    
    print(f"\n✅ Final: Found {len(matched)} matching jobs out of {len(seen_urls)} unique URLs crawled.")
    if matched:
        sync_to_sheets(matched)
        send_webhook(matched)
    else:
        print("📭 No new matches. Check FILTER DEBUG logs above to tune criteria.")

if __name__ == "__main__":
    run()
